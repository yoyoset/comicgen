"""翻译分块/部分恢复/输出校验的红绿测试。

背景（实测，493 页杂志一次真实运行）：不分块时密集页把 1 次批量调用放大成上百次
逐条调用（46 页密集页合计 ~2125 次），且 162 个区域的译文其实是提示词模板本身被
当成译文抹字重绘到了页面上。本文件锁住两条修复：
  1. 分块 + 前缀可信解析，让失败半径从"整页"缩小到"一个块的缺口"
  2. 输出校验，让回显/空白/原样吐回原文一律落成空字符串，不再被渲染

全离线，不加载真模型、不联网。BaseTranslator 无 __init__，可直接实例化，
用脚本化的 FakeLLM 替代真实 llama.cpp 调用。
"""
from comic_gen.core.config import Config
from comic_gen.pipeline.translate import (
    _ECHO_MARKERS,
    BaseTranslator,
    SakuraTranslator,
    _has_degenerate_repetition,
    _is_failed_translation,
    _strip_think_block,
)


class FakeLLM:
    """记录每次调用的 (prompt, max_tokens)，按脚本顺序返回；脚本用尽后返回空串。"""

    def __init__(self, replies=None):
        self.calls: list[tuple[str, int]] = []
        self._replies = list(replies) if replies else []

    def __call__(self, prompt: str, max_tokens: int) -> str:
        self.calls.append((prompt, max_tokens))
        if not self._replies:
            return ""
        r = self._replies.pop(0)
        return r(prompt) if callable(r) else r


# ---------------------------------------------------------------- 分块 / 对齐

def test_chunk_indices_partition_is_exact():
    for n in (0, 1, 9, 12, 13, 43, 80):
        texts = [f"t{i}" for i in range(n)]
        chunks = BaseTranslator._chunk_indices(texts)
        flat = [i for c in chunks for i in c]
        assert flat == list(range(n)), f"n={n} 分块未能精确拼回 range(n)"


def test_median_page_is_a_single_chunk():
    texts = [f"台词{i}" for i in range(9)]
    chunks = BaseTranslator._chunk_indices(texts)
    assert len(chunks) == 1


def test_dense_page_is_chunked():
    texts = [f"t{i}" for i in range(80)]
    chunks = BaseTranslator._chunk_indices(texts)
    assert len(chunks) == 7  # ceil(80/12)
    assert chunks[0] == list(range(12))
    assert chunks[-1] == list(range(72, 80))


def test_long_lines_chunk_by_token_budget_not_line_count():
    texts = ["あ" * 120 for _ in range(12)]
    chunks = BaseTranslator._chunk_indices(texts)
    assert len(chunks) > 1
    assert all(len(c) < BaseTranslator._MAX_CHUNK_LINES for c in chunks)


def test_max_tokens_scales_with_chunk_and_never_exceeds_ceiling():
    short_max = BaseTranslator._max_tokens_for(
        BaseTranslator._est_output_tokens("短"), BaseTranslator._OUTPUT_TOKEN_HEADROOM
    )
    long_max = BaseTranslator._max_tokens_for(
        BaseTranslator._est_output_tokens("长" * 200), BaseTranslator._OUTPUT_TOKEN_HEADROOM
    )
    assert BaseTranslator._OUTPUT_TOKEN_FLOOR <= short_max <= BaseTranslator._OUTPUT_TOKEN_CEILING
    assert BaseTranslator._OUTPUT_TOKEN_FLOOR <= long_max <= BaseTranslator._OUTPUT_TOKEN_CEILING
    assert long_max > short_max


# ---------------------------------------------------------------- 部分恢复（回归）

