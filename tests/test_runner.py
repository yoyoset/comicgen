"""Tests for pipeline.runner safety checks (offline)."""
import pytest
from pathlib import Path
import tempfile
from comic_gen.pipeline.runner import Runner
from comic_gen.core.config import Config


def test_check_output_safe_same_path():
    """Test that output == input is rejected."""
    cfg = Config()
    runner = Runner(cfg)

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / "input.zip"
        output_path = Path(tmpdir) / "input.zip"

        with pytest.raises(ValueError, match="覆盖原图"):
            runner._check_output_safe(input_path, output_path)


def test_check_output_safe_output_in_input():
    """Test that output inside input directory is rejected."""
    cfg = Config()
    runner = Runner(cfg)

    with tempfile.TemporaryDirectory() as tmpdir:
        input_dir = Path(tmpdir) / "input"
        input_dir.mkdir()
        output_path = input_dir / "output.zip"

        with pytest.raises(ValueError, match="输出目录位于输入目录内"):
            runner._check_output_safe(input_dir, output_path)


def test_check_output_safe_valid_paths():
    """Test that valid paths pass the check."""
    cfg = Config()
    runner = Runner(cfg)

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / "input.zip"
        output_path = Path(tmpdir) / "output.zip"

        # Should not raise
        runner._check_output_safe(input_path, output_path)


def test_check_output_safe_different_dirs():
    """Test that output in different directory is safe."""
    cfg = Config()
    runner = Runner(cfg)

    with tempfile.TemporaryDirectory() as tmpdir:
        input_dir = Path(tmpdir) / "input_folder"
        output_dir = Path(tmpdir) / "output_folder"
        input_dir.mkdir()
        output_dir.mkdir()

        # Should not raise
        runner._check_output_safe(input_dir, output_dir)
