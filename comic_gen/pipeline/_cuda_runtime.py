"""提前把 nvidia-* pip 包的 CUDA 运行时 DLL 目录加入搜索路径，供 llama_cpp GPU 后端加载。"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def preload_cuda_runtime():
    candidates = []
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent / "_internal" / "nvidia"
        candidates.append(base)
    else:
        import site

        for d in site.getsitepackages():
            candidates.append(Path(d) / "nvidia")
    for nvidia_dir in candidates:
        if not nvidia_dir.is_dir():
            continue
        for sub in nvidia_dir.iterdir():
            bin_dir = sub / "bin"
            if bin_dir.is_dir() and str(bin_dir) not in os.environ.get("_CG_CUDA_PATHS", ""):
                try:
                    os.add_dll_directory(str(bin_dir))
                except Exception:
                    pass
                os.environ["_CG_CUDA_PATHS"] = os.environ.get("_CG_CUDA_PATHS", "") + ";" + str(bin_dir)
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")