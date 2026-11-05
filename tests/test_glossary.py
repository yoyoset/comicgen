"""术语表（pipeline/glossary.py）的纯函数行为。

背景：2026-11-05 灌篮高手英文版 v01 实测，同一卷里 HANAMICHI 被译成「花火」和「花道」
两种写法，源头是 EasyOCR 对风格化字体把它识别成 `HANAMiCHiW`。这些测试锁住的就是
"这种 OCR 错拼也必须命中术语表"以及"不相关的词一个都不许替换"。
"""
from comic_gen.pipeline.glossary import (
    apply_glossary,
    glossary_prompt,
    normalize_term,
    parse_glossary_text,
)

GLOSSARY = {
    "Hanamichi": "樱木花道",
    "Sakuragi": "樱木花道",
    "Akagi": "赤木刚宪",
    "Haruko": "赤木晴子",
    "Rukawa": "流川枫",
}


# ---------------------------------------------------------------- 解析

def test_parse_skips_comments_blank_lines_and_malformed_rows():
    text = """
# 灌篮高手 v01
Hanamichi=樱木花道

Akagi = 赤木刚宪
这一行没有等号，丢弃
=没有原文
没有译名=
"""
    assert parse_glossary_text(text) == {"Hanamichi": "樱木花道", "Akagi": "赤木刚宪"}


def test_parse_keeps_spaces_in_source_terms_and_supports_fullwidth_equal():
    assert parse_glossary_text("Hanamichi Sakuragi＝樱木花道") == {"Hanamichi Sakuragi": "樱木花道"}


def test_parse_later_duplicate_wins():
    assert parse_glossary_text("Akagi=赤木\nAkagi=赤木刚宪") == {"Akagi": "赤木刚宪"}


def test_normalize_term_strips_punctuation_and_case():
    assert normalize_term("HANAMiCHiW!") == "hanamichiw"
    assert normalize_term("Sakuragi's") == "sakuragis"


# ---------------------------------------------------------------- 替换

def test_exact_name_is_replaced_and_punctuation_kept():
    out = apply_glossary("CONGRATULATIONS, HANAMICHI!", GLOSSARY)
    assert out == "CONGRATULATIONS, 樱木花道!"


def test_ocr_misspelling_still_matches():
    # 真实 OCR 输出（p008）：HANAMICHI 被认成 HANAMiCHiW
    out = apply_glossary("CONGrAt HANAMiCHiW! Fifty rejections", GLOSSARY)
    assert out == "CONGrAt 樱木花道! Fifty rejections"


def test_possessive_form_matches():
    assert apply_glossary("HANAMICHI'S REALLY EASY", GLOSSARY) == "樱木花道 REALLY EASY"


def test_unrelated_words_are_not_touched():
    text = "I HEARD Like; ThE GANG LEADERS AREN'T You SCARED?"
    assert apply_glossary(text, GLOSSARY) == text


def test_empty_glossary_is_identity():
    text = "HANAMICHI!"
    assert apply_glossary(text, {}) == text


def test_multiword_term_replaces_as_one_phrase():
    g = {"Hanamichi Sakuragi": "樱木花道"}
    assert apply_glossary("HANAMICHI SAKURAGI is here", g) == "樱木花道 is here"


def test_single_name_said_twice_collapses():
    # 术语表只收了单个词时，"Sakuragi Hanamichi" 会两半都命中，中文说一次就够
    assert apply_glossary("SAKURAGI HANAMICHI!", GLOSSARY) == "樱木花道!"


def test_short_names_require_exact_match():
    g = {"Ai": "阿井"}
    # 4 字符以下不做模糊匹配，避免 "AT"/"IS" 之类的词被替换
    assert apply_glossary("AI is here", g) == "阿井 is here"
    assert apply_glossary("AS IF", g) == "AS IF"


def test_similarity_threshold_rejects_loose_lookalikes():
    g = {"Suzuki": "铃木"}
    # DAISUKI 与 SUZUKI 相似度 0.77，低于阈值，不能被替换
    assert apply_glossary("DAISUKI", g) == "DAISUKI"


# ---------------------------------------------------------------- 提示词

def test_glossary_prompt_empty_for_no_glossary():
    assert glossary_prompt({}) == ""


def test_glossary_prompt_lists_mappings():
    p = glossary_prompt({"Hanamichi": "樱木花道"})
    assert "Hanamichi=樱木花道" in p
    assert p.endswith("。")
