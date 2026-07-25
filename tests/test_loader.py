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


def test_pack_output_missing_images():
    """Test that missing images are skipped gracefully."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        imgs = create_test_images(tmpdir, count=1)

        loaded = LoadedInput(
            kind="zip",
            input_path=tmpdir / "in.zip",
            work_dir=tmpdir,
            rel_names=["page_001.png", "page_002.png"],  # page_002 doesn't exist
            pages=[],
        )

        trans_dir = tmpdir / "translated"
        trans_dir.mkdir()

        # Only copy page_001
        (trans_dir / "page_001.png").write_bytes(imgs[0].read_bytes())

        output_zip = tmpdir / "output.zip"
        result = pack_output(loaded, trans_dir, output_zip)

        # Should succeed, but only page_001 in output
        assert result.exists()
        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert "page_001.png" in names
            assert "page_002.png" not in names  # Missing file skipped
