"""core.model_locator 的离线测试：构造两种真实布局，确认都能命中。

背景：同机 subgen 把 GGUF 平铺在 HF_HOME 根下，comic-gen 自己下的在 hub/models--*/snapshots/ 下。
两种都得认，否则权重明明在盘上还会去联网（用户实测崩在 SSL UNEXPECTED_EOF）。
"""
import os
import time
from pathlib import Path

import pytest

from comic_gen.core import model_locator
from comic_gen.core.model_locator import (
    STATUS_INCOMPLETE,
    STATUS_MISSING,
    STATUS_PATH_BROKEN,
    STATUS_READY,
    STATUS_SIZE_MISMATCH,
    ModelStatus,
    clear_cache,
    find_duplicates,
    find_local_gguf,
    gguf_search_roots,
    resolve_model_path,
    resolve_status,
    scan_all_status,
    status_of,
    status_text,
    trash_destination,
    validate_candidate,
)


@pytest.fixture
def hf_root(tmp_path, monkeypatch):
    """把 HF_HOME 指到临时目录，并清掉搜索缓存（否则会拿到上一个用例的结果）。"""
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    clear_cache()
    yield tmp_path
    clear_cache()


def test_finds_flat_file(hf_root):
    """subgen 布局：直接平铺在根目录下。"""
    f = hf_root / "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
    f.write_bytes(b"x")
    assert find_local_gguf("Qwen3-4B-Instruct-2507-Q4_K_M.gguf") == f


def test_finds_hf_snapshot_layout(hf_root):
    """comic-gen 自己下载的布局：hub/models--org--name/snapshots/<sha>/。"""
    snap = hf_root / "hub" / "models--SakuraLLM--Sakura-7B" / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    f = snap / "sakura.gguf"
    f.write_bytes(b"x")
    assert find_local_gguf("sakura.gguf") == f


def test_ignores_blobs_and_incomplete(hf_root):
    """blobs 里是哈希名，.incomplete 是下载残留，都不能被当成可用权重。"""
    blobs = hf_root / "hub" / "models--a--b" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "model.gguf").write_bytes(b"x")
    (hf_root / "model.gguf.incomplete").write_bytes(b"x")
    assert find_local_gguf("model.gguf") is None


def test_picks_newest_on_duplicate(hf_root):
    """同名多份（hub 一份 + 平铺一份）取修改时间最新的，而不是遍历顺序里的随便一个。"""
    old = hf_root / "hub" / "models--a--b" / "snapshots" / "s1"
    old.mkdir(parents=True)
    old_f = old / "dup.gguf"
    old_f.write_bytes(b"x")
    os.utime(old_f, (time.time() - 10_000, time.time() - 10_000))

    new_f = hf_root / "dup.gguf"
    new_f.write_bytes(b"x")

    assert find_local_gguf("dup.gguf") == new_f


def test_missing_returns_none_not_raises(hf_root):
    """找不到必须返回 None，让调用方走联网兜底，而不是直接崩。"""
    assert find_local_gguf("nope.gguf") is None
    assert find_local_gguf("") is None


def test_search_roots_include_hf_home_root(hf_root):
    """必须包含 HF_HOME 根本身，不能只看 hub/——subgen 的平铺文件就在根下。"""
    (hf_root / "hub").mkdir()
    roots = gguf_search_roots()
    assert hf_root in roots
    assert hf_root / "hub" in roots


def test_explicit_model_dir_wins(tmp_path, monkeypatch):
    """设置页显式指定的模型目录优先于环境变量。"""
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("HF_HOME", str(other))
    clear_cache()
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    assert gguf_search_roots(str(explicit))[0] == explicit
    clear_cache()


def test_cache_avoids_rescan(hf_root, monkeypatch):
    """结果缓存生效：第二次调用不应再遍历磁盘（共享根很大，重复全扫会明显变慢）。"""
    f = hf_root / "cached.gguf"
    f.write_bytes(b"x")
    assert find_local_gguf("cached.gguf") == f

    calls = []
    real_walk = model_locator.os.walk
    monkeypatch.setattr(model_locator.os, "walk",
                        lambda *a, **k: (calls.append(1), real_walk(*a, **k))[1])
    assert find_local_gguf("cached.gguf") == f
    assert calls == []


