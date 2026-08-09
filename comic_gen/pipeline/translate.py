"""翻译引擎：本地 Sakura(GGUF，日译中专用) / 本地通用 LLM(GGUF，多语言) / mock（流水线自测）。

全部本地推理，无需联网 API、无需 API Key。
"""
from __future__ import annotations

import re
from typing import Any

from ..core.config import Config
from ..core.errors import InputError, ModelError
from ..core.manifest import MODEL_MANIFEST
from ..core.model_locator import (
    STATUS_PATH_BROKEN,
    STATUS_SIZE_MISMATCH,
    resolve_status,
)
from ..infra.logging import logger

_LANG_NAMES = {"ja": "日文", "en": "英文", "zh": "中文", "other": "原文语言"}

_SAKURA_SYS = (
    "你是一个轻小说翻译模型，可以流畅通顺地以日本轻小说的风格将日文翻译成简体中文，"
    "并联系上下文正确使用人称代词，不要擅自脱离原文情节。"
)

_LLM_SYS_TMPL = (
    "你是专业的漫画翻译。把给定的{src}漫画台词翻译成{dst}。"
    "要求：口语自然、符合漫画语境；保留原文语气和情绪；"
    "拟声词可意译；不要输出任何解释。"
)

# 实测（493 页杂志一次真实运行）：162 个区域的 translated_text 是提示词模板本身被
# 当成译文抹字重绘到了页面上。这些子串取自 _batch_prompt/_single_prompt 的字面量，
# 一旦改了提示词措辞要同步改这里——test_translate_batch.py 有防漂移断言。
_ECHO_MARKERS = (
    "仅供理解语境",
    "只输出译文",
    "保持编号格式输出",
    "不要合并不要遗漏",
    "把下面这句话翻译成",
    "把下列编号文本逐条翻译成",
)


def _is_failed_translation(source: str, output: str) -> bool:
    """明显失败的译文：空白 / 回显了提示词模板 / 原样吐回原文。

    纯函数，不依赖模型。判为失败的译文由调用方落成空字符串——下游
    select_drawable_regions（runner.py）会保留原文不抹除，校对页会列进
    「需要注意」，不需要额外的失败态管线。
    """
    stripped = output.strip()
    if not stripped:
        return True
    if any(marker in stripped for marker in _ECHO_MARKERS):
        return True
    if stripped == source.strip():
        return True
    return False


