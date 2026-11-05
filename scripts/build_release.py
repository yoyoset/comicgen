"""构建发布用的 CPU 版安装包：PyInstaller → 产物体检 → zip → sha256。

用法（仓库根目录）：
    .\\.venv-cpu\\Scripts\\python.exe scripts\\build_release.py

为什么单独一个脚本、单独一个 venv：
- 发布包只带 CPU 框架，GPU 部分由程序「组件」页首次运行下载（core/gpu_components.py）。
  PyInstaller 只能打包"当前环境里装的那份" torch / llama-cpp-python，所以发布必须在
  装着 CPU 版的 .venv-cpu 里构建；日常开发的 .venv 是 CUDA 版（scripts/build_exe.ps1 用它，
  产物 5.9G，只给本机自用）。
- 产物从全新的 build\\release 目录出来，不是 dist\\comicgen——后者是本机自用目录，里面有
  settings.json / comicgen.log / models\\ 等私人数据，直接打包会把它们带出去。

体检项（任何一项不过都退出 1，不产出 zip）：
1. 构建环境确实是 CPU 版：torch.version.cuda 为 None，llama_cpp/lib 里没有 ggml-cuda.dll；
2. 产物只有 comicgen-ui.exe + _internal\\；
3. 不含私人数据：settings.json、*.log、models\\、components\\、*.gguf、*.safetensors；
4. 不含 CUDA 大件（torch_cuda.dll / cudnn* / cublas* / ggml-cuda.dll）；
5. 没有单个文件 ≥ 2GiB（GitHub Release 单文件上限），zip 本身也要 < 2GiB。
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv-cpu" / "Scripts" / "python.exe"
DIST = ROOT / "build" / "release"
WORK = ROOT / "build" / "work-release"
OUT_DIR = ROOT / "dist" / "release"
APP = "comicgen"
EXE = "comicgen-ui.exe"
GIB2 = 2 * 1024 ** 3

PRIVATE_PATTERNS = ("settings.json", "settings.json.bak", "*.log", "*.gguf", "*.safetensors",
                    "big-lama.pt", "pages.json", "job.json")
PRIVATE_DIRS = ("models", "components", "hf", "hub")
CUDA_PATTERNS = ("torch_cuda.dll", "c10_cuda.dll", "cudnn*.dll", "cublas*.dll", "cufft*.dll",
                 "cusparse*.dll", "cusolver*.dll", "curand*.dll", "nvrtc*.dll", "ggml-cuda.dll")


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}")
    sys.exit(1)


def version() -> str:
    for line in (ROOT / "comic_gen" / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip('"')
    fail("读不到 comic_gen.__version__")
    return ""


def check_build_env() -> None:
    if not VENV_PY.is_file():
        fail(f"缺少 CPU 构建环境 {VENV_PY}，按 HANDOFF.md「构建发布包」一节创建")
    code = ("import torch, llama_cpp, pathlib; "
            "lib = pathlib.Path(llama_cpp.__file__).parent / 'lib'; "
            "print(torch.__version__, torch.version.cuda, (lib / 'ggml-cuda.dll').exists())")
    out = subprocess.run([str(VENV_PY), "-c", code], capture_output=True, text=True, check=True).stdout.split()
    print(f"构建环境：torch {out[0]}，torch.version.cuda={out[1]}，ggml-cuda.dll 存在={out[2]}")
    if out[1] != "None" or out[2] != "False":
        fail(".venv-cpu 里装的不是 CPU 版 torch / llama-cpp-python")


def build() -> Path:
    shutil.rmtree(DIST, ignore_errors=True)
    t0 = time.monotonic()
    r = subprocess.run([str(VENV_PY), "-m", "PyInstaller", "build_exe.spec", "--noconfirm", "--clean",
                        "--distpath", str(DIST), "--workpath", str(WORK)], cwd=ROOT)
    if r.returncode != 0:
        fail("PyInstaller 构建失败")
    print(f"PyInstaller 用时 {time.monotonic() - t0:.0f}s")
    return DIST / APP


def inspect(app: Path) -> tuple[int, int]:
    entries = sorted(p.name for p in app.iterdir())
    if entries != sorted([EXE, "_internal"]):
        fail(f"产物结构不符合预期：{entries}")
    total, count, biggest = 0, 0, (0, "")
    problems: list[str] = []
    for dirpath, dirnames, filenames in os.walk(app):
        rel_dir = Path(dirpath).relative_to(app)
        for d in dirnames:
            if d in PRIVATE_DIRS and rel_dir == Path("."):
                problems.append(f"私人数据目录：{rel_dir / d}")
        for f in filenames:
            p = Path(dirpath) / f
            size = p.stat().st_size
            total += size
            count += 1
            biggest = max(biggest, (size, str(p.relative_to(app))))
            rel = p.relative_to(app)
            if rel.parent == Path(".") and any(fnmatch.fnmatch(f, pat) for pat in PRIVATE_PATTERNS):
                problems.append(f"私人数据文件：{rel}")
            if any(fnmatch.fnmatch(f.lower(), pat) for pat in ("*.gguf", "*.safetensors")):
                problems.append(f"模型权重：{rel}")
            if any(fnmatch.fnmatch(f.lower(), pat) for pat in CUDA_PATTERNS):
                problems.append(f"CUDA 库混进了 CPU 包：{rel}")
            if size >= GIB2:
                problems.append(f"单文件超过 2GiB：{rel}（{size}）")
    if problems:
        fail("产物体检不通过：\n  " + "\n  ".join(problems))
    print(f"产物：{count} 个文件，{total / 1e9:.2f} GB，最大单文件 {biggest[1]} {biggest[0] / 1e6:.0f} MB")
    return total, count


def make_zip(app: Path, ver: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"comicgen-{ver}-win64-cpu.zip"
    out.unlink(missing_ok=True)
    t0 = time.monotonic()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dirpath, _, filenames in os.walk(app):
            for f in filenames:
                p = Path(dirpath) / f
                zf.write(p, Path(APP) / p.relative_to(app))
        # 二进制包里带着 PyMuPDF（AGPL-3.0）：许可与第三方声明必须随包分发，见 THIRD_PARTY_NOTICES.md。
        for doc in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            zf.write(ROOT / doc, Path(APP) / doc)
    size = out.stat().st_size
    print(f"zip：{out}（{size / 1e9:.2f} GB，{size} 字节，压缩用时 {time.monotonic() - t0:.0f}s）")
    if size >= GIB2:
        fail("zip 超过 2GiB，GitHub Release 传不上去")
    h = hashlib.sha256()
    with open(out, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    digest = h.hexdigest()
    (out.with_suffix(out.suffix + ".sha256")).write_text(f"{digest}  {out.name}\n", encoding="ascii")
    print(f"sha256：{digest}")
    return out


def main() -> None:
    ver = version()
    check_build_env()
    app = build()
    inspect(app)
    make_zip(app, ver)


if __name__ == "__main__":
    main()
