"""GPU 加速组件：安装包只带 CPU 运行框架，GPU 版 torch/torchvision/llama.cpp 首次运行时下载。

为什么这样做（2026-11-05 实测）：带 GPU 的打包目录 5.9G，其中 torch 3.6G（torch_cuda.dll、
cuDNN、cuBLAS 等 CUDA 库约 3.3G）、llama.cpp 的 ggml-cuda.dll 902M；GitHub Release 单文件
上限 2G。"让用户自己装 CUDA"省不掉这 3.3G——pip 装的 torch 自带 CUDA 库、不认系统装的
CUDA。所以照 subgen 首次运行下载 whisper.cpp 的做法：安装包是 CPU 版，GPU 部分按需下载
上游官方 wheel，钉死 URL + SHA256，校验不过就阻断，不留"对不上也继续用"的降级路径。

运行时怎么切换：PyInstaller 6.x 的冻结导入器挂在 sys.path_hooks 上（不是 meta_path 最前面），
所以把组件目录插到 sys.path[0] 就能让 `import torch` 优先命中下载来的 GPU 版——前提是
`activate()` 在任何代码 import torch 之前调用（ui/app.py、app/job_service.py 入口第一件事）。
llama-cpp-python 认环境变量 LLAMA_CPP_LIB_PATH（llama_cpp.py 第 36 行），指向组件里的 cu124
DLL 目录即可；ggml-cuda.dll 依赖的 cudart64_12/cublas64_12 由 _cuda_runtime 指向 GPU 版
torch/lib 解决（同一套 CUDA 12.4 运行库，不再单独下载）。

本模块不依赖 Qt，下载/校验/解包/激活都是普通函数，进度与取消通过回调/Event 传入。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .paths import app_dir

# 组件包标识：换任何一个 wheel 都要改这里，老目录会被判定为"版本不符"而提示重装。
PACK_ID = "gpu-cu124-torch2.6.0-tv0.21.0-llama0.3.34"
PACK_DIR_NAME = "gpu-cu124"
MARKER_NAME = "installed.json"
REMOVE_PENDING_NAME = "REMOVE_PENDING"

# 解包后的磁盘占用（字节），用于下载前的空间检查。2026-11-05 用 select_members 的过滤规则
# 对三个真实 wheel 求和：torch 3,810,318,591（2028 个文件，整包 4.65G 里 .lib/头文件被滤掉）
# + torchvision 15,790,457 + llama 955,309,568（6 个 DLL）。上游 wheel 不变它就不变。
EXTRACTED_BYTES = 4_781_418_616


@dataclass(frozen=True)
class WheelSpec:
    key: str
    label: str
    url: str
    mirrors: tuple[str, ...]
    sha256: str
    size: int
    file_name: str
    # 解包规则：只取 include 前缀下的条目，排除 exclude 前缀/后缀；strip 前缀后落到 dest 子目录。
    include: tuple[str, ...]
    exclude_prefixes: tuple[str, ...] = ()
    exclude_suffixes: tuple[str, ...] = ()
    strip: str = ""
    dest: str = "site"


# 哈希来源：PyTorch 官方 index 页面链接上的 #sha256= 片段、GitHub Release API 的 digest 字段；
# 2026-11-05 完整下载后本地 sha256sum 复核一致。镜像（阿里云 pytorch-wheels、gh-proxy 反代）
# 只在官方源失败后才用，最终都要过同一个哈希。以后升级：重新下载算真实哈希，不要凭记忆填。
WHEELS: tuple[WheelSpec, ...] = (
    WheelSpec(
        key="torch",
        label="PyTorch GPU 版（CUDA 12.4）",
        url="https://download-r2.pytorch.org/whl/cu124/torch-2.6.0%2Bcu124-cp313-cp313-win_amd64.whl",
        mirrors=("https://mirrors.aliyun.com/pytorch-wheels/cu124/torch-2.6.0+cu124-cp313-cp313-win_amd64.whl",),
        sha256="519330eef09534acad8110b6f423d2fe58c1d8e9ada999ed077a637a0021f908",
        size=2_532_308_219,
        file_name="torch-2.6.0+cu124-cp313-cp313-win_amd64.whl",
        include=("torch/", "torch-2.6.0+cu124.dist-info/"),
        # .lib 是 C++ 链接用的导入库/静态库（dnnl.lib 一个就 624M），运行时用不到；
        # include/、share/ 是编译 C++ 扩展用的头文件与 cmake 配置。
        exclude_prefixes=("torch/include/", "torch/share/"),
        exclude_suffixes=(".lib",),
    ),
    WheelSpec(
        key="torchvision",
        label="torchvision GPU 版",
        url="https://download-r2.pytorch.org/whl/cu124/torchvision-0.21.0%2Bcu124-cp313-cp313-win_amd64.whl",
        mirrors=("https://mirrors.aliyun.com/pytorch-wheels/cu124/torchvision-0.21.0+cu124-cp313-cp313-win_amd64.whl",),
        sha256="8fcf55321b206de70ff8e01c884fa42e57a60b1cb749341b96e0f22c8a7c9ec7",
        size=6_141_095,
        file_name="torchvision-0.21.0+cu124-cp313-cp313-win_amd64.whl",
        include=("torchvision/", "torchvision-0.21.0+cu124.dist-info/"),
        exclude_suffixes=(".lib",),
    ),
    WheelSpec(
        key="llama",
        label="llama.cpp GPU 版（本地翻译引擎）",
        url="https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.34-cu124/"
            "llama_cpp_python-0.3.34-py3-none-win_amd64.whl",
        mirrors=("https://gh-proxy.com/https://github.com/abetlen/llama-cpp-python/releases/download/"
                 "v0.3.34-cu124/llama_cpp_python-0.3.34-py3-none-win_amd64.whl",),
        sha256="d4655623f6256316bd66981d06dd35f2d54bccfd35b4b62ca6665ded2d237a3d",
        size=536_546_937,
        file_name="llama_cpp_python-0.3.34-cu124-py3-none-win_amd64.whl",
        include=("llama_cpp/lib/",),
        exclude_suffixes=(".lib",),
        strip="llama_cpp/lib/",
        dest="llama_lib",
    ),
)

# 安装完成后必须存在的文件：激活前逐个确认，缺一个就当损坏处理，不去半激活。
REQUIRED_FILES = (
    "site/torch/__init__.py",
    "site/torch/lib/torch_cuda.dll",
    "site/torch/lib/cudart64_12.dll",
    "site/torch/lib/cublas64_12.dll",
    "site/torchvision/__init__.py",
    "llama_lib/llama.dll",
    "llama_lib/ggml.dll",
    "llama_lib/ggml-cuda.dll",
)


def total_download_bytes() -> int:
    return sum(w.size for w in WHEELS)


# ------------------------------------------------------------------ 路径与状态

def components_root(base: Path | None = None) -> Path:
    return (base or app_dir()) / "components"


def pack_dir(base: Path | None = None) -> Path:
    return components_root(base) / PACK_DIR_NAME


def downloads_dir(base: Path | None = None) -> Path:
    return components_root(base) / "_downloads"


STATE_MISSING = "missing"
STATE_READY = "ready"
STATE_BROKEN = "broken"


@dataclass
class PackStatus:
    state: str
    path: Path
    reason: str = ""

    @property
    def is_ready(self) -> bool:
        return self.state == STATE_READY


def pack_status(base: Path | None = None) -> PackStatus:
    """目录不存在 = missing；标记文件/版本/必需文件任一不对 = broken（需要重装）。"""
    d = pack_dir(base)
    if not d.exists():
        return PackStatus(STATE_MISSING, d)
    marker = d / MARKER_NAME
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return PackStatus(STATE_BROKEN, d, "缺少安装记录（上次安装可能没有完成）")
    if info.get("pack_id") != PACK_ID:
        return PackStatus(STATE_BROKEN, d, f"组件版本不符（已装 {info.get('pack_id')}，需要 {PACK_ID}）")
    missing = [f for f in REQUIRED_FILES if not (d / f).is_file()]
    if missing:
        return PackStatus(STATE_BROKEN, d, f"文件缺失：{missing[0]} 等 {len(missing)} 个")
    return PackStatus(STATE_READY, d)


def has_nvidia_driver() -> bool:
    """nvcuda.dll 是 NVIDIA 显卡驱动装进 System32 的 CUDA 驱动接口；能加载就说明有驱动。

    只是提示用（"你的机器大概率用得上 GPU 组件"），不阻止下载：驱动以后再装也行。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        ctypes.WinDLL("nvcuda.dll")
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ 激活（启动时、import torch 之前）

