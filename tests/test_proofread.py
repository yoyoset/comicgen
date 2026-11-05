"""OCR 校对规则（comic_gen/pipeline/proofread.py）的纯函数测试。

这些规则不是拍脑袋写的：每一条都来自对真实成品的逐页校对（2026-11-06 七龍珠第17卷
229 页）。它们决定"重排到页面上的是不是一句人话"，所以和采样参数一样要用测试锁住。
"""
from comic_gen.pipeline.proofread import (
    apply_confusions,
    normalize_punctuation,
    parse_numbered,
    proofread_lines,
)

# ---------------------------------------------------------------- 字形表 / 位置规则


def test_japanese_shinjitai_is_mapped_to_simplified():
    assert apply_confusions("実戦様値遅剰伝応険黒") == "实战样值迟剩传应险黑"


def test_scan_confusions_from_real_pages():
    assert apply_confusions("狼强") == "很强"
    assert apply_confusions("什廖") == "什么"
    assert apply_confusions("好肥") == "好吧"
    assert apply_confusions("我憧了") == "我懂了"
    assert apply_confusions("岩有此理") == "岂有此理"
    assert apply_confusions("我拔不到他") == "我找不到他"


def test_sentence_final_question_particle():
    assert apply_confusions("是克林喂？") == "是克林吗？"
    assert apply_confusions("你愿意离开这里妈？") == "你愿意离开这里吗？"


def test_leading_wei_is_kept_as_interjection():
    # 句首的「喂」是真在喊人，不能当语气词改成「吗」
    assert apply_confusions("喂…喂…！") == "喂…喂…！"


def test_na_before_direction_word_becomes_na_where():
    assert apply_confusions("往娜边？") == "往哪边？"
    assert apply_confusions("娜美") == "娜美"  # 人名里的娜不动


def test_yan_before_person_is_gen():
    assert apply_confusions("眼你们拼了") == "跟你们拼了"
    assert apply_confusions("眼睛") == "眼睛"  # 真·眼睛不动
    assert apply_confusions("眼泪") == "眼泪"


def test_katakana_leaks_are_mapped():
    assert apply_confusions("カ量") == "力量"
    assert apply_confusions("オ能") == "才能"


# ---------------------------------------------------------------- 标点归一


def test_punctuation_normalization_from_real_pages():
    # 句尾的「：」转成逗号后没有下文，等于多余的标点，直接去掉
    assert normalize_punctuation("哈哈哈：") == "哈哈哈"
    assert normalize_punctuation("在你死之前；我先说") == "在你死之前，我先说"
    assert normalize_punctuation("他逃出去了．．．！") == "他逃出去了…！"
    assert normalize_punctuation("呜啊啊啊｜｜") == "呜啊啊啊！！"
    assert normalize_punctuation("哈哈哈：．．．！！") == "哈哈哈，…！！"


def test_clock_like_colon_is_kept():
    assert normalize_punctuation("１２：３０") == "１２：３０"


def test_no_trailing_comma():
    assert normalize_punctuation("好，") == "好"


# ---------------------------------------------------------------- LLM 校对的解析与护栏


def test_parse_numbered_trusts_only_contiguous_prefix():
    raw = "1. 甲\n2. 乙\n4. 丁"
    assert parse_numbered(raw, 4) == ["甲", "乙", None, None]


def test_proofread_keeps_original_when_model_rewrites_too_much():
    def llm(_prompt):
        return "1. 完全改写成了一个跟原文毫无关系而且长得多的句子" + "啊" * 40

    fixed, changed = proofread_lines(["短句"], llm)
    assert fixed == ["短句"]
    assert changed == 0


def test_proofread_applies_small_fix():
    def llm(_prompt):
        return "1. 我找不到他\nEND"

    fixed, changed = proofread_lines(["我拔不到他"], llm)
    assert fixed == ["我找不到他"]
    assert changed == 1


def test_proofread_falls_back_when_model_raises():
    def llm(_prompt):
        raise RuntimeError("boom")

    fixed, _ = proofread_lines(["原样"], llm)
    assert fixed == ["原样"]