class BaseTranslator:
    name = "base"

    # 实测（493 页杂志一次真实运行）：不分块时密集页（40+ 区域）预估输出 token 超过
    # 旧硬编码的 max_tokens=1024，输出从中间被截断，_parse_numbered 判定整页解析失败，
    # 触发全页逐条重试——46 页密集页合计把 46 次批量调用放大成约 2125 次调用。
    # 分块把「整页一次调用」改成「按预算切块」，让 90% 的常规页（中位数 9 区域）
    # 仍是 1 次调用不回归，同时让密集页的失败半径从"整页"缩小到"一个块"。
    _MAX_CHUNK_LINES = 12
    _CHUNK_OUTPUT_TOKEN_BUDGET = 480
    _OUTPUT_TOKEN_HEADROOM = 2.0
    _SINGLE_OUTPUT_HEADROOM = 3.0
    _OUTPUT_TOKEN_CEILING = 1024
    _OUTPUT_TOKEN_FLOOR = 96
    # 全页逐条重试曾经是全有全无：80 条里错 1 条，另外 79 条正确译文也要重译一遍。
    # 现在只重试真正缺失的下标，但仍需要一个每页硬上限，防止病态页把重试次数
    # 顶回接近"每个区域一次调用"的老路。
    _MAX_SINGLE_RETRIES_PER_PAGE = 16

    def translate(self, texts: list[str], cfg: Config) -> list[str]:
        raise NotImplementedError

    @staticmethod
    def _est_output_tokens(text: str) -> int:
        """粗估一条译文的输出 token 数（含编号前缀与换行）。未经模型分词器校准，
        只用于分块决策，_MAX_CHUNK_LINES 兜住估算偏差的极端情况。"""
        return 6 + int(len(text) * 1.5)

    @classmethod
    def _max_tokens_for(cls, est: int, headroom: float) -> int:
        return min(cls._OUTPUT_TOKEN_CEILING, max(cls._OUTPUT_TOKEN_FLOOR, int(est * headroom)))

    @classmethod
    def _chunk_indices(cls, texts: list[str]) -> list[list[int]]:
        """把整页切块，返回原始下标分组（不是切片）。

        用原始下标而不是切片，是为了让「各块拼接后恰好等于 range(len(texts))」这个
        对齐契约可以直接断言——runner.py 的 translate() 调用方用 zip(regions, results)
        按位置写回，这个契约一旦破坏就是静默错位。
        """
        chunks: list[list[int]] = []
        cur: list[int] = []
        cur_tokens = 0
        for i, t in enumerate(texts):
            est = cls._est_output_tokens(t)
            if cur and (len(cur) >= cls._MAX_CHUNK_LINES
                        or cur_tokens + est > cls._CHUNK_OUTPUT_TOKEN_BUDGET):
                chunks.append(cur)
                cur = []
                cur_tokens = 0
            cur.append(i)
            cur_tokens += est
        if cur:
            chunks.append(cur)
        return chunks

    @staticmethod
    def _batch_prompt(chunk_texts: list[str], cfg: Config) -> str:
        """块内编号提示词，编号从 1 开始——与全局下标的映射由 _chunk_indices 负责。"""
        numbered = "\n".join(
            f"{i + 1}. {t.replace(chr(10), ' ')}" for i, t in enumerate(chunk_texts)
        )
        return (
            f"把下列编号文本逐条翻译成{cfg.target_lang}，保持编号格式输出，"
            f"每条一行，不要合并不要遗漏：\n{numbered}"
        )

    def _translate_batch_llm(self, texts: list[str], cfg: Config, call_llm) -> list[str]:
        """整页分块编号批量翻译；块内解析缺口只对缺的那几条逐条重试（带上下文），
        并有每页重试上限。返回列表长度恒等于 len(texts)——调用方按位置 zip 写回
        region，短一条就会静默丢掉尾部区域。
        """
        n = len(texts)
        results: list[str | None] = [None] * n
        for chunk in self._chunk_indices(texts):
            chunk_texts = [texts[i] for i in chunk]
            est = sum(self._est_output_tokens(t) for t in chunk_texts)
            max_tokens = self._max_tokens_for(est, self._OUTPUT_TOKEN_HEADROOM)
            raw = call_llm(self._batch_prompt(chunk_texts, cfg), max_tokens)
            parsed = self._parse_numbered(raw, len(chunk))
            for pos, gi in enumerate(chunk):
                val = parsed[pos]
                if val is not None and not _is_failed_translation(texts[gi], val):
                    results[gi] = val

        missing = [i for i in range(n) if results[i] is None]
        if missing:
            logger.warning(
                "翻译分块解析有缺口（%d/%d 条待逐条重试，%s）", len(missing), n, self.name
            )
        retry_budget = missing[: self._MAX_SINGLE_RETRIES_PER_PAGE]
        for i in retry_budget:
            est = self._est_output_tokens(texts[i])
            max_tokens = self._max_tokens_for(est, self._SINGLE_OUTPUT_HEADROOM)
            out = call_llm(self._single_prompt(texts, i, cfg), max_tokens)
            if not _is_failed_translation(texts[i], out):
                results[i] = out
        if len(missing) > len(retry_budget):
            logger.warning(
                "翻译重试预算耗尽（%d 条超出每页上限 %d，%s），保留原文不抹除",
                len(missing) - len(retry_budget), self._MAX_SINGLE_RETRIES_PER_PAGE, self.name,
            )
        return [v if v is not None else "" for v in results]

    @staticmethod
    def _single_prompt(texts: list[str], i: int, cfg: Config) -> str:
        """逐条兜底翻译的提示词：带前后一句原文作上下文，而不是完全孤立地翻译一句。"""
        before = texts[i - 1] if i > 0 else ""
        after = texts[i + 1] if i < len(texts) - 1 else ""
        ctx = ""
        if before or after:
            ctx = (
                f"（上下文，仅供理解语境，不要翻译）前一句：{before or '（无）'}；"
                f"后一句：{after or '（无）'}\n"
            )
        return f"{ctx}把下面这句话翻译成{cfg.target_lang}，只输出译文：\n{texts[i]}"

    @staticmethod
    def _parse_numbered(text: str, n: int) -> list[str | None]:
        """解析"编号. 内容"格式的批量翻译输出。返回长度恒为 n 的列表，不可信的位置为 None。

        只信任 1,2,…,k 这个"每个编号都存在且唯一"的最长前缀，第一个缺口（缺失/重复）
        起全部置 None——数量对不代表编号对（实测：模型漏掉第 4 条却多输出一条第 7 条，
        若天真保留所有合法编号，它的"5,6,7"其实对应原文 4,5,6，会静默渲染三条错译）。
        前缀内的编号不会有这种错位（每个都存在且唯一），所以可信；缺口之后一律当作
        整体不可信，交给上层逐条重试。这也正好匹配最常见的失败模式——max_tokens
        截断丢的是后缀，前缀完整可信。

        不匹配编号格式的行追加到上一条（多行译文的续行），不再静默丢弃。
        超出 n 的编号无需特判：前缀扫描只走 1..n，天然忽略。
        """
        items: dict[int, str] = {}
        dupes: set[int] = set()
        last_key: int | None = None
        for line in text.strip().splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = re.match(r"^\s*(\d+)\s*[.、．:：]\s*(.+)$", stripped)
            if m:
                idx = int(m.group(1))
                if idx in items:
                    dupes.add(idx)
                else:
                    items[idx] = m.group(2).strip()
                last_key = idx
            elif last_key is not None and last_key in items:
                items[last_key] = (items[last_key] + " " + stripped).strip()

        trusted = 0
        for i in range(1, n + 1):
            if i not in items or i in dupes:
                break
            trusted = i
        return [items[i + 1] if i < trusted else None for i in range(n)]


