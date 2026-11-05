"""core.gpu_components：解包规则、状态判定、下载（续传/校验/镜像回退/取消）、安装与激活。

不碰真实网络：起一个只监听 127.0.0.1 的小 HTTP 服务（支持 Range），wheel 是测试里现做的 zip。
"""
from __future__ import annotations

import hashlib
import http.server
import io
import os
import sys
import threading
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from comic_gen.core import gpu_components as gc

# ------------------------------------------------------------------ 小工具


def _make_wheel(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class _Server:
    """按路径提供字节内容，支持 Range；可配置某个路径返回 500 或截断，模拟网络故障。"""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.fail_paths: set[str] = set()
        self.truncate_once: dict[str, int] = {}
        self.range_requests: list[tuple[str, str]] = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path in outer.fail_paths or self.path not in outer.files:
                    self.send_response(500 if self.path in outer.fail_paths else 404)
                    self.end_headers()
                    return
                data = outer.files[self.path]
                rng = self.headers.get("Range")
                start = 0
                if rng:
                    outer.range_requests.append((self.path, rng))
                    start = int(rng.split("=")[1].split("-")[0])
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                else:
                    self.send_response(200)
                body = data[start:]
                cut = outer.truncate_once.pop(self.path, None)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if cut is not None:
                    self.wfile.write(body[:cut])  # 发一半就断开，客户端收到的少于 Content-Length
                    self.close_connection = True
                    return
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def server():
    s = _Server()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(gc.time, "sleep", lambda s: None)


def _spec(server: _Server, key: str, files: dict[str, bytes], **kw) -> gc.WheelSpec:
    data = _make_wheel(files)
    server.files[f"/{key}.whl"] = data
    base = dict(
        key=key, label=key, url=f"{server.base}/{key}.whl", mirrors=(),
        sha256=hashlib.sha256(data).hexdigest(), size=len(data), file_name=f"{key}.whl",
        include=(f"{key}/",),
    )
    base.update(kw)
    return gc.WheelSpec(**base)


def _noop(*_):
    pass


# ------------------------------------------------------------------ 解包规则（纯函数）


def test_select_members_torch_rules_drop_static_libs_headers_and_dirs():
    spec = next(w for w in gc.WHEELS if w.key == "torch")
    names = [
        "torch/", "torch/__init__.py", "torch/lib/torch_cuda.dll", "torch/lib/dnnl.lib",
        "torch/include/ATen/ATen.h", "torch/share/cmake/Torch/TorchConfig.cmake",
        "torch-2.6.0+cu124.dist-info/METADATA", "functorch/__init__.py", "torchgen/model.py",
    ]
    got = dict(gc.select_members(spec, names))
    assert got == {
        "torch/__init__.py": "site/torch/__init__.py",
        "torch/lib/torch_cuda.dll": "site/torch/lib/torch_cuda.dll",
        "torch-2.6.0+cu124.dist-info/METADATA": "site/torch-2.6.0+cu124.dist-info/METADATA",
    }


def test_select_members_llama_strips_prefix_into_llama_lib():
    spec = next(w for w in gc.WHEELS if w.key == "llama")
    got = dict(gc.select_members(spec, [
        "llama_cpp/lib/ggml-cuda.dll", "llama_cpp/lib/ggml-cuda.lib", "llama_cpp/llama.py",
    ]))
    assert got == {"llama_cpp/lib/ggml-cuda.dll": "llama_lib/ggml-cuda.dll"}


def test_select_members_rejects_zip_slip():
    spec = gc.WheelSpec(key="x", label="x", url="", mirrors=(), sha256="", size=0, file_name="x",
                        include=("x/",))
    assert gc.select_members(spec, ["x/../../evil.dll", "x/ok.py"]) == [("x/ok.py", "site/x/ok.py")]


def test_url_candidates_official_first_then_mirrors():
    for w in gc.WHEELS:
        c = gc.url_candidates(w)
        assert c[0] == w.url and len(c) == 1 + len(w.mirrors)


def test_pinned_specs_are_consistent():
    """钉死的常量自洽：哈希 64 位十六进制、文件名唯一、必需文件都能被解包规则产出。"""
    assert len({w.file_name for w in gc.WHEELS}) == len(gc.WHEELS)
    for w in gc.WHEELS:
        assert len(w.sha256) == 64 and int(w.sha256, 16) >= 0
        assert w.size > 0
    dests = {w.dest for w in gc.WHEELS}
    for f in gc.REQUIRED_FILES:
        assert f.split("/")[0] in dests


# ------------------------------------------------------------------ 状态判定


def _fake_ready_pack(base: Path):
    d = gc.pack_dir(base)
    for f in gc.REQUIRED_FILES:
        (d / f).parent.mkdir(parents=True, exist_ok=True)
        (d / f).write_bytes(b"x")
    (d / gc.MARKER_NAME).write_text(f'{{"pack_id": "{gc.PACK_ID}"}}', encoding="utf-8")
    return d


def test_pack_status_missing_ready_broken(tmp_path):
    assert gc.pack_status(tmp_path).state == gc.STATE_MISSING
    d = _fake_ready_pack(tmp_path)
    assert gc.pack_status(tmp_path).is_ready
    (d / "llama_lib/ggml-cuda.dll").unlink()
    st = gc.pack_status(tmp_path)
    assert st.state == gc.STATE_BROKEN and "ggml-cuda" in st.reason


def test_pack_status_old_pack_id_is_broken(tmp_path):
    d = _fake_ready_pack(tmp_path)
    (d / gc.MARKER_NAME).write_text('{"pack_id": "gpu-cu118-old"}', encoding="utf-8")
    st = gc.pack_status(tmp_path)
    assert st.state == gc.STATE_BROKEN and "版本不符" in st.reason


def test_pack_status_without_marker_is_broken(tmp_path):
    """解包到一半被杀掉的目录不能被当成已安装。"""
    d = _fake_ready_pack(tmp_path)
    (d / gc.MARKER_NAME).unlink()
    assert gc.pack_status(tmp_path).state == gc.STATE_BROKEN


# ------------------------------------------------------------------ 下载


def test_download_resumes_from_part_file(server, tmp_path):
    spec = _spec(server, "torch", {"torch/a.bin": os.urandom(300_000)})
    data = server.files["/torch.whl"]
    (tmp_path / (spec.file_name + ".part")).write_bytes(data[:100_000])
    out = gc.download_wheel(spec, tmp_path, _noop, _noop, threading.Event())
    assert out.read_bytes() == data
    assert server.range_requests == [("/torch.whl", "bytes=100000-")]


def test_download_recovers_from_mid_stream_disconnect(server, tmp_path):
    spec = _spec(server, "torch", {"torch/a.bin": os.urandom(300_000)})
    server.truncate_once["/torch.whl"] = 50_000
    logs: list[str] = []
    out = gc.download_wheel(spec, tmp_path, _noop, logs.append, threading.Event())
    assert hashlib.sha256(out.read_bytes()).hexdigest() == spec.sha256
    assert any("下载中断" in m for m in logs)
    assert server.range_requests and server.range_requests[0][1].startswith("bytes=")


def test_download_falls_back_to_mirror_when_official_fails(server, tmp_path):
    spec = _spec(server, "torch", {"torch/a.bin": b"hello" * 1000})
    server.files["/mirror/torch.whl"] = server.files["/torch.whl"]
    server.fail_paths.add("/torch.whl")
    spec = replace(spec, mirrors=(f"{server.base}/mirror/torch.whl",))
    logs: list[str] = []
    gc.download_wheel(spec, tmp_path, _noop, logs.append, threading.Event())
    assert any("镜像" in m for m in logs)


def test_download_hash_mismatch_blocks_and_deletes(server, tmp_path):
    spec = replace(_spec(server, "torch", {"torch/a.bin": b"x" * 5000}), sha256="0" * 64)
    with pytest.raises(gc.InstallError) as ei:
        gc.download_wheel(spec, tmp_path, _noop, _noop, threading.Event())
    assert "SHA256" in ei.value.technical
    assert not (tmp_path / spec.file_name).exists()
    assert not (tmp_path / (spec.file_name + ".part")).exists()


def test_download_cancel_keeps_part_for_resume(server, tmp_path):
    spec = _spec(server, "torch", {"torch/a.bin": os.urandom(3_000_000)})
    cancel = threading.Event()

    def progress(stage, done, total):
        if done > 0:
            cancel.set()

    with pytest.raises(gc.Cancelled):
        gc.download_wheel(spec, tmp_path, progress, _noop, cancel)
    assert (tmp_path / (spec.file_name + ".part")).is_file()


# ------------------------------------------------------------------ 安装与激活


def _fake_wheels(server, monkeypatch):
    torch = _spec(server, "torch", {
        "torch/__init__.py": b"GPU_BUILD = True\n",
        "torch/lib/torch_cuda.dll": b"dll", "torch/lib/cudart64_12.dll": b"dll",
        "torch/lib/cublas64_12.dll": b"dll", "torch/lib/big.lib": b"static",
    }, include=("torch/",), exclude_suffixes=(".lib",))
    tv = _spec(server, "torchvision", {"torchvision/__init__.py": b""})
    llama = _spec(server, "llama", {
        "llama_cpp/lib/llama.dll": b"d", "llama_cpp/lib/ggml.dll": b"d",
        "llama_cpp/lib/ggml-cuda.dll": b"d", "llama_cpp/llama.py": b"",
    }, include=("llama_cpp/lib/",), strip="llama_cpp/lib/", dest="llama_lib")
    monkeypatch.setattr(gc, "WHEELS", (torch, tv, llama))
    monkeypatch.setattr(gc, "EXTRACTED_BYTES", 1000)
    monkeypatch.setattr(gc, "_active_dir", None)


def test_install_end_to_end_then_activate(server, tmp_path, monkeypatch):
    _fake_wheels(server, monkeypatch)
    st = gc.install(tmp_path)
    assert st.is_ready
    d = gc.pack_dir(tmp_path)
    assert not (d / "site/torch/lib/big.lib").exists()      # .lib 被过滤
    assert not (d.parent / (d.name + ".tmp")).exists()      # 临时目录已改名就位
    assert not (gc.downloads_dir(tmp_path) / "torch.whl").exists()  # 下载包装完即删

    # 激活：torch 必须从组件目录导入（不是测试环境里装的那份）
    monkeypatch.setattr(sys, "path", list(sys.path))
    saved = {k: v for k, v in sys.modules.items() if k == "torch" or k.startswith("torch.")}
    for k in saved:
        monkeypatch.delitem(sys.modules, k)
    monkeypatch.delenv("LLAMA_CPP_LIB_PATH", raising=False)
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    st = gc.activate(tmp_path)
    try:
        assert st.is_ready and gc.active_dir() == d
        assert os.environ["LLAMA_CPP_LIB_PATH"] == str(d / "llama_lib")
        import torch as fake_torch
        assert getattr(fake_torch, "GPU_BUILD", False) is True
        from comic_gen.pipeline._cuda_runtime import torch_lib_dir
        assert torch_lib_dir() == d / "site" / "torch" / "lib"
    finally:
        sys.modules.pop("torch", None)
        sys.modules.update(saved)


def test_activate_refuses_when_torch_already_imported(tmp_path, monkeypatch):
    _fake_ready_pack(tmp_path)
    monkeypatch.setattr(gc, "_active_dir", None)
    monkeypatch.setitem(sys.modules, "torch", object())
    monkeypatch.setattr(sys, "path", list(sys.path))
    st = gc.activate(tmp_path)
    assert not st.is_ready and "torch 已在激活前被导入" in st.reason
    assert gc.active_dir() is None


def test_activate_missing_pack_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, "_active_dir", None)
    before = list(sys.path)
    assert gc.activate(tmp_path).state == gc.STATE_MISSING
    assert sys.path == before


def test_remove_while_active_is_deferred_to_next_start(tmp_path, monkeypatch):
    d = _fake_ready_pack(tmp_path)
    monkeypatch.setattr(gc, "_active_dir", d)
    assert gc.schedule_removal(tmp_path) is False
    assert d.exists()
    monkeypatch.setattr(gc, "_active_dir", None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert gc.activate(tmp_path).state == gc.STATE_MISSING   # 启动时先删再判断
    assert not d.exists()


def test_install_refuses_when_disk_too_small(server, tmp_path, monkeypatch):
    _fake_wheels(server, monkeypatch)
    monkeypatch.setattr(gc, "check_disk_space", lambda base=None: (10 ** 12, 10 ** 9))
    with pytest.raises(gc.InstallError, match="磁盘空间不足"):
        gc.install(tmp_path)
