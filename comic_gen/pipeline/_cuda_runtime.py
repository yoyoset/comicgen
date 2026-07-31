"""提前把 torch 自带的 CUDA 运行时 DLL 目录加入搜索路径，供 llama_cpp GPU 后端加载。

之前指向 nvidia-cublas-cu12 等独立 pip 包（_internal/nvidia/*/bin/）——那是给
llama-cpp-python 单独装的一套 CUDA 12.9 运行时，而 torch/lib 下已经自带同一套
DLL（cublas64_12.dll/cublasLt64_12.dll/cufft64_11.dll/cufftw64_11.dll/
nvrtc64_120_0.dll/nvJitLink_120_0.dll/cudart64_12.dll，CUDA 12.4 系），是重复的
第二套 CUDA 栈，打包体积多约 1.3GB。改为直接指向 torch/lib——它是单层目录
（不像 nvidia 包那样每个子包各有自己的 bin/ 子目录），逻辑也跟着简化成单目录。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def preload_cuda_runtime():
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "_internal" / "torch" / "lib")
    else:
        import site

        for d in site.getsitepackages():
            candidates.append(Path(d) / "torch" / "lib")
    for lib_dir in candidates:
        if not lib_dir.is_dir():
            continue
        if str(lib_dir) in os.environ.get("_CG_CUDA_PATHS", ""):
            continue
        try:
            os.add_dll_directory(str(lib_dir))
        except Exception:
            pass
        os.environ["_CG_CUDA_PATHS"] = os.environ.get("_CG_CUDA_PATHS", "") + ";" + str(lib_dir)
        os.environ["PATH"] = str(lib_dir) + os.pathsep + os.environ.get("PATH", "")