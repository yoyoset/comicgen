"""Tests for core.utils (offline, no models/network)."""
import pytest
from comic_gen.core.utils import natural_sort_key, save_json, load_json
import json
from pathlib import Path
import tempfile


def test_natural_sort_key():
    """Test natural sorting of filenames."""
    files = ["page_10.png", "page_1.png", "page_2.png", "page_20.png"]
    sorted_files = sorted(files, key=natural_sort_key)
    assert sorted_files == ["page_1.png", "page_2.png", "page_10.png", "page_20.png"]


def test_natural_sort_key_mixed():
    """Test natural sorting with mixed text and numbers."""
    files = ["ch10_p1.png", "ch2_p1.png", "ch1_p10.png", "ch1_p2.png"]
    sorted_files = sorted(files, key=natural_sort_key)
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


def test_load_json_not_exists():
    """Test loading non-existent JSON returns empty dict."""
    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = Path(tmpdir) / "nonexistent.json"
        result = load_json(json_path)
        assert result == {}
