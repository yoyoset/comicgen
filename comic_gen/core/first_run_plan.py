"""首次运行"要下什么、多大、放哪"的纯函数，以及模型下载进度的旁路估算。Qt-free，可单测。

为什么要旁路估算进度：模型下载走的是 transformers.from_pretrained / hf_hub_download /
torch.hub.download_url_to_file，都没有进度回调；8 GB 的 Sakura 下载期间界面上只有一行
"下载中…"，用户分不清是在下还是卡死了。这里按"下载目标目录的字节数增长"来估：
HF 模型看 `<hub>/models--<org>--<name>/blobs/`（下载中是 *.incomplete，完成后改名，
总字节不变），LaMa 看 torch hub 的 checkpoints 目录（下载中是 big-lama.pt.<uuid>.partial）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .manifest import MODEL_MANIFEST

# 默认勾选：必需的三个 + 默认日译中引擎 Sakura（AppSettings 默认 source_lang=ja / translator=sakura）。
# 非日文 OCR 和通用 LLM 只在翻译非日文时需要，首次运行不默认下（通用 LLM 又是 8 GB）。
_DEFAULT_OPTIONAL = ("sakura",)

_SIZE_RE = re.compile(r"([\d.]+)\s*([KMG])B", re.I)
_UNIT = {"K": 10 ** 3, "M": 10 ** 6, "G": 10 ** 9}


def approx_bytes(key: str) -> int:
    """manifest 有精确字节数就用它，没有就解析 approx_size（"~170MB"）。解析不了返回 0。"""
    spec = MODEL_MANIFEST.get(key)
    if spec is None:
        return 0
    if spec.size_bytes:
        return spec.size_bytes
    m = _SIZE_RE.search(spec.approx_size or "")
    return int(float(m.group(1)) * _UNIT[m.group(2).upper()]) if m else 0


def default_selected(key: str) -> bool:
    spec = MODEL_MANIFEST.get(key)
    return bool(spec and (spec.required or key in _DEFAULT_OPTIONAL))


def total_bytes(keys) -> int:
    return sum(approx_bytes(k) for k in keys)


def human_size(n: int) -> str:
    """十进制单位（和组件页、下载站一致），≥1GB 一位小数，否则 MB 取整。"""
    if n >= 10 ** 9:
        return f"{n / 1e9:.1f} GB"
    return f"{max(1, round(n / 1e6))} MB"


# ------------------------------------------------------------------ 进度旁路估算

def hub_cache_dir() -> Path:
    """和 huggingface_hub 同一套优先级：HF_HUB_CACHE → HF_HOME/hub → ~/.cache/huggingface/hub。

    core.environment.apply_model_dir 会把这两个环境变量设成用户选的模型目录。
    """
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def torch_checkpoints_dir() -> Path:
    """torch.hub.get_dir() 的规则（TORCH_HOME → XDG_CACHE_HOME/torch → ~/.cache/torch）+ /hub/checkpoints。"""
    if os.environ.get("TORCH_HOME"):
        base = Path(os.environ["TORCH_HOME"])
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "torch"
    return base / "hub" / "checkpoints"


def watch_target(key: str) -> tuple[Path, str] | None:
    """返回 (要统计字节数的目录, glob)；不知道下载落在哪的（EasyOCR）返回 None。"""
    spec = MODEL_MANIFEST.get(key)
    if spec is None:
        return None
    if key == "lama":
        return torch_checkpoints_dir(), f"{spec.filename}*"
    if spec.repo:
        return hub_cache_dir() / ("models--" + spec.repo.replace("/", "--")) / "blobs", "*"
    return None


def watched_bytes(key: str) -> int | None:
    t = watch_target(key)
    if t is None:
        return None
    d, pattern = t
    total = 0
    try:
        for p in d.glob(pattern):
            try:
                total += p.stat().st_size
            except OSError:
                pass  # 下载器正好在改名，跳过这一轮
    except OSError:
        return 0
    return total