def test_truncated_numbered_list_only_retries_missing_tail():
    """头号回归用例：12 行的块，回复在第 7 条后被截断（模拟 max_tokens 截断）。
    旧实现：整页判定失败，12 条全部逐条重译。新实现：只重试缺失的 5 条。
    """
    texts = [f"line{i}" for i in range(12)]
    truncated = "\n".join(f"{i}. 译{i}" for i in range(1, 8))  # 只有 1..7
    retries = [f"T{i}" for i in range(8, 13)]  # 5 条重试回复，对应缺失的 7..11(0-idx)
    fake = FakeLLM([truncated, *retries])

    result = BaseTranslator()._translate_batch_llm(texts, Config(), fake)

    assert len(fake.calls) == 1 + 5  # 1 次批量 + 5 次单条重试，不是 12 次
    assert result[:7] == [f"译{i}" for i in range(1, 8)]
    assert result[7:] == retries


def test_result_length_always_equals_input_length():
    texts = [f"t{i}" for i in range(5)]
    scenarios = [
        "",  # 空回复
        "一堆无关的话，没有编号",  # 纯垃圾
        "1. a\n1. b\n2. c\n3. d\n4. e",  # 重复编号
        "1. a\n2. b",  # 截断（只有前两条）
        "1. a\n2. b\n3. c\n4. d\n5. e\n6. f\n7. g",  # 幻觉多输出
    ]
    for raw in scenarios:
        fake = FakeLLM([raw])  # 后续重试用默认空串（也算失败）
        result = BaseTranslator()._translate_batch_llm(texts, Config(), fake)
        assert len(result) == 5, f"场景 {raw!r} 结果长度不等于输入长度"
        assert all(isinstance(v, str) for v in result)


def test_single_retry_uses_global_neighbor_context():
    """重试用的上下文必须来自整页原文，而不是重试失败所在那个块的局部邻居。"""
    texts = [f"text{i}" for i in range(13)]  # 13 条 -> 2 块：[0..11] [12]
    # 块 0 只输出 1..11（0-idx 0..10），漏掉块内第 12 条（全局下标 11）——
    # 它的“下一句”落在块 1（全局下标 12），跨块。
    chunk0_reply = "\n".join(f"{i}. 译{i}" for i in range(1, 12))
    chunk1_reply = "1. 译12"
    retry_reply = "重译12"
    fake = FakeLLM([chunk0_reply, chunk1_reply, retry_reply])

    result = BaseTranslator()._translate_batch_llm(texts, Config(), fake)

    retry_prompt = fake.calls[-1][0]
    assert "text12" in retry_prompt  # 全局下标 12 的原文，来自块 1，不是块 0 局部
    assert "text10" in retry_prompt  # 全局下标 10 的原文（前一句）
    assert result[11] == retry_reply


# ---------------------------------------------------------------- 输出校验（回归）

def test_prompt_echo_is_rejected_and_becomes_empty():
    """头号画面回归用例：模型把提示词模板本身回显为"译文"，不能被当成合法译文。"""
    texts = ["ドン"]
    echo = "（上下文，仅供理解语境，不要翻译）把下面这句话翻译成简体中文，只输出译文：\nドン"
    fake = FakeLLM([echo, echo])

    result = BaseTranslator()._translate_batch_llm(texts, Config(), fake)

    assert result == [""]
    assert echo not in result


def test_is_failed_translation_rules():
    assert _is_failed_translation("あ", "") is True
    assert _is_failed_translation("あ", "   ") is True
    assert _is_failed_translation("あ", "把下面这句话翻译成简体中文，只输出译文：\nあ") is True
    assert _is_failed_translation("こんにちは", "こんにちは") is True
    assert _is_failed_translation("こんにちは", "你好") is False


def test_degenerate_repetition_is_rejected():
    """回归：328.JPEG 实测——垃圾 OCR 输入让 Sakura 循环输出同一短句 N 遍，
    `_is_failed_translation` 必须拦下这种结果落空串（否则抹字渲染到页面上）。"""
    src = "そんなことはないのですが……"
    assert _is_failed_translation(src, "那是什么？因为是这样吗？还是因为这样呢？因为是这样吗？因为是这样吗？因为是这样吗？因为是这样吗？因为是这样吗？") is True
    # 无标点的连写退化
    assert _is_failed_translation(src, "这样这样这样这样这样这样这样这样这样这样") is True
    # 输出长度暴涨（循环吃满 max_tokens 的特征）
    assert _is_failed_translation("短", "短" * 300) is True
    # 正常译文不受影响
    assert _is_failed_translation(src, "虽然没有那种事……但就是会这么想呢。而且或许不知道，虽然觉得应该没有，但就是那样。") is False


