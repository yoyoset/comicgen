"""render.py 排版度量修正的红绿测试（真实字体，不需要网络/GPU）。"""
from PIL import Image, ImageDraw
import numpy as np
import pytest
from pathlib import Path

from comic_gen.core.config import Config
from comic_gen.core.models import Region
from comic_gen.pipeline.render import (
    _fits,
    _line_advance,
    _load_font,
    _pick_size,
    _stroke_for,
    render_page,
)

_HAS_MSYH = Path(r"C:\Windows\Fonts\msyh.ttc").exists()
pytestmark = pytest.mark.skipif(not _HAS_MSYH, reason="需要 Windows 系统 msyh.ttc 字体")


def test_line_advance_exceeds_old_1_3x_assumption():
    """实测：ascent+descent 恒大于旧代码假设的 size*1.3，是行间压叠的根因。"""
    for size in (10, 16, 24, 40, 48):
        font = _load_font(r"C:\Windows\Fonts\msyh.ttc", size)
        assert _line_advance(font) > size * 1.3


def test_line_advance_fallback_without_getmetrics():
    class FakeFont:
        size = 20

    assert _line_advance(FakeFont()) == 20 * 1.35


def test_stroke_width_scales_with_size():
    light = (255, 255, 255)
    dark = (0, 0, 0)
    assert _stroke_for(dark, 40) == ((0, 0, 0), 0)  # 深色不描边
    _, sw_small = _stroke_for(light, 10)
    _, sw_large = _stroke_for(light, 48)
    assert sw_small >= 1
    assert sw_large > sw_small  # 随字号变大


def test_pick_size_vertical_upper_bound_respects_box_width():
    """实测：旧实现竖排字号上界完全不看 bw，窄框仍会从 max_font_size 开始试。"""
    cfg = Config(max_font_size=48, min_font_size=10)
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font, sw, fits = _pick_size(draw, "中" * 10, 45, 900, "vertical", cfg, (0, 0, 0))
    assert font.size <= 45


def test_fits_accounts_for_stroke_width():
    """sw>0 时有效宽高变小，同样的文字在描边下更容易判定为不适配。"""
    cfg = Config()
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font = _load_font(cfg.font_path, 40)
    text = "你好世界"
    fits_no_stroke = _fits(draw, text, font, 200, 60, "horizontal", sw=0)
    fits_with_stroke = _fits(draw, text, font, 200, 60, "horizontal", sw=20)
    assert fits_no_stroke
    assert not fits_with_stroke  # 描边挤占空间，同样内容不再适配


def test_render_page_horizontal_text_visually_centered():
    """anchor='mm' 修复：墨迹几何中心应接近框中心，不再系统性偏下 ~24% em。"""
    cfg = Config()
    img = Image.new("RGB", (200, 200), "white")
    regions = [Region(bbox=[20, 20, 160, 160], direction="horizontal",
                       translated_text="你好", color=[0, 0, 0])]
    out = render_page(img, regions, cfg)
    arr = np.array(out.convert("L")) < 128
    ys, xs = np.where(arr)
    box_cx, box_cy = 20 + 160 / 2, 20 + 160 / 2
    ink_cx, ink_cy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
    assert abs(ink_cx - box_cx) < 5
    assert abs(ink_cy - box_cy) < 5  # 旧实现在 size=40 时偏移达 ~9.5px


def test_render_page_multiline_no_overlap():
    """多行文字：相邻行的墨迹带之间不应出现负间隙（即不应压叠成一坨）。"""
    cfg = Config()
    img = Image.new("RGB", (300, 300), "white")
    long_text = "中文测试文字换行内容比较长会分成好几行看看重不重叠"
    regions = [Region(bbox=[10, 10, 280, 280], direction="horizontal",
                       translated_text=long_text, color=[0, 0, 0])]
    out = render_page(img, regions, cfg)
    arr = np.array(out.convert("L")) < 128
    row_has_ink = arr.any(axis=1)
    bands = []
    in_band = False
    start = 0
    for y, v in enumerate(row_has_ink):
        if v and not in_band:
            start, in_band = y, True
        elif not v and in_band:
            bands.append((start, y - 1))
            in_band = False
    if in_band:
        bands.append((start, len(row_has_ink) - 1))
    assert len(bands) >= 2  # 确保真的分成了多行，不是退化成单行
    gaps = [bands[i + 1][0] - bands[i][1] for i in range(len(bands) - 1)]
    assert all(g >= 0 for g in gaps)


def test_render_page_overflow_warning_printed(capsys):
    """极端情况（框极小、文字极长）缩到 min_font_size 仍放不下时要打印警告，不静默溢出。"""
    cfg = Config(min_font_size=10, max_font_size=48)
    img = Image.new("RGB", (500, 500), "white")
    regions = [Region(bbox=[0, 0, 15, 15], direction="horizontal",
                       translated_text="这是一段完全不可能塞进这么小框里的长文字内容",
                       color=[0, 0, 0])]
    render_page(img, regions, cfg)
    captured = capsys.readouterr()
    assert "仍超出边框" in captured.err


def test_render_page_fitting_text_no_warning(capsys):
    cfg = Config()
    img = Image.new("RGB", (300, 300), "white")
    regions = [Region(bbox=[10, 10, 280, 280], direction="horizontal",
                       translated_text="短文字", color=[0, 0, 0])]
    render_page(img, regions, cfg)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_load_font_is_cached():
    """lru_cache：同样的 (path,size) 返回同一个对象，不是每次都重新打开文件。"""
    a = _load_font(r"C:\Windows\Fonts\msyh.ttc", 24)
    b = _load_font(r"C:\Windows\Fonts\msyh.ttc", 24)
    assert a is b