_active_dir: Path | None = None


def active_dir() -> Path | None:
    """本进程已激活的组件目录；None 表示本次运行用的是安装包自带的 CPU 版。"""
    return _active_dir


def activate(base: Path | None = None) -> PackStatus:
    """组件就绪就把它接到导入路径最前面。必须在任何 `import torch` / `import llama_cpp` 之前调用。

    torch 已经被导入过的话激活只会造成新旧两套混用，直接拒绝（返回 broken 并说明原因）。
    """
    global _active_dir
    root = components_root(base)
    pending = root / REMOVE_PENDING_NAME
    if pending.exists():
        # 上次运行时用户点了"删除"，但组件 DLL 正被加载、删不掉，留到这次启动、加载前删。
        shutil.rmtree(pack_dir(base), ignore_errors=True)
        try:
            pending.unlink()
        except OSError:
            pass
    st = pack_status(base)
    if not st.is_ready:
        return st
    if "torch" in sys.modules:
        return PackStatus(STATE_BROKEN, st.path, "torch 已在激活前被导入，本次运行仍是 CPU 版")
    site = st.path / "site"
    llama_lib = st.path / "llama_lib"
    sys.path.insert(0, str(site))
    os.environ["LLAMA_CPP_LIB_PATH"] = str(llama_lib)
    torch_lib = site / "torch" / "lib"
    if sys.platform == "win32":
        # ggml-cuda.dll 按名字找 cudart64_12/cublas64_12，它们在 GPU 版 torch/lib 里。
        try:
            os.add_dll_directory(str(torch_lib))
        except OSError:
            pass
        os.environ["PATH"] = str(torch_lib) + os.pathsep + os.environ.get("PATH", "")
    _active_dir = st.path
    return st


