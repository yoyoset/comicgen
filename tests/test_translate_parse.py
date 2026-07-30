"""_parse_numbered / _single_prompt 的红绿测试。

四个场景来自实测复现（改动前全部静默通过/丢数据，改动后全部正确判定失败并回退）：
  1. 编号跳号又多出一条（模型漏译第 4 条、多输出第 7 条）
  2. 续行（模型把一条译文拆成两行输出）
  3. 多输出的条目（n=3 却给了 5 条）
  4. 重复编号（模型把编号打重了）
"""
from comic_gen.core.config import Config
from comic_gen.pipeline.translate import BaseTranslator


def test_skipped_and_extra_number_fails_parse():
    """实测场景 1：模型输出编号 1,2,3,5,6,7（n=6，跳过 4，多出 7）。

    旧实现：len(items)==6==n 直接判定"成功"，返回 ['一','二','三','','五','六']，
    第 4 条静默变空、第 7 条静默丢弃。新实现必须判定失败触发逐条重试。
    """
    text = "1. 一\n2. 二\n3. 三\n5. 五\n6. 六\n7. 七"
    assert BaseTranslator._parse_numbered(text, 6) is None


def test_continuation_line_appended_not_dropped():
    """实测场景 2：模型把一条译文分两行输出，续行不带编号。"""
    text = "1. 第一行\n这是第一条的第二行\n2. 第二条\n3. 第三条"
    result = BaseTranslator._parse_numbered(text, 3)
    assert result == ["第一行 这是第一条的第二行", "第二条", "第三条"]


def test_extra_items_beyond_n_fails_parse():
    """实测场景 3：n=3 但模型幻觉多输出了 4、5 两条，不应静默丢弃。"""
    text = "1. a\n2. b\n3. c\n4. d\n5. e"
    assert BaseTranslator._parse_numbered(text, 3) is None


def test_duplicate_number_fails_parse():
    """实测场景 4：重复编号，旧实现后者覆盖前者、判定"成功"；新实现必须失败。"""
    text = "1. first\n1. SECOND\n2. b\n3. c"
    assert BaseTranslator._parse_numbered(text, 3) is None


def test_normal_case_still_parses():
    text = "1. 本文1的翻译\n2. 本文2的翻译"
    assert BaseTranslator._parse_numbered(text, 2) == ["本文1的翻译", "本文2的翻译"]


def test_cjk_punctuation_still_tolerated():
    assert BaseTranslator._parse_numbered("1、甲\n2：乙", 2) == ["甲", "乙"]


def test_missing_items_fails_parse():
    assert BaseTranslator._parse_numbered("1. 翻译1\n2. 翻译2", 3) is None


def test_preamble_before_first_number_ignored():
    """编号前的杂散行（模型输出了寒暄）没有 last_key 可追加，丢弃而不报错。"""
    text = "好的，以下是翻译：\n1. 甲\n2. 乙"
    assert BaseTranslator._parse_numbered(text, 2) == ["甲", "乙"]


def test_single_prompt_includes_neighbors():
    texts = ["第一句", "第二句", "第三句"]
    p = BaseTranslator._single_prompt(texts, 1, Config(target_lang="简体中文"))
    assert "第一句" in p
    assert "第三句" in p
    assert "第二句" in p  # 被翻译的那句本身


def test_single_prompt_boundary_has_no_before_after():
    texts = ["唯一一句"]
    p = BaseTranslator._single_prompt(texts, 0, Config())
    assert "上下文" not in p  # 前后都没有时不该塞一堆空上下文进提示词
    assert "唯一一句" in p
