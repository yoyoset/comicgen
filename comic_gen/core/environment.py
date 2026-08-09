"""运行环境能力检测：GPU/本地 LLM 依赖/外部工具/模型是否就位。

Qt-free，供 UI 首次运行自检与设置页"检查环境"按钮复用，也可独立单测。
不在这里下载任何东西——只检测现状，下载逻辑仍在各 pipeline 模块里。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .manifest import MODEL_MANIFEST
from .model_locator import ModelStatus, resolve_status
from .model_locator import clear_cache as _clear_locator_cache


def apply_model_dir(model_dir: str | None) -> None:
    """把 HuggingFace 缓存重定向到指定目录（留空则不改动，沿用全局默认）。

    影响 huggingface_hub 后续所有 from_pretrained/hf_hub_download/snapshot_download 调用，
    需在加载任何模型前调用。
    """
    base = (model_dir or "").strip().strip('"') or None
    if not base:
        return
    _clear_locator_cache()  # 搜索根变了，之前缓存的命中结果作废
    Path(base).mkdir(parents=True, exist_ok=True)
    hub = str(Path(base) / "hub")
    os.environ["HF_HOME"] = base
    os.environ["HF_HUB_CACHE"] = hub
    try:
        import huggingface_hub.constants as hc
        hc.HF_HUB_CACHE = hub
        if hasattr(hc, "HF_HOME"):
            hc.HF_HOME = base
    except Exception:
        pass

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
    model_paths: dict[str, str] = field(default_factory=dict)  # manifest key -> 实际命中路径
    model_status: dict[str, ModelStatus] = field(default_factory=dict)  # manifest key -> 四态状态
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


def enable_offline_mode() -> None:
    """所需模型都已在本地时，关掉 HuggingFace 的联网校验。

    为什么需要：即使权重已经下好，from_pretrained 每次仍会 HEAD 一次远端核对 etag。
    网络不通时它按退避策略重试 5 次——实测 manga-ocr 卡了 72 秒才继续
    （见 dist/comicgen/comicgen.log 15:06:59→15:08:11），开了这个开关后是 1.1 秒。
    只在模型齐备时调用，否则首次下载会被这个开关挡住。
    """
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    # 环境变量在这两个库里都是 import 期读进常量的，运行时改必须连常量一起改
    for mod, attr in (("huggingface_hub.constants", "HF_HUB_OFFLINE"),
                      ("transformers.utils.hub", "_is_offline_mode")):
        try:
            import importlib
            setattr(importlib.import_module(mod), attr, True)
        except Exception:
            pass


def find_model_path(key: str, model_dir: str | None = None,
                     bindings: Mapping[str, str] | None = None) -> str | None:
    """返回某个模型实际命中的本地路径，未就绪返回 None。

    诊断页用它把"复用到哪份权重"显示给用户——否则"已缓存"三个字看不出用的是哪一份。
    """
    st = resolve_status(key, bindings=bindings, model_dir=model_dir)
    return str(st.path) if st.is_ready and st.path else None


def is_model_cached(key: str, model_dir: str | None = None,
                     bindings: Mapping[str, str] | None = None) -> bool:
    """检测 MODEL_MANIFEST 里某个模型是否已本地就位。UI 模型管理页复用此函数，避免自行重复实现检查逻辑。

    改走 resolve_status 后修掉两个实测到的真问题：
    (1) 原先只要 `import simple_lama_inpainting` 成功就报 LaMa"已就绪"，权重根本没下也报绿；
    (2) 原先完全忽略 model_dir 参数，同一份报告里 find_model_path 却用了它，两者会给出矛盾答案。
    """
    return resolve_status(key, bindings=bindings, model_dir=model_dir).is_ready


def detect_environment(model_dir: str | None = None, check_models: bool = True,
                        bindings: Mapping[str, str] | None = None) -> EnvReport:
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
            st = resolve_status(key, bindings=bindings, model_dir=model_dir)
            report.model_status[key] = st
            report.models_cached[key] = st.is_ready
            if st.path:
                report.model_paths[key] = str(st.path)

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
