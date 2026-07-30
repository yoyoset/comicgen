"""Tests for pipeline.runner safety checks (offline)."""
import pytest
from pathlib import Path
import tempfile
from comic_gen.core.models import Region
from comic_gen.pipeline.runner import Runner, select_drawable_regions
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


def test_select_drawable_regions_keeps_only_translated():
    """有原文有译文 -> 抹字+重绘；有原文无译文 -> 原样保留，不进 drawable。"""
    regions = [
        Region(bbox=[0, 0, 10, 10], source_text="原文A", translated_text="译文A"),
        Region(bbox=[10, 10, 10, 10], source_text="原文B", translated_text=""),  # 漏译
        Region(bbox=[20, 20, 10, 10], source_text="", translated_text=""),  # 无文字区域
    ]
    drawable = select_drawable_regions(regions)
    assert len(drawable) == 1
    assert drawable[0].source_text == "原文A"


def test_select_drawable_regions_empty_translated_never_erased():
    """回归测试：这是 R2 的红绿用例——之前的实现会把这种区域送去抹字。"""
    regions = [Region(bbox=[0, 0, 10, 10], source_text="残留原文", translated_text="")]
    assert select_drawable_regions(regions) == []


def test_select_drawable_regions_all_translated():
    regions = [
        Region(bbox=[0, 0, 10, 10], source_text="A", translated_text="a"),
        Region(bbox=[10, 10, 10, 10], source_text="B", translated_text="b"),
    ]
    assert len(select_drawable_regions(regions)) == 2


def test_select_drawable_regions_whitespace_only_treated_as_empty():
    regions = [Region(bbox=[0, 0, 10, 10], source_text="原文", translated_text="   ")]
    assert select_drawable_regions(regions) == []
