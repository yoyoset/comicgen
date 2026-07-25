"""core.model_locator 的离线测试：构造两种真实布局，确认都能命中。

背景：同机 subgen 把 GGUF 平铺在 HF_HOME 根下，comic-gen 自己下的在 hub/models--*/snapshots/ 下。
两种都得认，否则权重明明在盘上还会去联网（用户实测崩在 SSL UNEXPECTED_EOF）。
"""
import os
import time

import pytest

from comic_gen.core import model_locator
from comic_gen.core.model_locator import clear_cache, find_local_gguf, gguf_search_roots


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
