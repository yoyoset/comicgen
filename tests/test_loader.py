"""Tests for pipeline.loader (offline, minimal data)."""
import pytest
import tempfile
import zipfile
from pathlib import Path
from PIL import Image
from comic_gen.pipeline.loader import pack_output, LoadedInput


def create_test_images(tmpdir: Path, count: int = 2):
    """Helper to create test PNG images."""
    imgs = []
    for i in range(count):
        img = Image.new("RGB", (100, 150), color=(255, 255, 255))
        path = tmpdir / f"page_{i+1:03d}.png"
        img.save(path)
        imgs.append(path)
    return imgs


def test_pack_output_folder():
    """Test packing output to folder."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        imgs = create_test_images(tmpdir)

        # Create LoadedInput for folder
        loaded = LoadedInput(
            kind="folder",
            input_path=tmpdir,
            work_dir=tmpdir,
            rel_names=["page_001.png", "page_002.png"],
            pages=[],
        )

        trans_dir = tmpdir / "translated"
        trans_dir.mkdir()

        # Copy test images to translated dir
        for img_path in imgs:
            (trans_dir / img_path.name).write_bytes(img_path.read_bytes())

        output_dir = tmpdir / "output"
        result = pack_output(loaded, trans_dir, output_dir)

        # Check output files exist
        assert result.exists()
        assert (result / "page_001.png").exists()
        assert (result / "page_002.png").exists()


def test_pack_output_zip():
    """Test packing output to ZIP."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        imgs = create_test_images(tmpdir)

        loaded = LoadedInput(
            kind="zip",
            input_path=tmpdir / "in.zip",
            work_dir=tmpdir,
            rel_names=["page_001.png", "page_002.png"],
            pages=[],
        )

        trans_dir = tmpdir / "translated"
        trans_dir.mkdir()

        for img_path in imgs:
            (trans_dir / img_path.name).write_bytes(img_path.read_bytes())

        output_zip = tmpdir / "output.zip"
        result = pack_output(loaded, trans_dir, output_zip)

        # Check ZIP exists and contains files
        assert result.exists()
        assert zipfile.is_zipfile(result)

        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert "page_001.png" in names
            assert "page_002.png" in names


def test_pack_output_missing_images_and_no_original_are_skipped():
    """既没有译文也没有可回退的原图（pages=[] 意味着 loader 层根本没有该页的原始
    路径记录）时，只能跳过——这是真正无解的情况，和"有原图但翻译失败"是两回事。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        imgs = create_test_images(tmpdir, count=1)

        loaded = LoadedInput(
            kind="zip",
            input_path=tmpdir / "in.zip",
            work_dir=tmpdir,
            rel_names=["page_001.png", "page_002.png"],  # page_002 无译文也无原图记录
            pages=[],
        )

        trans_dir = tmpdir / "translated"
        trans_dir.mkdir()

        # Only copy page_001
        (trans_dir / "page_001.png").write_bytes(imgs[0].read_bytes())

        output_zip = tmpdir / "output.zip"
        result = pack_output(loaded, trans_dir, output_zip)

        assert result.exists()
        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert "page_001.png" in names
            assert "page_002.png" not in names


def test_pack_output_falls_back_to_original_when_translation_missing():
    """0.4 回归测试：某页翻译失败、trans_dir 里没有它，但原图还在——打包时必须用
    原图顶上，而不是让这一页从产物里消失（旧行为：直接跳过，产物比原作少页）。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        orig_dir = tmpdir / "orig"
        orig_dir.mkdir()
        orig_imgs = create_test_images(orig_dir, count=2)

        loaded = LoadedInput(
            kind="zip",
            input_path=tmpdir / "in.zip",
            work_dir=tmpdir,
            rel_names=["page_001.png", "page_002.png"],
            pages=orig_imgs,  # 两页的原始路径都在，第二页从未成功翻译
        )

        trans_dir = tmpdir / "translated"
        trans_dir.mkdir()
        # 只有第一页翻译成功；第二页失败，trans_dir 里没有它
        (trans_dir / "page_001.png").write_bytes(orig_imgs[0].read_bytes())

        logs = []
        output_zip = tmpdir / "output.zip"
        result = pack_output(loaded, trans_dir, output_zip, log=logs.append)

        assert result.exists()
        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert "page_001.png" in names
            assert "page_002.png" in names  # 用原图顶上了，不再从产物里消失

        assert any("page_002.png" in m and "原图" in m for m in logs)
        assert not any("page_001.png" in m for m in logs)  # 成功的页不该被当成回退


def test_pack_output_folder_falls_back_to_original():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        orig_dir = tmpdir / "orig"
        orig_dir.mkdir()
        orig_imgs = create_test_images(orig_dir, count=2)

        loaded = LoadedInput(
            kind="folder", input_path=tmpdir, work_dir=tmpdir,
            rel_names=["page_001.png", "page_002.png"], pages=orig_imgs,
        )
        trans_dir = tmpdir / "translated"
        trans_dir.mkdir()
        (trans_dir / "page_001.png").write_bytes(orig_imgs[0].read_bytes())

        output_dir = tmpdir / "output"
        result = pack_output(loaded, trans_dir, output_dir)
        assert (result / "page_001.png").exists()
        assert (result / "page_002.png").exists()  # 回退原图


def test_pack_output_pdf_falls_back_to_original():
    """_pack_pdf 是独立的重打包路径（不复用 pack_output 主体的分支），单独验证回退。"""
    pytest.importorskip("fitz")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        orig_dir = tmpdir / "src"
        orig_dir.mkdir()
        orig_imgs = create_test_images(orig_dir, count=2)

        loaded = LoadedInput(
            kind="pdf", input_path=tmpdir / "in.pdf", work_dir=orig_dir,
            rel_names=["page_001.png", "page_002.png"], pages=orig_imgs,
        )
        trans_dir = tmpdir / "translated"
        trans_dir.mkdir()
        (trans_dir / "page_001.png").write_bytes(orig_imgs[0].read_bytes())

        output_pdf = tmpdir / "output.pdf"
        result = pack_output(loaded, trans_dir, output_pdf, pdf_dpi=150)

        assert result.exists()
        import fitz

        with fitz.open(result) as doc:
            assert doc.page_count == 2  # 第二页没丢，用原图撑起来了


def test_pack_output_epub_falls_back_to_original_without_self_copy_error():
    """epub 回退时原图本来就在 work_dir（staging）里，不能对同一个文件 copy 到自己，
    否则会抛 SameFileError；这里验证不崩。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        staging = tmpdir / "staging"
        staging.mkdir()
        orig_imgs = create_test_images(staging, count=2)

        loaded = LoadedInput(
            kind="epub", input_path=tmpdir / "in.epub", work_dir=staging,
            rel_names=["page_001.png", "page_002.png"], pages=orig_imgs,
        )
        trans_dir = tmpdir / "translated"
        trans_dir.mkdir()
        (trans_dir / "page_001.png").write_bytes(orig_imgs[0].read_bytes())
        (staging / "mimetype").write_text("application/epub+zip")

        output_epub = tmpdir / "output.epub"
        result = pack_output(loaded, trans_dir, output_epub)  # 不应抛异常

        assert result.exists()
        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert "page_001.png" in names
            assert "page_002.png" in names