def schedule_removal(base: Path | None = None) -> bool:
    """删除组件。本进程正在用它（DLL 已加载、Windows 删不掉）时改为下次启动前删除。

    返回 True = 已立即删除；False = 已安排到下次启动。
    """
    d = pack_dir(base)
    if _active_dir is not None and d.resolve() == _active_dir.resolve():
        components_root(base).mkdir(parents=True, exist_ok=True)
        (components_root(base) / REMOVE_PENDING_NAME).write_text("1", encoding="utf-8")
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


# ------------------------------------------------------------------ 纯函数：解包规则

def select_members(spec: WheelSpec, names: Iterable[str]) -> list[tuple[str, str]]:
    """按 spec 的规则从 wheel 条目里挑出要解包的文件，返回 (zip 内路径, 相对 dest 的落地路径)。"""
    out: list[tuple[str, str]] = []
    for name in names:
        if name.endswith("/"):
            continue
        if not any(name.startswith(p) for p in spec.include):
            continue
        if any(name.startswith(p) for p in spec.exclude_prefixes):
            continue
        if any(name.lower().endswith(s) for s in spec.exclude_suffixes):
            continue
        rel = name[len(spec.strip):] if spec.strip and name.startswith(spec.strip) else name
        if not rel or ".." in Path(rel).parts or Path(rel).is_absolute():
            continue  # 防 zip slip：不允许跳出目标目录
        out.append((name, f"{spec.dest}/{rel}"))
    return out


def url_candidates(spec: WheelSpec) -> list[str]:
    """官方地址永远第一个，镜像排后面——和 subgen 的 url_candidates 同一个原则。"""
    return [spec.url, *spec.mirrors]


# ------------------------------------------------------------------ 下载

ProgressCb = Callable[[str, int, int], None]   # (阶段描述, 已完成字节, 总字节)
LogCb = Callable[[str], None]

_USER_AGENT = "comicgen-component-downloader"
_CHUNK = 1024 * 1024
_ATTEMPTS_PER_URL = 2