class _LlamaGGUFTranslator(BaseTranslator):
    """本地 GGUF（llama.cpp）翻译引擎基类：加载 + 调用逻辑复用。

    子类的 name 必须与 core.manifest.MODEL_MANIFEST 的键一致——_resolve_model_path
    用它去查 manifest 拿 repo/filename，也用它去 cfg.model_paths 里查显式绑定。
    子类另需设置 system_prompt、采样参数。
    """

    system_prompt: str = ""
    temperature: float = 0.3
    top_p: float = 1.0
    # 实测：pinned 的 llama-cpp-python==0.3.34 默认 repeat_penalty=1.0（即关闭）。
    # 小量化模型在逐条兜底模式面对碎片化输入时会退化成把提示词原样吐回来
    # （_ECHO_MARKERS 命中的 162 个真实区域），开启惩罚是缓解手段之一，但不是
    # 保证——真正兜底的是 _is_failed_translation 的输出校验。
    repeat_penalty: float = 1.1

    def __init__(self, cfg: Config):
        # 让 llama_cpp 的 CUDA 后端能找到 torch 自带的 CUDA 运行时 DLL（torch/lib）
        try:
            from ._cuda_runtime import preload_cuda_runtime
            preload_cuda_runtime()
        except Exception:
            pass
        try:
            from llama_cpp import Llama
        except (ImportError, RuntimeError) as e:
            raise ModelError(
                f"未安装 GPU 版 llama-cpp-python 或 CUDA 运行时缺失，无法使用本地 {self.name} 引擎。"
                "安装见 README。",
                technical=str(e),
            ) from e

        path = self._resolve_model_path(cfg)
        # 之前硬编码 n_gpu_layers=-1（全部层丢 GPU），设置页选"CPU"对这个引擎完全
        # 不起作用——四个模型里只有 detect/inpaint 认 cfg.device，这是另外两个漏洞
        # 之一（另一个是 registry.py 的 OCR 后端）。cfg.device != "cuda" 时退回
        # 0（全 CPU）；GPU 是否真的可用交给 llama_cpp 自己在加载时判断，和这里改动前
        # 的行为一致，不新增一层探测逻辑。
        n_gpu_layers = -1 if cfg.device == "cuda" else 0
        # n_ctx=4096 的取法：分块后单次调用的上下文上界 = system_prompt + 一块内
        # 最多 _MAX_CHUNK_LINES(12) 条原文 + max_tokens 输出上限（_OUTPUT_TOKEN_CEILING，
        # 1024）。4096 对当前分块预算充足；设太小会在长原文页被截断，设太大会增加
        # 上下文分配的显存/分配开销。它与分块预算联动——未来调大 _MAX_CHUNK_LINES
        # 或单条原文显著变长时，需要一并重新评估这里。
        self.llm = Llama(model_path=path, n_ctx=4096, n_gpu_layers=n_gpu_layers, verbose=False)
        self.cfg = cfg

    def _resolve_model_path(self, cfg: Config) -> str:
        """走 core.model_locator 的四态状态机：显式绑定 → 本地搜索 → 联网下载兜底。

        原先无条件走 list_repo_files() 联网列目录挑量化，网络一抖就整个任务失败
        （实测 SSL UNEXPECTED_EOF，权重当时其实就躺在 F:\\hf_cache 里）。
        绑定失效（文件被移走）或大小不对（下载不完整）时必须直接报错，不能悄悄
        回落去联网下载一份新的——那会让用户设置里的绑定形同虚设。
        """
        spec = MODEL_MANIFEST[self.name]
        st = resolve_status(self.name, bindings=cfg.model_paths)
        if st.is_ready:
            logger.info("翻译模型使用：%s", st.path)
            return str(st.path)
        if st.state == STATUS_PATH_BROKEN:
            raise ModelError(
                f"设置里绑定的{spec.label}文件已找不到：{st.path}\n"
                f"请到「模型管理」页重新定位，或清除这条绑定。"
            )
        if st.state == STATUS_SIZE_MISMATCH:
            raise ModelError(
                f"{spec.label}文件大小异常（期望 {st.expected_size} 字节，实际 {st.size} 字节），"
                f"可能下载不完整。请到「模型管理」页重新下载。"
            )

        # 未绑定、本地也没搜到：兜底联网下载
        from huggingface_hub import hf_hub_download, list_repo_files

        name = spec.filename
        try:
            if not name:
                files = [f for f in list_repo_files(spec.repo) if f.lower().endswith(".gguf")]
                for pref in ("q4_k_m", "iq4_xs", "iq4xs", "q5_k_m", "q6_k", "q6k", "q8_0", "q8"):
                    cands = [f for f in files if pref in f.lower()]
                    if cands:
                        name = cands[0]
                        break
                else:
                    name = files[0]
            logger.info("翻译模型本地未找到 %s，从 %s 下载…", name, spec.repo)
            return hf_hub_download(spec.repo, name)
        except Exception as e:
            raise ModelError(
                f"本地未找到{spec.label}（{name or spec.repo}），联网下载也失败：{e}\n"
                f"可在「模型管理」页把「模型目录」指向已有权重所在目录，或直接指定文件路径。",
                technical=str(e),
            ) from e

    def _call(self, prompt: str, max_tokens: int) -> str:
        # llama_cpp 的返回类型被声明成两个响应形状的并集（流式/非流式），mypy 索引
        # 不了；实际调用没开 stream，结果一定是非流式 dict，这里按 Any 处理。
        resp: Any = self.llm.create_chat_completion(
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=self.temperature,
            top_p=self.top_p,
            max_tokens=max_tokens,
            repeat_penalty=self.repeat_penalty,
        )
        content = resp["choices"][0]["message"]["content"]
        # content 可能为 None（模型只产出 reasoning/格式异常时），直接 .strip() 会炸——
        # 旧实现这个位置一旦崩溃，整页被 _run_stage 标成失败而不是走逐条重试。
        # 落成空串后 _parse_numbered 解析不出任何编号，自然进入逐条重试 + 输出校验。
        return (content or "").strip()

    def translate(self, texts: list[str], cfg: Config) -> list[str]:
        return self._translate_batch_llm(texts, cfg, self._call)