def test_has_degenerate_repetition_does_not_flag_normal_emphasis():
    """漫画里合法的强调重复（短句、1-2 字重复）不能误伤。"""
    assert _has_degenerate_repetition("加油！加油！加油！") is False
    assert _has_degenerate_repetition("哈哈哈哈哈哈") is False
    assert _has_degenerate_repetition("好。好。好。我们走吧。") is False
    assert _has_degenerate_repetition("我明白了，你明白了，他也明白了。") is False
    assert _has_degenerate_repetition("既然如此，那就没办法了。") is False
    assert _has_degenerate_repetition("……") is False


def test_sakura_repeat_penalty_restored_to_1_1():
    """回归：曾按官方 README 改成 1.0，实测 IQ4XS 量化在垃圾输入下退化成重复循环
    （1500+ 字），1.1 是同输入下的既有缓解。守住这个值不被改回去。"""
    assert SakuraTranslator.repeat_penalty == 1.1


def test_strip_think_block_removes_reasoning_and_keeps_answer():
    raw = "<think>这里应该翻译成……</think>你好"
    assert _strip_think_block(raw) == "你好"


def test_strip_think_block_handles_multiline_reasoning():
    raw = "<think>\n分析一下语境\n再想想措辞\n</think>\n早安。"
    assert _strip_think_block(raw) == "早安。"


def test_strip_think_block_no_think_tag_passes_through_unchanged():
    assert _strip_think_block("你好") == "你好"


def test_echo_markers_still_appear_in_prompts():
    """防漂移：_ECHO_MARKERS 是提示词模板的字面量子串，改了措辞要同步改这里。

    必须覆盖两个引擎：llm 引擎（BaseTranslator）的编号批量格式，和 Sakura 引擎
    （SakuraTranslator）的官方多行直连格式，各自动态的 marker 缺一不可。
    """
    cfg = Config(target_lang="简体中文")
    combined = (
        BaseTranslator._batch_prompt(["a", "b"], cfg)
        + BaseTranslator._single_prompt(["a", "b", "c"], 1, cfg)
        + SakuraTranslator._batch_prompt(["a", "b"], cfg)
        + SakuraTranslator._single_prompt(["a", "b", "c"], 1, cfg)
    )
    for marker in _ECHO_MARKERS:
        assert marker in combined, f"标记 {marker!r} 未出现在任何提示词模板里，测试已失去意义"


def test_echoed_batch_item_does_not_survive():
    """批量回复里格式良好但内容是回显的条目，不能原样通过；重试后应拿到正确译文。"""
    texts = ["甲src", "乙src", "丙src"]
    batch_reply = "1. 甲\n2. 乙\n3. 把下面这句话翻译成简体中文，只输出译文："
    fake = FakeLLM([batch_reply, "丙"])

    result = BaseTranslator()._translate_batch_llm(texts, Config(), fake)

    assert result == ["甲", "乙", "丙"]
    assert len(fake.calls) == 2


# ---------------------------------------------------------------- 成本上限

def test_single_retry_budget_is_capped():
    texts = [f"t{i}" for i in range(80)]
    fake = FakeLLM([])  # 每次调用都返回空串：所有块解析失败，所有重试也失败

    result = BaseTranslator()._translate_batch_llm(texts, Config(), fake)

    expected_chunks = len(BaseTranslator._chunk_indices(texts))
    assert len(fake.calls) == expected_chunks + BaseTranslator._MAX_SINGLE_RETRIES_PER_PAGE
    assert len(result) == 80


