"""运行配置。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    # ---- 翻译 ----
    translator: str = "sakura"  # sakura(本地，开箱即用) | deepseek | mock
    source_lang: str = "ja"  # ja | en | zh | other —— 决定 OCR 后端与推荐引擎
    target_lang: str = "简体中文"
    deepseek_api_key: str = ""  # 也可用环境变量 DEEPSEEK_API_KEY
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    sakura_repo: str = "SakuraLLM/Sakura-7B-Qwen2.5-v1.0-GGUF"
    sakura_filename: str = ""  # 留空则自动在 repo 中选最小可用量化（本仓为 IQ4_XS）
    # ---- 检测 ----
    detector_repo: str = "ogkalu/comic-text-and-bubble-detector"
    det_threshold: float = 0.3
    # ---- 渲染 ----
    direction: str = "auto"  # auto | horizontal | vertical
    font_path: str = r"C:\Windows\Fonts\msyh.ttc"
    min_font_size: int = 10
    max_font_size: int = 48
    # ---- 运行 ----
    device: str = "cuda"
    pdf_dpi: int = 150  # PDF 输入时每页渲染 DPI（150 快且够用，300 画质好但慢）

    def resolved_api_key(self) -> str:
        return self.deepseek_api_key or os.environ.get("DEEPSEEK_API_KEY", "")