# ==================================================================== status_of（纯函数，表驱动）

def test_status_of_ready_file():
    st = status_of(Path("x.gguf"), is_binding=False, layout="file", exists=True,
                    size=100, expected_size=100)
    assert st == ModelStatus(STATUS_READY, path=Path("x.gguf"), size=100, expected_size=100)


def test_status_of_size_mismatch():
    st = status_of(Path("x.gguf"), is_binding=False, layout="file", exists=True,
                    size=50, expected_size=100)
    assert st.state == STATUS_SIZE_MISMATCH
    assert st.size == 50 and st.expected_size == 100


def test_status_of_missing_when_no_binding():
    st = status_of(None, is_binding=False, layout="file", exists=False)
    assert st.state == STATUS_MISSING


def test_status_of_path_broken_when_binding_gone():
    """绑定过的文件消失了必须报"路径失效"，不能悄悄降级成"未安装"——那会让用户以为从没配置过。"""
    st = status_of(Path("gone.gguf"), is_binding=True, layout="file", exists=False)
    assert st.state == STATUS_PATH_BROKEN


def test_status_of_dir_incomplete():
    st = status_of(Path("d"), is_binding=False, layout="dir", exists=True,
                    missing_members=("config.json",))
    assert st.state == STATUS_INCOMPLETE
    assert st.missing_members == ("config.json",)


def test_status_of_dir_ready_when_no_missing():
    st = status_of(Path("d"), is_binding=False, layout="dir", exists=True, missing_members=())
    assert st.state == STATUS_READY


def test_is_ready_property():
    assert ModelStatus(STATUS_READY).is_ready
    assert not ModelStatus(STATUS_MISSING).is_ready


# ==================================================================== resolve_status（真实磁盘 I/O）

def test_resolve_status_file_ready_via_search(hf_root, monkeypatch):
    """未绑定时按 manifest 声明的文件名在搜索根里找，找到且大小吻合就是 ready。"""
    from dataclasses import replace
    f = hf_root / "sakura-7b-qwen2.5-v1.0-iq4xs.gguf"
    f.write_bytes(b"x" * 10)
    patched = dict(model_locator.MODEL_MANIFEST)
    patched["sakura"] = replace(patched["sakura"], size_bytes=10)
    monkeypatch.setattr(model_locator, "MODEL_MANIFEST", patched)
    st = resolve_status("sakura", model_dir=None)
    assert st.state == STATUS_READY
    assert st.path == f


def test_resolve_status_binding_overrides_search(hf_root, tmp_path):
    """有显式绑定时必须用绑定的文件，即使搜索能找到别的同名文件也不能用那个。"""
    (hf_root / "sakura-7b-qwen2.5-v1.0-iq4xs.gguf").write_bytes(b"wrong one")
    bound_file = tmp_path / "my_sakura.gguf"
    bound_file.write_bytes(b"correct")
    st = resolve_status("sakura", bindings={"sakura": str(bound_file)})
    assert st.path == bound_file


def test_resolve_status_binding_path_broken_does_not_fallback(hf_root, tmp_path):
    """绑定指向的文件不存在时必须报 path_broken，绝不能静默回落去搜别的同名文件顶替。"""
    (hf_root / "sakura-7b-qwen2.5-v1.0-iq4xs.gguf").write_bytes(b"decoy")
    gone = tmp_path / "gone.gguf"
    st = resolve_status("sakura", bindings={"sakura": str(gone)})
    assert st.state == STATUS_PATH_BROKEN


def test_resolve_status_dir_missing(tmp_path, monkeypatch):
    """必须隔离 Path.home()：开发机上可能真的装过 EasyOCR，不隔离会在本机测出假阳性。"""
    monkeypatch.setattr(model_locator.Path, "home", staticmethod(lambda: tmp_path))
    st = resolve_status("ocr_other", model_dir=str(tmp_path / "nowhere"))
    assert st.state == STATUS_MISSING


def test_resolve_model_path_none_when_not_ready(hf_root):
    """必须用 hf_root（隔离了 HF_HOME/HF_HUB_CACHE）而非裸 tmp_path——
    否则真机上 HF_HOME 指向的真实缓存会被一并搜到，把这个"未就绪"用例测成假阴性。"""
    assert resolve_model_path("sakura", model_dir=str(hf_root)) is None


