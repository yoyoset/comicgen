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


# ---- core.first_run_plan：首次运行弹窗"要下什么、多大、放哪"与下载进度旁路估算 ----
import os  # noqa: E402

from comic_gen.core import first_run_plan as frp  # noqa: E402


def test_approx_bytes_prefers_exact_then_parses_approx_string():
    assert frp.approx_bytes("sakura") == 8_180_361_440          # 精确字节数
    assert frp.approx_bytes("detect") == 170_000_000            # "~170MB"
    assert frp.approx_bytes("llm") == 8_400_000_000             # size_bytes=0 → "~8.4GB"
    assert frp.approx_bytes("nope") == 0


def test_total_counts_dir_layout_models_too():
    """旧汇总只算 LAYOUT_FILE（plan_download_bytes），检测/OCR 被漏掉、还写"大小见上表"。"""
    assert frp.total_bytes(["detect", "ocr_ja", "sakura"]) == 170_000_000 + 450_000_000 + 8_180_361_440


def test_default_selection_is_required_plus_sakura():
    picked = {k for k in ("detect", "ocr_ja", "ocr_other", "sakura", "llm", "lama") if frp.default_selected(k)}
    assert picked == {"detect", "ocr_ja", "lama", "sakura"}


def test_human_size_decimal_units():
    assert frp.human_size(8_180_361_440) == "8.2 GB"
    assert frp.human_size(205_803_670) == "206 MB"


def test_watch_target_hf_repo_and_lama(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setenv("TORCH_HOME", str(tmp_path / "torch"))
    d, pat = frp.watch_target("sakura")
    assert d == tmp_path / "hub" / "models--SakuraLLM--Sakura-14B-Qwen3-v1.5-GGUF" / "blobs" and pat == "*"
    d, pat = frp.watch_target("lama")
    assert d == tmp_path / "torch" / "hub" / "checkpoints" and pat == "big-lama.pt*"


def test_watched_bytes_counts_incomplete_and_partial_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setenv("TORCH_HOME", str(tmp_path / "torch"))
    blobs = tmp_path / "hub" / "models--ogkalu--comic-text-and-bubble-detector" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "abc.incomplete").write_bytes(b"x" * 1000)
    (blobs / "def").write_bytes(b"x" * 500)
    assert frp.watched_bytes("detect") == 1500
    ck = tmp_path / "torch" / "hub" / "checkpoints"
    ck.mkdir(parents=True)
    (ck / "big-lama.pt.1234abcd.partial").write_bytes(b"x" * 700)
    (ck / "other.pt").write_bytes(b"x" * 9)
    assert frp.watched_bytes("lama") == 700


def test_watched_bytes_missing_dir_is_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "nothing"))
    assert frp.watched_bytes("sakura") == 0
    assert os.environ["HF_HUB_CACHE"].endswith("nothing")
