"""运行配置。"""
from __future__ import annotations

from dataclasses import dataclass

from .manifest import MODEL_MANIFEST


@dataclass
class Config:
    # ---- 翻译（全部本地引擎，无需联网 API） ----
    translator: str = "sakura"  # sakura(本地，日译中，开箱即用) | llm(本地，多语言) | mock
    source_lang: str = "ja"  # ja | en | zh | other —— 决定 OCR 后端与推荐引擎
    target_lang: str = "简体中文"
    sakura_repo: str = MODEL_MANIFEST["sakura"].repo
    sakura_filename: str = ""  # 留空则自动在 repo 中选最小可用量化（本仓为 IQ4_XS）
    llm_repo: str = MODEL_MANIFEST["llm"].repo
    llm_filename: str = ""  # 留空则自动在 repo 中选最小可用量化
    # ---- 检测 ----
    detector_repo: str = MODEL_MANIFEST["detect"].repo
    det_threshold: float = 0.3
    # ---- 渲染 ----
    direction: str = "auto"  # auto | horizontal | vertical
    font_path: str = r"C:\Windows\Fonts\msyh.ttc"
    min_font_size: int = 10
    max_font_size: int = 48
    # ---- 运行 ----
    device: str = "cuda"
    pdf_dpi: int = 150  # PDF 输入时每页渲染 DPI（150 快且够用，300 画质好但慢）
