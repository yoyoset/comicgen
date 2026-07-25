"""首次运行弹窗用到的纯函数：估算下载量。离线，用 tmp_path 造假文件驱动。"""
from comic_gen.core.model_locator import plan_download_bytes


def test_plan_download_bytes_sums_layout_file_only():
    total = plan_download_bytes(["sakura", "llm"])
    assert total == 4_250_298_208 + 2_497_281_120


def test_plan_download_bytes_excludes_layout_dir():
    """detect/ocr_ja/ocr_other 没有精确总大小，不该被计入——避免精确数字和估算数字混着加。"""
    assert plan_download_bytes(["detect", "ocr_ja", "ocr_other"]) == 0


def test_plan_download_bytes_mixed():
    total = plan_download_bytes(["sakura", "detect"])
    assert total == 4_250_298_208


def test_plan_download_bytes_empty_list():
    assert plan_download_bytes([]) == 0


def test_plan_download_bytes_ignores_unknown_key():
    assert plan_download_bytes(["not_a_real_key"]) == 0
