"""成品页图片的编码格式：扩展名必须和内容一致，且 JPEG 页不该是无损体积。

背景（2026-11-05 实测）：translated/ 里 995×1515 的一页存成 PNG 有 1.22 MB，
却沿用输入的 `.jpg` 文件名，整卷 299 页因此约 600 MB；同一张图存 JPEG q90 只有
426 KB。这两个断言守的就是"别再退回 PNG + 假后缀"。
"""
from __future__ import annotations

from PIL import Image

from comic_gen.core.utils import OUTPUT_JPEG_QUALITY, _image_save_plan, atomic_save_image


def _sample_image(mode: str = "RGB", size=(120, 180)):
    colour = (200, 30, 30) if mode == "RGB" else 128
    return Image.new(mode, size, colour)


def test_jpg_suffix_writes_real_jpeg(tmp_path):
    out = tmp_path / "page.jpg"
    atomic_save_image(_sample_image(), out)
    assert out.read_bytes()[:2] == b"\xff\xd8"
    with Image.open(out) as im:
        assert im.format == "JPEG"


def test_jpeg_suffix_is_treated_as_jpeg(tmp_path):
    plan = _image_save_plan(tmp_path / "page.JPEG", _sample_image(), OUTPUT_JPEG_QUALITY)
    assert plan[0] == "JPEG"


def test_png_suffix_still_writes_png(tmp_path):
    out = tmp_path / "page.png"
    atomic_save_image(_sample_image(), out)
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_rgba_is_converted_for_jpeg(tmp_path):
    out = tmp_path / "page.jpg"
    atomic_save_image(_sample_image("RGBA"), out)
    with Image.open(out) as im:
        assert im.mode == "RGB"


def test_jpeg_is_much_smaller_than_png(tmp_path):
    # 用带噪声的图才像真实扫描页：纯色图两者都很小，比较没有意义
    noise = Image.effect_noise((400, 600), 48).convert("RGB")
    png_path, jpg_path = tmp_path / "noise.png", tmp_path / "noise.jpg"
    assert _image_save_plan(png_path, noise, OUTPUT_JPEG_QUALITY)[0] == "PNG"
    atomic_save_image(noise, png_path)
    atomic_save_image(noise, jpg_path)
    assert jpg_path.stat().st_size < png_path.stat().st_size / 2
