"""模型清册：全部依赖模型的单一真相源。

流水线（Config 默认值）与 UI 模型管理/首次运行页都从这里取 repo/文件特征，避免多处各写一份而漂移。
"""
from __future__ import annotations

from dataclasses import dataclass

LAYOUT_FILE = "file"  # 单个可直接加载的文件（GGUF/.pt）
LAYOUT_DIR = "dir"    # HuggingFace snapshot 目录 / EasyOCR 存储目录


@dataclass(frozen=True)
class ModelSpec:
    key: str              # 内部标识，用于代码引用
    label: str             # 界面显示名
    purpose: str           # 用途说明，模型清单"用途"列用
    repo: str              # HuggingFace repo id（无 repo 的模型留空，如 LaMa）
    kind: str              # detect | ocr | llm_gguf | inpaint
    layout: str            # LAYOUT_FILE | LAYOUT_DIR —— 决定状态检查方式与"定位…"对话框类型
    approx_size: str       # 人类可读大小，界面提示用兜底
    required: bool         # False = 按需（如 Sakura 仅日文才需要）

    # ---- LAYOUT_FILE 专用 ----
    filename: str = ""     # 确切文件名
    size_bytes: int = 0    # 精确字节数（已实测核实）；0 = 未核实，状态检查时跳过精确比对
    magic: bytes = b""     # 文件头前 4 字节，"定位…"廉价预检用
    ext_filter: str = ""   # 文件选择对话框的扩展名过滤器

    # ---- LAYOUT_DIR 专用 ----
    # 判定"目录是否完整"的成员文件表：外层各元组之间是 AND，元组内部是 OR
    # （例：manga-ocr-base 有两个 snapshot 目录，一个只有 model.safetensors，
    #  另一个有 config/tokenizer 但没有 safetensors——必须都齐全才算就绪，
    #  "目录存在"本身不是可靠判据，已实测踩过这个坑）。
    member_files: tuple[tuple[str, ...], ...] = ()


MODEL_MANIFEST: dict[str, ModelSpec] = {
    "detect": ModelSpec(
        key="detect",
        label="检测模型",
        purpose="文字/气泡定位",
        repo="ogkalu/comic-text-and-bubble-detector",
        kind="detect",
        layout=LAYOUT_DIR,
        approx_size="~170MB",
        required=True,
        member_files=(
            ("config.json",),
            ("preprocessor_config.json",),
            ("model.safetensors",),
        ),
    ),
    "ocr_ja": ModelSpec(
        key="ocr_ja",
        label="OCR（manga-ocr，日文）",
        purpose="日文文字识别",
        repo="kha-white/manga-ocr-base",
        kind="ocr",
        layout=LAYOUT_DIR,
        approx_size="~450MB",
        required=True,
        member_files=(
            ("config.json",),
            ("preprocessor_config.json",),
            ("vocab.txt",),
            ("pytorch_model.bin", "model.safetensors"),
        ),
    ),
    "ocr_other": ModelSpec(
        key="ocr_other",
        label="OCR（EasyOCR，非日文）",
        purpose="英文/中文等文字识别",
        repo="",  # EasyOCR 走自己的模型仓库，非标准 HF repo，仅记录存储目录
        kind="ocr",
        layout=LAYOUT_DIR,
        approx_size="~100MB",
        required=False,
        member_files=(
            ("craft_mlt_25k.pth",),
            ("english_g2.pth",),
        ),
    ),
    "sakura": ModelSpec(
        key="sakura",
        label="Sakura（本地日译中）",
        purpose="日文漫画翻译（轻小说风格）",
        repo="SakuraLLM/Sakura-7B-Qwen2.5-v1.0-GGUF",
        kind="llm_gguf",
        layout=LAYOUT_FILE,
        approx_size="~4.3GB",
        required=False,
        filename="sakura-7b-qwen2.5-v1.0-iq4xs.gguf",
        size_bytes=4_250_298_208,
        magic=b"GGUF",
        ext_filter="GGUF 模型 (*.gguf)",
    ),
    # 与同机 subgen 项目声明的 repo/文件名保持完全一致，两个项目共用 F:\hf_cache 下的同一份权重。
    # 改这里前先确认 subgen 那边（src/translate/mod.rs）用的是不是同一个文件名，否则会各下一份。
    "llm": ModelSpec(
        key="llm",
        label="通用本地翻译（多语言）",
        purpose="非日文漫画翻译",
        repo="unsloth/Qwen3-4B-Instruct-2507-GGUF",
        kind="llm_gguf",
        layout=LAYOUT_FILE,
        approx_size="~2.4GB",
        required=False,
        filename="Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        size_bytes=2_497_281_120,
        magic=b"GGUF",
        ext_filter="GGUF 模型 (*.gguf)",
    ),
    "lama": ModelSpec(
        key="lama",
        label="抹字 LaMa",
        purpose="擦除原文字，为渲染译文留白",
        repo="",  # 走 simple_lama_inpainting 内置下载（GitHub release），非 HF repo
        kind="inpaint",
        layout=LAYOUT_FILE,
        approx_size="~200MB",
        required=True,
        filename="big-lama.pt",
        size_bytes=205_803_670,
        magic=b"PK\x03\x04",  # torch.save 用 zip 容器
        ext_filter="PyTorch 模型 (*.pt)",
    ),
}


def get_model(key: str) -> ModelSpec:
    return MODEL_MANIFEST[key]
