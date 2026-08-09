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