# ==================================================================== status_text / 查重 / 回收站

def test_status_text_ready_mentions_size():
    text, color = status_text(ModelStatus(STATUS_READY, size=1024 * 1024 * 1024))
    assert "已就绪" in text and "GB" in text


def test_status_text_path_broken_wording():
    text, _ = status_text(ModelStatus(STATUS_PATH_BROKEN))
    assert "不在原位置" in text


def test_find_duplicates_returns_all_copies(hf_root):
    flat = hf_root / "sakura-7b-qwen2.5-v1.0-iq4xs.gguf"
    flat.write_bytes(b"x")
    snap = hf_root / "hub" / "models--a--b" / "snapshots" / "s1"
    snap.mkdir(parents=True)
    (snap / "sakura-7b-qwen2.5-v1.0-iq4xs.gguf").write_bytes(b"x")
    dups = find_duplicates("sakura")
    assert len(dups) == 2


def test_trash_destination_same_volume(tmp_path):
    """_trash 就在原文件同目录下，结构上必然同卷（改名而非跨盘复制）。"""
    f = tmp_path / "model.gguf"
    f.write_bytes(b"x")
    dest = trash_destination(f)
    assert dest.parent == tmp_path / "_trash"
    assert dest.drive == f.drive


def test_trash_destination_avoids_collision(tmp_path):
    f = tmp_path / "model.gguf"
    f.write_bytes(b"x")
    trash = tmp_path / "_trash"
    trash.mkdir()
    (trash / "model.gguf").write_bytes(b"already here")
    dest = trash_destination(f)
    assert dest.name != "model.gguf"
    assert not dest.exists()


# ==================================================================== scan_all_status / validate_candidate

def test_scan_all_status_covers_all_six_keys(tmp_path):
    from comic_gen.core.manifest import MODEL_MANIFEST
    result = scan_all_status(model_dir=str(tmp_path))
    assert set(result) == set(MODEL_MANIFEST)
    assert all(isinstance(v, ModelStatus) for v in result.values())


def test_validate_candidate_accepts_correct_gguf(tmp_path, monkeypatch):
    """不用真实模型的几 GB size_bytes——NTFS 上 truncate 到那么大会触发同步清零，实测单个用例要 20 秒。
    换一个体积很小的假 spec 验证同一段校验逻辑。"""
    from dataclasses import replace
    patched = dict(model_locator.MODEL_MANIFEST)
    patched["llm"] = replace(patched["llm"], size_bytes=8)
    monkeypatch.setattr(model_locator, "MODEL_MANIFEST", patched)

    f = tmp_path / "candidate.gguf"
    f.write_bytes(b"GGUF" + b"\x00" * 4)
    assert validate_candidate("llm", str(f)) is None


def test_validate_candidate_rejects_wrong_magic(tmp_path):
    f = tmp_path / "candidate.gguf"
    f.write_bytes(b"NOPE" + b"\x00" * 100)
    msg = validate_candidate("llm", str(f))
    assert msg is not None and "文件头" in msg


def test_validate_candidate_rejects_wrong_size():
    """典型误操作：给 Sakura 那一行选了 Qwen3 的文件——大小对不上，必须在做任何昂贵操作前拒绝。"""
    from comic_gen.core.manifest import MODEL_MANIFEST
    import tempfile
    spec = MODEL_MANIFEST["sakura"]
    with tempfile.NamedTemporaryFile(suffix=".gguf", delete=False) as f:
        f.write(b"GGUF" + b"\x00" * 1000)  # 远小于 spec.size_bytes
        path = f.name
    try:
        msg = validate_candidate("sakura", path)
        assert msg is not None and "大小不对" in msg
    finally:
        Path(path).unlink(missing_ok=True)


def test_validate_candidate_dir_layout_checks_members(tmp_path):
    d = tmp_path / "some_snapshot"
    d.mkdir()
    (d / "config.json").write_text("{}")
    msg = validate_candidate("ocr_ja", str(d))
    assert msg is not None and "不完整" in msg


def test_validate_candidate_rejects_missing_path(tmp_path):
    assert validate_candidate("llm", str(tmp_path / "nope.gguf")) is not None
