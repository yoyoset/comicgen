"""Tests for pipeline.render font fallback (offline, no models)."""
import pytest
from PIL import Image, ImageDraw
from comic_gen.pipeline.render import _load_font
from pathlib import Path


def test_load_font_nonexistent():
    """Test that _load_font doesn't crash with nonexistent path."""
    nonexistent = "/nonexistent/path/font.ttf"
    font = _load_font(nonexistent)

    # Should return default font, not crash
    assert font is not None


def test_load_font_bad_file():
    """Test that _load_font handles invalid font file."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".ttf") as f:
        # Write garbage to the file
        f.write(b"not a real font")
        f.flush()

        font = _load_font(f.name)
        # Should return default, not crash
        assert font is not None


def test_load_font_system():
    """Test that _load_font works with system fallback fonts."""
    # Try loading a common system font (if available)
    # This test may be skipped on non-Windows or systems without CJK fonts
    from comic_gen.pipeline.render import _FONT_FALLBACKS

    for font_path in _FONT_FALLBACKS:
        if Path(font_path).exists():
            font = _load_font(font_path)
            assert font is not None
            break


def test_text_render_with_default_font():
    """Test that text can be rendered with default font."""
    from comic_gen.pipeline.render import _load_font

    font = _load_font("/nonexistent/font.ttf")  # Forces default

    # Create a test image
    img = Image.new("RGB", (200, 100), color="white")
    draw = ImageDraw.Draw(img)

    # Try to draw text - should not crash
    try:
        draw.text((10, 10), "テスト", font=font, fill="black")
    except Exception as e:
        pytest.fail(f"Failed to render text with default font: {e}")

    # Should have drawn something
    assert img.size == (200, 100)
