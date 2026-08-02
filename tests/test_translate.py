"""pipeline.translate 的离线测试：不加载真模型、不联网。

注意：这些测试之前引用的是已删除的 DeepSeekTranslator，且断言 MockTranslator 原样返回输入
（它实际返回"测试译文N号"）——因为 venv 里根本没装 pytest，从来没跑起来过。
现在按真实行为重写。
"""
from comic_gen.core.config import Config
from comic_gen.core.errors import InputError
from comic_gen.pipeline.translate import (
    BaseTranslator,
    MockTranslator,
    engine_lang_conflict,
    get_translator,
)


def test_mock_translator_marks_each_text():
    cfg = Config(translator="mock")
    result = MockTranslator().translate(["本文1", "本文2", "本文3"], cfg)
    assert result == ["测试译文1号", "测试译文2号", "测试译文3号"]


def test_mock_translator_empty():
    assert MockTranslator().translate([], Config()) == []


def test_get_translator_mock_needs_no_model():
    tr = get_translator(Config(translator="mock"))
    assert isinstance(tr, MockTranslator)


def test_get_translator_unknown_engine():
    try:
        get_translator(Config(translator="nope"))
    except InputError as e:
        assert "nope" in str(e)
    else:
        raise AssertionError("未知引擎应该抛 InputError")


def test_parse_numbered_basic():
    result = BaseTranslator._parse_numbered("1. 本文1的翻译\n2. 本文2的翻译", 2)
    assert result == ["本文1的翻译", "本文2的翻译"]


def test_parse_numbered_tolerates_cjk_punctuation():
    """LLM 常输出全角标点，编号解析不能只认半角点。"""
    assert BaseTranslator._parse_numbered("1、甲\n2：乙", 2) == ["甲", "乙"]


def test_parse_numbered_marks_missing_tail_as_none():
    """条数对不上时，缺失位置必须是 None（交给上层逐条重试），绝不能是错位的结果。"""
    assert BaseTranslator._parse_numbered("1. 翻译1\n2. 翻译2", 3) == ["翻译1", "翻译2", None]


def test_engine_lang_conflict():
    assert engine_lang_conflict("sakura", "ja") is None
    assert engine_lang_conflict("llm", "en") is None
    msg = engine_lang_conflict("sakura", "en")
    assert msg and "ja" in msg
