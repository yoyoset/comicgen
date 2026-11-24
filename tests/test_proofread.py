"""OCR 校对规则（comic_gen/pipeline/proofread.py）的纯函数测试。

这些规则不是拍脑袋写的：每一条都来自对真实成品的逐页校对（2026-11-06 七龍珠第17卷
229 页）。它们决定"重排到页面上的是不是一句人话"，所以和采样参数一样要用测试锁住。
"""
from comic_gen.pipeline.proofread import (
    apply_confusions,
    looks_like_ocr_noise,
    normalize_dashes,
    normalize_punctuation,
    parse_numbered,
    proofread_lines,
    render_text,
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


def test_ma_before_question_mark_is_particle():
    # 分区下标偏移时「是马？」会单独成一个气泡，句首没有前字也要吃掉
    assert apply_confusions("是马？") == "是吗？"
    assert apply_confusions("马？") == "吗？"
    assert apply_confusions("马上就去") == "马上就去"  # 「马上」不动


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


# ---------------------------------------------------------------- 重排专用（2026-11-16）


def test_render_text_never_applies_scan_confusions():
    """重排到页面上的是校对后的译文，不能再过 OCR 单字表。

    2026-11-16 实测（用户看死神成品发现）：门被印成斗、悠闲→你闲、死鱼眼→死鱼跟。
    """
    assert render_text("为什么不敲门就打开啊！") == "为什么不敲门就打开啊！"
    assert render_text("是个悠闲的地方。") == "是个悠闲的地方。"
    assert render_text("你这死鱼眼！") == "你这死鱼眼！"


def test_render_text_converts_japanese_long_vowel_to_dash():
    """ー 在竖排里就是一根竖线，连用两个看起来像「一一」（用户实测反馈）。"""
    assert render_text("ーー应该是你！") == "——应该是你！"
    assert render_text("滚开ー！") == "滚开——！"


def test_normalize_dashes_keeps_existing_em_dash():
    assert normalize_dashes("——我实在太没用了！") == "——我实在太没用了！"
    assert normalize_dashes("－－") == "－－"  # 全角连字符可能出现在型号里，不动


def test_normalize_dashes_handles_halfwidth_long_vowel():
    assert normalize_dashes("ｰｰ等等") == "——等等"


# ---------------------------------------------------------------- OCR 幻觉垃圾串（2026-11-16）


def test_ocr_noise_detected():
    """manga-ocr 在装饰花纹/页脚/排名表上幻觉出的日期与条款串。"""
    assert looks_like_ocr_noise("２０１８年１２月２６日（土）第１２条第八第三条第二条第三项の“ホテル")
    assert looks_like_ocr_noise("土曜日お父さんを选択する２月２５日のチームフレー体验の作者、月日")
    assert looks_like_ocr_noise("１９３－１００６年１０月１８日【レート】ＣＡＲ")


def test_dialogue_with_ellipsis_and_numbers_is_not_noise():
    """真实的「……」多、数字多的台词不能被当成垃圾（用全库 12 万格验证过）。"""
    assert not looks_like_ocr_noise("怎……怎么会这样……！我……我们完了……！")
    assert not looks_like_ocr_noise("3年后的5月12日上午10点左右，南都的西南方9公里外的岛屿上会出现恐怖的二人组…")
    assert not looks_like_ocr_noise("2…23000…真的只有23000…？")
    assert not looks_like_ocr_noise("「１８５０００圆…０８０圆！」")
    assert not looks_like_ocr_noise("藤真178！良田168！！")


def test_render_text_drops_noise_but_keeps_dialogue():
    assert render_text("２０１８年１２月２６日（土）第１２条第八第三条") == ""
    assert render_text("为什么不敲门就打开啊！") == "为什么不敲门就打开啊！"