class InstallError(Exception):
    """人话摘要 + 技术细节分层：str(e) 给界面，technical 进日志。"""

    def __init__(self, message: str, technical: str = ""):
        super().__init__(message)
        self.technical = technical


class Cancelled(InstallError):
    def __init__(self):
        super().__init__("已取消下载（已下载的部分保留，下次点下载会从断点继续）")


def _hash_existing(path: Path) -> tuple["hashlib._Hash", int]:
    h = hashlib.sha256()
    n = 0
    try:
        with open(path, "rb") as f:
            while chunk := f.read(_CHUNK):
                h.update(chunk)
                n += len(chunk)
    except OSError:
        return hashlib.sha256(), 0
    return h, n


def _download_once(url: str, part: Path, spec: WheelSpec, progress: ProgressCb,
                   cancel: threading.Event, log: LogCb) -> str:
    """单次尝试（可能是续传）。返回整文件 sha256 hex。.part 在任何失败路径都保留，给续传用。"""
    hasher, have = _hash_existing(part) if part.is_file() else (hashlib.sha256(), 0)
    if have > spec.size:
        part.unlink(missing_ok=True)
        hasher, have = hashlib.sha256(), 0
    if have == spec.size:
        return hasher.hexdigest()
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    if have:
        req.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(req, timeout=60) as resp:
        status = resp.status
        if status == 206 and have:
            log(f"{spec.label}：从断点续传（已下载 {have / 1e6:.0f} MB）")
            mode = "ab"
        elif status == 200:
            if have:
                log(f"{spec.label}：服务器不支持断点续传，从头下载")
            hasher, have, mode = hashlib.sha256(), 0, "wb"
        else:
            raise InstallError(f"下载 {spec.label} 失败：HTTP {status}", url)
        last = 0.0
        with open(part, mode) as f:
            while True:
                if cancel.is_set():
                    raise Cancelled()
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                hasher.update(chunk)
                have += len(chunk)
                now = time.monotonic()
                if now - last > 0.2:
                    progress(f"下载 {spec.label}", have, spec.size)
                    last = now
    progress(f"下载 {spec.label}", have, spec.size)
    if have != spec.size:
        raise InstallError(f"{spec.label} 下载不完整（{have}/{spec.size} 字节）", url)
    return hasher.hexdigest()


