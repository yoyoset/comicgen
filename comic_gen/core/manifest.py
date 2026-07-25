"""模型清册：全部依赖模型的单一真相源。

流水线（Config 默认值）与 UI 模型管理页都从这里取 repo，避免两处各写一份而漂移。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    key: str            # 内部标识，用于代码引用
    label: str           # 界面显示名
    repo: str            # HuggingFace repo id
    kind: str            # detect | ocr | llm_gguf | inpaint
    approx_size: str     # 人类可读大小，界面提示用
    required: bool        # False = 按需（如 Sakura 仅日文才需要）
    filename: str = ""   # llm_gguf 专用：确切的 .gguf 文件名
    # 为什么要写死 filename：留空时代码只能调 list_repo_files() 联网列目录来挑量化，
    # 于是"权重明明在盘上却因为网络抖动崩掉"（实测 SSL UNEXPECTED_EOF）。
    # 写死之后本地发现全程离线，见 core.model_locator。


MODEL_MANIFEST: dict[str, ModelSpec] = {
    "detect": ModelSpec(
        key="detect",
        label="检测模型",
        repo="ogkalu/comic-text-and-bubble-detector",
        kind="detect",
        approx_size="~170MB",
        required=True,
    ),
    "ocr_ja": ModelSpec(
        key="ocr_ja",
        label="OCR（manga-ocr，日文）",
        repo="kha-white/manga-ocr-base",
        kind="ocr",
        approx_size="~450MB",
        required=True,
    ),
    "sakura": ModelSpec(
        key="sakura",
        label="Sakura（本地日译中）",
        repo="SakuraLLM/Sakura-7B-Qwen2.5-v1.0-GGUF",
        kind="llm_gguf",
        approx_size="~4.3GB",
        required=False,
        filename="sakura-7b-qwen2.5-v1.0-iq4xs.gguf",
    ),
    # 与同机 subgen 项目声明的 repo/文件名保持完全一致，两个项目共用 F:\hf_cache 下的同一份权重。
    # 改这里前先确认 subgen 那边（src/translate/mod.rs）用的是不是同一个文件名，否则会各下一份。
    "llm": ModelSpec(
        key="llm",
        label="通用本地翻译（多语言）",
        repo="unsloth/Qwen3-4B-Instruct-2507-GGUF",
        kind="llm_gguf",
        approx_size="~2.4GB",
        required=False,
        filename="Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
    ),
    "lama": ModelSpec(
        key="lama",
        label="抹字 LaMa",
        repo="",  # 走 simple_lama_inpainting 内置下载（GitHub release），非 HF repo
        kind="inpaint",
        approx_size="~200MB",
        required=True,
    ),
}


def get_model(key: str) -> ModelSpec:
    return MODEL_MANIFEST[key]
