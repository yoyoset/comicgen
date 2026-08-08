"""Tests for core.utils (offline, no models/network)."""
import json
import tempfile
from pathlib import Path

import pytest

from comic_gen.core.utils import (
    atomic_save_image,
    atomic_write_text,
    available_space,
    estimate_output_requirement,
    load_json,
    natural_key,
    save_json,
)


def test_natural_key():
    """Test natural sorting of filenames."""
    files = ["page_10.png", "page_1.png", "page_2.png", "page_20.png"]
    sorted_files = sorted(files, key=natural_key)
    assert sorted_files == ["page_1.png", "page_2.png", "page_10.png", "page_20.png"]


def test_natural_key_mixed():
    """Test natural sorting with mixed text and numbers."""
    files = ["ch10_p1.png", "ch2_p1.png", "ch1_p10.png", "ch1_p2.png"]
    sorted_files = sorted(files, key=natural_key)
    assert sorted_files == ["ch1_p2.png", "ch1_p10.png", "ch2_p1.png", "ch10_p1.png"]


def test_save_load_json():
    """Test JSON save and load roundtrip."""
    data = {
        "pages": [
            {"image": "p1.png", "status": "rendered"},
            {"image": "p2.png", "status": "translated"},
        ],
        "input": "/some/path/file.zip",
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = Path(tmpdir) / "test.json"
        save_json(data, json_path)

        assert json_path.exists()

        loaded = load_json(json_path)
        assert loaded == data


def test_save_load_json_empty():
    """Test JSON save and load with minimal data."""
    data = {"pages": []}

    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = Path(tmpdir) / "empty.json"
        save_json(data, json_path)
        loaded = load_json(json_path)
        assert loaded == data


def test_load_json_not_exists_raises():
    """文件不存在时抛错而不是静默返回 {}：静默会让"缓存丢失"伪装成"没有缓存"。

    调用方（runner._load_pages）自己先判存在性再读，语义清楚。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(FileNotFoundError):
            load_json(Path(tmpdir) / "nonexistent.json")


def test_atomic_write_creates_parent_and_writes_complete_content():
    """临时文件 + 原子替换后，目标读到的是完整内容（不是写了一半）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "sub" / "pages.json"
        atomic_write_text(target, '{"pages": [1, 2, 3]}')
        assert target.read_text(encoding="utf-8") == '{"pages": [1, 2, 3]}'
        # 不留下任何临时文件
        assert list(Path(tmpdir).rglob("*.tmp")) == []


def test_atomic_write_overwrites_existing_target():
    """覆盖已有文件时内容完整替换，不存在"新旧混写"。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "job.json"
        atomic_write_text(target, "old")
        atomic_write_text(target, "new-content")
        assert target.read_text(encoding="utf-8") == "new-content"


def test_atomic_write_failure_keeps_original_and_cleans_temp(monkeypatch):
    """替换前任何一步失败（这里模拟 os.replace 抛错），原文件内容必须原样保留，
    临时文件必须被清理——这是"写一半丢全部恢复点"要防住的场景。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "pages.json"
        atomic_write_text(target, "original")

        def _boom(src, dst):
            raise OSError("模拟磁盘写入失败")

        monkeypatch.setattr("comic_gen.core.utils.os.replace", _boom)
        with pytest.raises(OSError):
            atomic_write_text(target, "partial-new")
        monkeypatch.undo()

        assert target.read_text(encoding="utf-8") == "original"
        # 失败路径下临时文件也要清干净，不能往工作目录里堆垃圾
        assert list(Path(tmpdir).iterdir()) == [target]


def test_save_json_uses_atomic_write(tmp_path):
    """save_json 走 atomic_write_text，roundtrip 内容一致且无临时文件残留。"""
    data = {"pages": [{"image": "p1.png", "status": "rendered"}]}
    target = tmp_path / "nested" / "pages.json"
    save_json(data, target)
    assert load_json(target) == data
    assert list(tmp_path.rglob("*.tmp")) == []


def test_estimate_output_requirement_scales_with_input_size(tmp_path):
    """磁盘需求按输入图片实际字节数×系数估算；越大越占，不存在的文件按 0 计。"""
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"x" * 1000)
    b.write_bytes(b"y" * 3000)
    missing = tmp_path / "gone.png"

    req = estimate_output_requirement([a, b, missing])
    # 1000+3000=4000 字节，×2.5 = 10000；缺失文件不拖垮估算
    assert req == 10000


def test_estimate_output_requirement_empty_input_is_zero():
    assert estimate_output_requirement([]) == 0


def test_available_space_returns_positive_bytes(tmp_path):
    free = available_space(tmp_path)
    assert free > 0


def test_atomic_save_image_writes_complete_png(tmp_path):
    """原子写图后产物是完整可读的 PNG，不留临时文件。"""
    from PIL import Image

    img = Image.new("RGB", (8, 8), (255, 0, 0))
    target = tmp_path / "sub" / "page.png"
    atomic_save_image(img, target)
    with Image.open(target) as loaded:
        loaded.load()
        assert loaded.size == (8, 8)
    assert list(tmp_path.rglob("*.tmp")) == []


def test_atomic_save_image_failure_keeps_original_and_cleans_temp(monkeypatch, tmp_path):
    """替换前失败（模拟 os.replace 抛错）时，原图保留、临时文件清理。"""
    from PIL import Image

    target = tmp_path / "page.png"
    atomic_save_image(Image.new("RGB", (4, 4), "white"), target)
    before = target.read_bytes()

    def _boom(src, dst):
        raise OSError("模拟写入失败")

    monkeypatch.setattr("comic_gen.core.utils.os.replace", _boom)
    with pytest.raises(OSError):
        atomic_save_image(Image.new("RGB", (4, 4), "black"), target)
    monkeypatch.undo()

    assert target.read_bytes() == before
    assert list(tmp_path.iterdir()) == [target]