def download_wheel(spec: WheelSpec, dest_dir: Path, progress: ProgressCb, log: LogCb,
                   cancel: threading.Event) -> Path:
    """下载并强校验一个 wheel。已有校验通过的成品直接复用；中断留下的 .part 会续传。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / spec.file_name
    if final.is_file() and final.stat().st_size == spec.size:
        h, _ = _hash_existing(final)
        if h.hexdigest() == spec.sha256:
            log(f"{spec.label}：已下载过且校验通过，跳过")
            progress(f"下载 {spec.label}", spec.size, spec.size)
            return final
        final.unlink(missing_ok=True)
    part = dest_dir / (spec.file_name + ".part")
    last_err = ""
    candidates = url_candidates(spec)
    for i, url in enumerate(candidates):
        if i:
            log(f"{spec.label}：官方源失败，改用镜像 {url.split('/')[2]}")
        for attempt in range(_ATTEMPTS_PER_URL):
            if cancel.is_set():
                raise Cancelled()
            try:
                digest = _download_once(url, part, spec, progress, cancel, log)
            except Cancelled:
                raise
            except (urllib.error.URLError, OSError, InstallError) as e:
                last_err = f"{type(e).__name__}: {e}"
                log(f"{spec.label}：下载中断（第 {attempt + 1} 次，{url.split('/')[2]}）：{last_err}")
                time.sleep(2)
                continue
            if digest != spec.sha256:
                # 文件完整但内容不对：删掉，换下一个源，不在同一个源上反复下同一份坏文件。
                part.unlink(missing_ok=True)
                last_err = f"SHA256 不一致（期望 {spec.sha256}，实际 {digest}）"
                log(f"{spec.label}：{last_err}")
                break
            os.replace(part, final)
            log(f"{spec.label}：下载完成，SHA256 校验通过")
            return final
    raise InstallError(f"{spec.label} 下载失败，请检查网络后点「重试」（已下载部分会保留）", last_err)


# ------------------------------------------------------------------ 解包与安装

def _extract(spec: WheelSpec, wheel: Path, target: Path, progress: ProgressCb,
             cancel: threading.Event) -> int:
    with zipfile.ZipFile(wheel) as zf:
        members = select_members(spec, zf.namelist())
        total = sum(zf.getinfo(n).file_size for n, _ in members)
        done = 0
        last = 0.0
        for name, rel in members:
            if cancel.is_set():
                raise Cancelled()
            out = target / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            # zipfile 读完每个条目会核对 CRC32，损坏会抛 BadZipFile——wheel 整体已过 SHA256，
            # 这是第二道。
            with zf.open(name) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst, _CHUNK)
            done += zf.getinfo(name).file_size
            now = time.monotonic()
            if now - last > 0.2:
                progress(f"解压 {spec.label}", done, total)
                last = now
        progress(f"解压 {spec.label}", total, total)
        return len(members)


def check_disk_space(base: Path | None = None) -> tuple[int, int] | None:
    """返回 (需要字节, 可用字节)；查不到返回 None（不阻断）。需要 = 还没下完的 wheel + 解包占用。"""
    root = components_root(base)
    probe = root
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return None
    need = EXTRACTED_BYTES
    dl = downloads_dir(base)
    for w in WHEELS:
        have = 0
        for p in (dl / w.file_name, dl / (w.file_name + ".part")):
            if p.is_file():
                have = max(have, p.stat().st_size)
        need += max(0, w.size - have)
    return need, free


def install(base: Path | None = None, progress: ProgressCb | None = None, log: LogCb | None = None,
            cancel: threading.Event | None = None, keep_downloads: bool = False) -> PackStatus:
    """下载 → 校验 → 解包到临时目录 → 核对必需文件 → 写安装记录 → 原子改名就位。

    任何一步失败都抛 InstallError，不会留下一个"看起来装好了"的目录：就位前的目录叫
    gpu-cu124.tmp，安装记录在改名前最后一步写入，pack_status 认的是记录 + 必需文件。
    """
    progress = progress or (lambda *_: None)
    log = log or (lambda *_: None)
    cancel = cancel or threading.Event()

    st = pack_status(base)
    if st.is_ready:
        log("GPU 组件已安装，无需重复下载")
        return st
    if _active_dir is not None:
        raise InstallError("组件正在使用中，请重启程序后再试")

    space = check_disk_space(base)
    if space and space[0] > space[1]:
        need, free = space
        raise InstallError(
            f"磁盘空间不足：需要约 {need / 1e9:.1f} GB，"
            f"{components_root(base).anchor or '程序所在盘'} 只剩 {free / 1e9:.1f} GB")

    dl = downloads_dir(base)
    wheels: list[tuple[WheelSpec, Path]] = []
    for spec in WHEELS:
        wheels.append((spec, download_wheel(spec, dl, progress, log, cancel)))

    final = pack_dir(base)
    tmp = final.with_name(final.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        for spec, wheel in wheels:
            n = _extract(spec, wheel, tmp, progress, cancel)
            log(f"{spec.label}：解压 {n} 个文件")
    except zipfile.BadZipFile as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise InstallError("组件压缩包损坏，请删除 components\\_downloads 后重试", str(e)) from e
    except OSError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise InstallError(f"解压失败：{e.strerror or e}", str(e)) from e
    except Cancelled:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    missing = [f for f in REQUIRED_FILES if not (tmp / f).is_file()]
    if missing:
        shutil.rmtree(tmp, ignore_errors=True)
        raise InstallError("组件解压结果不完整", f"缺少 {missing}")
    (tmp / MARKER_NAME).write_text(json.dumps({
        "pack_id": PACK_ID,
        "wheels": {s.key: s.sha256 for s in WHEELS},
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(final, ignore_errors=True)
    os.replace(tmp, final)
    if not keep_downloads:
        for _, wheel in wheels:
            wheel.unlink(missing_ok=True)
        try:
            dl.rmdir()
        except OSError:
            pass
    st = pack_status(base)
    if not st.is_ready:
        raise InstallError("安装后自检失败", st.reason)
    log("GPU 组件安装完成，重启程序后生效")
    return st
