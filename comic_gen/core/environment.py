"""运行环境能力检测：GPU/本地 LLM 依赖/外部工具/模型是否就位。

Qt-free，供 UI 首次运行自检与设置页"检查环境"按钮复用，也可独立单测。
不在这里下载任何东西——只检测现状，下载逻辑仍在各 pipeline 模块里。
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import MODEL_MANIFEST

# 本机常见 WinRAR/UnRAR 安装位置（RAR/CBR 输入需要）。
# 单一来源：pipeline.loader 的 RAR 解包逻辑复用这里，避免两处路径列表漂移。
UNRAR_CANDIDATES = (
    r"C:\Program Files\WinRAR\UnRAR.exe",
    r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
)


def find_unrar() -> str | None:
    for cand in UNRAR_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


@dataclass
class EnvReport:
    cuda_available: bool = False
    llama_cpp_available: bool = False
    unrar_path: str | None = None
    models_cached: dict[str, bool] = field(default_factory=dict)  # manifest key -> 是否已缓存
    disk_free_gb: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def unrar_available(self) -> bool:
        return self.unrar_path is not None

    @property
    def ready_for_offline_run(self) -> bool:
        """检测+OCR(日文)+抹字是最小可跑集合；Sakura/通用LLM 按需。"""
        required = ("detect", "ocr_ja", "lama")
        return all(self.models_cached.get(k, False) for k in required)


def _check_model_cached(key: str) -> bool:
    spec = MODEL_MANIFEST[key]
    if spec.kind == "inpaint":
        # LaMa 走 simple_lama_inpainting 内置下载（非 HF repo），只能探测包是否可导入
        try:
            import simple_lama_inpainting  # noqa: F401
            return True
        except Exception:
            return False
    if not spec.repo:
        return False
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(spec.repo, local_files_only=True)
        return True
    except Exception:
        return False


def detect_environment(model_dir: str | None = None, check_models: bool = True) -> EnvReport:
    report = EnvReport()

    try:
        import torch
        report.cuda_available = bool(torch.cuda.is_available())
    except Exception as e:
        report.warnings.append(f"torch 检测失败: {e}")

    try:
        import llama_cpp  # noqa: F401
        report.llama_cpp_available = True
    except Exception:
        report.llama_cpp_available = False

    report.unrar_path = find_unrar()

    if check_models:
        for key in MODEL_MANIFEST:
            report.models_cached[key] = _check_model_cached(key)

    check_dir = Path(model_dir) if model_dir else Path.home()
    try:
        usage = shutil.disk_usage(check_dir if check_dir.exists() else check_dir.anchor or ".")
        report.disk_free_gb = round(usage.free / (1024 ** 3), 1)
    except Exception as e:
        report.warnings.append(f"磁盘空间检测失败: {e}")

    if not report.cuda_available:
        report.warnings.append("未检测到可用 CUDA，将使用 CPU（速度明显变慢）")
    if not report.unrar_available:
        report.warnings.append("未检测到 WinRAR/UnRAR，无法处理 RAR/CBR 输入")

    return report