def test_uncapped_page_leaves_empty_not_garbage():
    texts = [f"t{i}" for i in range(80)]
    fake = FakeLLM([])

    result = BaseTranslator()._translate_batch_llm(texts, Config(), fake)

    assert all(v == "" for v in result)  # 没有 None 泄漏，也没有半截垃圾文本


# ---------------------------------------------------------------- Sakura 官方格式（回归）

def test_sakura_batch_prompt_is_official_multi_line():
    cfg = Config(target_lang="简体中文")
    prompt = SakuraTranslator._batch_prompt(["あ", "い"], cfg)
    assert prompt == "将下面的日文文本翻译成中文：あ\nい"
    assert "1. " not in prompt  # 不带编号


def test_sakura_single_prompt_keeps_official_format_with_context():
    cfg = Config(target_lang="简体中文")
    prompt = SakuraTranslator._single_prompt(["前", "中", "后"], 1, cfg)
    assert prompt.startswith("（上下文，仅供理解语境，不要翻译）")
    assert prompt.endswith("将下面的日文文本翻译成中文：中")
    assert "编号" not in prompt


def test_parse_plain_lines_alignment():
    assert SakuraTranslator._parse_plain_lines("甲\n乙\n丙", 3) == ["甲", "乙", "丙"]
    assert SakuraTranslator._parse_plain_lines(" 甲 \n 乙 ", 2) == ["甲", "乙"]  # strip
    # 行数不足/超出/空回复：整体置 None，绝不按错误位置错位
    assert SakuraTranslator._parse_plain_lines("甲\n乙", 3) == [None, None, None]
    assert SakuraTranslator._parse_plain_lines("甲\n乙\n丙\n丁", 3) == [None, None, None]
    assert SakuraTranslator._parse_plain_lines("", 3) == [None, None, None]


def test_parse_batch_hook_dispatches_per_engine():
    assert BaseTranslator._parse_batch("1. 甲\n2. 乙", 2) == ["甲", "乙"]
    assert SakuraTranslator._parse_batch("甲\n乙", 2) == ["甲", "乙"]


def test_sakura_translate_uses_official_format_and_plain_parse():
    texts = ["a", "b", "c"]
    reply = "\n".join(["译a", "译b", "译c"])
    fake = FakeLLM([reply])
    # __new__ 绕过 __init__（后者会加载真实模型），只测 batch 流程本身
    inst = SakuraTranslator.__new__(SakuraTranslator)

    result = inst._translate_batch_llm(texts, Config(target_lang="简体中文"), fake)

    assert result == ["译a", "译b", "译c"]
    prompt = fake.calls[0][0]
    assert prompt.startswith("将下面的日文文本翻译成中文：")
    assert prompt == "将下面的日文文本翻译成中文：a\nb\nc"
    assert len(fake.calls) == 1


def test_sakura_line_count_mismatch_retries_individually():
    texts = ["a", "b", "c"]
    # 官方格式回复行数不足（2 行 vs 3 行）→ 整块判 None → 逐条重试 3 次
    fake = FakeLLM(["译a\n译b", "A", "B", "C"])
    inst = SakuraTranslator.__new__(SakuraTranslator)

    result = inst._translate_batch_llm(texts, Config(target_lang="简体中文"), fake)

    assert result == ["A", "B", "C"]
    assert len(fake.calls) == 1 + 3
    assert "将下面的日文文本翻译成中文：" in fake.calls[1][0]  # 重试也是官方单句格式


def test_sakura_official_echo_is_rejected():
    texts = ["ドン"]
    echo = "将下面的日文文本翻译成中文：ドン"
    fake = FakeLLM([echo, echo])
    inst = SakuraTranslator.__new__(SakuraTranslator)

    result = inst._translate_batch_llm(texts, Config(target_lang="简体中文"), fake)

    assert result == [""]  # 回显官方格式提示词 → 落空串保留原文
    assert echo not in result
