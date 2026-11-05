"""把"实际会被 import 的那份 torch"的 lib 目录加入 DLL 搜索路径，供 llama_cpp GPU 后端加载。

ggml-cuda.dll 按名字找 cudart64_12.dll / cublas64_12.dll（CUDA 12.4 系），torch GPU 版的
torch/lib 里自带同一套——借用它，不再单独打包/下载一套 CUDA 运行时。

历史：最早指向 nvidia-cublas-cu12 等独立 pip 包（重复的第二套 CUDA 栈，多约 1.3GB）；
后来写死成 `_internal/torch/lib`。2026-11-05 安装包改为 CPU 版、GPU 版 torch 由
core.gpu_components 首次运行下载到 components/ 后，写死的路径会指到 CPU 版 torch（里面
没有 cudart），所以改为用 importlib.util.find_spec 问"torch 会从哪导入"，不 import torch
本身，也自动跟随 gpu_components.activate() 对 sys.path 的调整。
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path


def torch_lib_dir() -> Path | None:
    try:
        spec = importlib.util.find_spec("torch")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    lib_dir = Path(spec.origin).parent / "lib"
    return lib_dir if lib_dir.is_dir() else None


def preload_cuda_runtime():
    lib_dir = torch_lib_dir()
    if lib_dir is None:
        return
    if str(lib_dir) in os.environ.get("_CG_CUDA_PATHS", ""):
        return
    try:
        os.add_dll_directory(str(lib_dir))
    except Exception:
        pass
    os.environ["_CG_CUDA_PATHS"] = os.environ.get("_CG_CUDA_PATHS", "") + ";" + str(lib_dir)
    os.environ["PATH"] = str(lib_dir) + os.pathsep + os.environ.get("PATH", "")
