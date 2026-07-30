"""wrap_by_width / apply_kinsoku / balance_lines 纯函数测试。

全部用 measure=len 注入，不加载字体、不依赖 PIL 的 ImageDraw。
"""
from comic_gen.pipeline.render import (
    NOT_LINE_END,
    NOT_LINE_START,
    apply_kinsoku,
    balance_lines,
    wrap_by_width,
)


# ---------------------------------------------------------------- wrap_by_width


def test_wrap_by_width_basic():
    assert wrap_by_width("abcdef", len, 3) == ["abc", "def"]


def test_wrap_by_width_respects_explicit_newline():
    assert wrap_by_width("ab\ncd", len, 10) == ["ab", "cd"]


def test_wrap_by_width_single_char_wider_than_max_w_still_placed():
    """单字超宽也要放进一行（不能因为超宽就丢字）。"""
    assert wrap_by_width("a", len, 0) == ["a"]


def test_wrap_by_width_empty_text():
    assert wrap_by_width("", len, 10) == []


# ---------------------------------------------------------------- apply_kinsoku


def test_kinsoku_line_start_punctuation_pushed_to_previous_line():
    """"下移末字"：把上一行末字挪到下一行开头，让违规标点退居第二个字符，而不是
    把标点本身拉回上一行末尾（那是"上提"，会让上一行变长，本实现不用这种方式）。
    """
    lines = ["今天天气很", "。我们出去"]
    result = apply_kinsoku(lines, len, 10)
    assert result == ["今天天气", "很。我们出去"]
    assert result[1][0] not in NOT_LINE_START  # 违规标点不再是行首
    assert "".join(result) == "".join(lines)  # 总字数不变


def test_kinsoku_line_end_open_bracket_pushed_to_next_line():
    lines = ["他说（", "你好）"]
    result = apply_kinsoku(lines, len, 10)
    assert result == ["他说", "（你好）"]


def test_kinsoku_single_line_unchanged():
    assert apply_kinsoku(["只有一行"], len, 10) == ["只有一行"]


def test_kinsoku_no_violation_unchanged():
    lines = ["今天天气很好", "我们出去玩"]
    assert apply_kinsoku(lines, len, 10) == lines


def test_kinsoku_gives_up_when_would_empty_a_line():
    """上一行只剩 1 个字符时不能再挪，宁可保留违规也不能把行清空。"""
    lines = ["a", "。后面的内容"]
    result = apply_kinsoku(lines, len, 20)  # max_w 给够，隔离出"长度 guard"单独生效
    assert all(ln for ln in result)  # 没有行被掏空
    # 上一行只有 1 字符，guard 生效，不发生移动
    assert result == ["a", "。后面的内容"]


def test_kinsoku_skips_push_that_would_overflow_receiving_line():
    """实测复现的真 bug：下移操作不会让"送出方"超宽，但会让"接收方"变长——如果不检查
    接收方的宽度，会把"违规标点"问题换成"文字溢出边框"问题（渲染时肉眼可见文字越界）。
    max_w 卡在刚好不够接收新字符时，必须放弃这次下移，保留原始的禁则违规。
    """
    lines = ["今天天气很", "。我们出去"]
    max_w = 5  # 接收行 "很。我们出去" 长度 6，超过 5，push 必须被拒绝
    result = apply_kinsoku(lines, len, max_w)
    assert result == lines


def test_kinsoku_idempotent():
    lines = ["今天天气很", "。我们出去"]
    once = apply_kinsoku(lines, len, 10)
    twice = apply_kinsoku(once, len, 10)
    assert once == twice


def test_kinsoku_never_leaves_empty_line():
    """连续多组违规标点也不应把任何一行掏空。"""
    lines = ["甲", "。", "乙", "、"]
    result = apply_kinsoku(lines, len, 20)
    assert all(ln for ln in result)


def test_kinsoku_constants_are_disjoint():
    assert not (set(NOT_LINE_START) & set(NOT_LINE_END))


# ---------------------------------------------------------------- balance_lines


def test_balance_lines_eliminates_orphan_last_line():
    """实测场景：13 个字、宽度 6，贪心得到 [6,6,1] 的孤字行；平衡后不应再是这个形状。"""
    text = "啊" * 13
    result = balance_lines(text, len, 6)
    assert "".join(result) == text
    assert len(result) == 3  # 行数不变
    assert result != ["啊" * 6, "啊" * 6, "啊"]
    assert all(len(ln) >= 1 for ln in result)
    # 不再出现只剩 1 字的末行
    assert len(result[-1]) > 1


def test_balance_lines_preserves_line_count():
    text = "一二三四五六七八九十甲乙丙丁"
    baseline = wrap_by_width(text, len, 5)
    result = balance_lines(text, len, 5)
    assert len(result) == len(baseline)


def test_balance_lines_preserves_total_text():
    text = "一二三四五六七八九十甲乙丙丁戊己庚"
    result = balance_lines(text, len, 4)
    assert "".join(result) == text


def test_balance_lines_single_line_passthrough():
    assert balance_lines("短文字", len, 100) == ["短文字"]


def test_balance_lines_empty_text():
    assert balance_lines("", len, 10) == []