class SakuraTranslator(_LlamaGGUFTranslator):
    """本地 Sakura：日译中专用轻小说/漫画翻译模型。"""

    name = "sakura"
    system_prompt = _SAKURA_SYS
    temperature = 0.1
    top_p = 0.3

    def __init__(self, cfg: Config):
        if cfg.source_lang != "ja":
            raise InputError("Sakura 为日文专用模型，源语言非日文请改用通用本地引擎（llm）")
        super().__init__(cfg)


class GeneralLLMTranslator(_LlamaGGUFTranslator):
    """本地通用 LLM：多语言（日/英/中/其它）漫画翻译，非日文场景的默认引擎。"""

    name = "llm"
    temperature = 0.3

    def __init__(self, cfg: Config):
        src = _LANG_NAMES.get(cfg.source_lang, "原文语言")
        self.system_prompt = _LLM_SYS_TMPL.format(src=src, dst=cfg.target_lang)
        super().__init__(cfg)


class MockTranslator(BaseTranslator):
    """流水线自测用：不真正翻译，只打标记。"""

    name = "mock"

    def translate(self, texts: list[str], cfg: Config) -> list[str]:
        return [f"测试译文{i + 1}号" for i in range(len(texts))]


# ---------------------------------------------------------------- 注册表（单一来源）
# 引擎分发/语言锁/推荐映射的实际数据和逻辑搬进了 pipeline/registry.py（新增的 OCR
# 后端注册表放在同一个文件，两者是同一类问题）。这里用模块级 __getattr__（PEP 562）
# 而不是模块顶层 `from .registry import ...` 委托——后者会在真实导入时炸：
# registry.py 的 _build_translator_registry() 需要从本模块拿 SakuraTranslator 等类
# （放在函数体内、惰性 import，本身没问题），但如果 registry.py 先于 translate.py
# 被导入，它会在自己模块体执行到一半时反过来 import 本模块；本模块此时如果在顶层
# 又立刻 `from .registry import TRANSLATOR_REGISTRY`，registry.py 还没把这个名字
# 赋值完，就会报 "cannot import name 'TRANSLATOR_REGISTRY' from partially
# initialized module"——这是实测复现过的真崩溃，不是假设性担忧
# （`from comic_gen.pipeline.registry import get_translator` 单独跑必现）。
# __getattr__ 只在属性访问时才触发，此时两个模块各自的顶层类/函数定义早已跑完，
# 不会撞上"对方还在初始化中"的窗口。
_LAZY_REGISTRY_NAMES = {"get_translator", "engine_lang_conflict", "ENGINE_LANG_LOCK", "ENGINE_RECOMMENDATION"}


def __getattr__(name):
    if name not in _LAZY_REGISTRY_NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import registry

    if name == "ENGINE_LANG_LOCK":
        return {n: e.lang_lock for n, e in registry.TRANSLATOR_REGISTRY.items() if e.lang_lock}
    if name == "ENGINE_RECOMMENDATION":
        return {lang: registry.recommended_engine(lang) for lang in ("ja", "en", "zh", "other")}
    return getattr(registry, name)
