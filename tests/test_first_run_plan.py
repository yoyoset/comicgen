"""首次运行弹窗用到的纯函数：估算下载量。离线，用 tmp_path 造假文件驱动。"""
from comic_gen.core.model_locator import plan_download_bytes


def test_plan_download_bytes_sums_layout_file_only():
    # "llm" 的 size_bytes 故意是 0（见 manifest.py/test_manifest.py 的注释——这个 key
    # 允许自由换绑其它模型，不拿具体某个模型的字节数卡死），对总量贡献为 0。
    total = plan_download_bytes(["sakura", "llm"])
    assert total == 8_180_361_440


def test_plan_download_bytes_excludes_layout_dir():
    """detect/ocr_ja/ocr_other 没有精确总大小，不该被计入——避免精确数字和估算数字混着加。"""
    assert plan_download_bytes(["detect", "ocr_ja", "ocr_other"]) == 0


def test_plan_download_bytes_mixed():
    total = plan_download_bytes(["sakura", "detect"])
    assert total == 8_180_361_440


def test_plan_download_bytes_empty_list():
    assert plan_download_bytes([]) == 0


def test_plan_download_bytes_ignores_unknown_key():
    assert plan_download_bytes(["not_a_real_key"]) == 0
