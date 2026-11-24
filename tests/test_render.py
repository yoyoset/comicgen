"""Tests for pipeline.render font fallback (offline, no models)."""
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from comic_gen.pipeline.render import _FONT_FALLBACKS, _load_font


def test_load_font_nonexistent():
    """路径不存在时要回落到系统字体/PIL 默认字体，绝不能抛异常打断整页渲染。"""
    assert _load_font("/nonexistent/path/font.ttf", 20) is not None


def test_load_font_bad_file():
    """文件存在但不是字体（OSError）同样要回落。"""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".ttf", delete=False) as f:
        f.write(b"not a real font")
        path = f.name
    try:
        assert _load_font(path, 20) is not None
    finally:
        Path(path).unlink(missing_ok=True)


def test_load_font_system():
    for font_path in _FONT_FALLBACKS:
        if Path(font_path).exists():
            assert _load_font(font_path, 20) is not None
            break


def test_text_render_with_fallback_font():
    font = _load_font("/nonexistent/font.ttf", 20)
    img = Image.new("RGB", (200, 100), color="white")
    draw = ImageDraw.Draw(img)
    try:
        draw.text((10, 10), "テスト", font=font, fill="black")
    except Exception as e:
        pytest.fail(f"Failed to render text with fallback font: {e}")
    assert img.size == (200, 100)


def test_forced_black_text_ignores_region_color():
    """交付标准：译文一律纯黑实心——区域里存的红/白颜色不能被沿用。

    回归背景（2026-11-06）：彩页和拟声字的取色会让成品出现彩色字；浅色字更糟，
    为了看得见必须加黑描边，出来就是"中空字"。这两条都被用户明确要求去掉。
    """
    from comic_gen.core.models import Region
    from comic_gen.pipeline import render

    assert render.FORCE_BLACK_TEXT is True
    img = Image.new("RGB", (120, 160), color="white")
    img.paste((60, 60, 60), (20, 20, 80, 120))          # 深色底，浅色字也看得见
    red = Region(bbox=[20, 20, 80, 120], direction="vertical",
                 translated_text="红色", color=[255, 0, 0])
    white = Region(bbox=[20, 20, 80, 120], direction="vertical",
                   translated_text="白字", color=[255, 255, 255])

    for region in (red, white):
        out = render.render_page(img, [region], _cfg())
        colors = out.getcolors(1 << 20) or []
        # 只允许出现底色、灰底和纯黑（抗锯齿边是灰阶，允许；但不许有彩色像素）
        for _count, (r, g, b) in colors:
            assert abs(r - g) <= 12 and abs(g - b) <= 12, f"出现彩色像素 {(r, g, b)}"


def _cfg():
    from comic_gen.core.config import Config

    cfg = Config()
    cfg.font_path = ""            # 走系统字体兜底
    return cfg


def test_dark_background_gets_white_text():
    """2026-11-17 用户实测：黑色气泡/夜景上纯黑字完全看不见。

    交付口径仍是"纯色实心、不加描边"，但深底改白字——只用黑/白两种颜色。
    """
    from comic_gen.core.models import Region
    from comic_gen.pipeline import render

    img = Image.new("RGB", (140, 200), color=(18, 18, 18))      # 近黑底
    region = Region(bbox=[20, 20, 100, 160], direction="vertical", translated_text="深底白字")
    out = render.render_page(img, [region], _cfg())
    pixels = list(out.getdata())
    light = sum(1 for r, g, b in pixels if r > 200 and g > 200 and b > 200)
    assert light > 50, "深色底上应当画白字"


def test_light_background_keeps_black_text():
    from comic_gen.core.models import Region
    from comic_gen.pipeline import render

    img = Image.new("RGB", (140, 200), color=(250, 250, 250))
    region = Region(bbox=[20, 20, 100, 160], direction="vertical", translated_text="浅底黑字")
    out = render.render_page(img, [region], _cfg())
    pixels = list(out.getdata())
    dark = sum(1 for r, g, b in pixels if r < 60 and g < 60 and b < 60)
    assert dark > 50, "浅色底上应当画黑字"


def test_background_luminance_uses_median():
    """气泡里有网点/残留笔画时，均值会被带偏，取中位数。"""
    from comic_gen.pipeline.render import background_luminance

    img = Image.new("L", (100, 100), 240)
    img.paste(0, (0, 0, 30, 100))            # 30% 纯黑 + 70% 近白
    lum = background_luminance(img, [0, 0, 100, 100])
    assert lum > 200, f"中位数应当是近白，实际 {lum}"
