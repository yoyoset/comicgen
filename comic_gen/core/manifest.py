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
        # 2026-11-02 由 Sakura-7B-Qwen2.5-v1.0 换成 Sakura-14B-Qwen3-v1.5（2026-01 发布）。
        # 同机 16 句日语台词 + 批量对齐实测：7B 把单数译成"你们"、批量 16 句只回 14 行；
        # 14B 都对（样本很小，只作方向性证据）。大小与 subgen 项目
        # 声明的同一文件一致，共用 F:\hf_cache 下一份权重。许可 cc-by-nc-sa-4.0（非商用）。
        # 旧 7B 文件仍在磁盘上，但绑定了它的旧设置会因大小不符被判无效，需要重新「定位…」。
        repo="SakuraLLM/Sakura-14B-Qwen3-v1.5-GGUF",
        kind="llm_gguf",
        layout=LAYOUT_FILE,
        approx_size="~8.2GB",
        required=False,
        filename="sakura-14b-qwen3-v1.5-iq4xs.gguf",
        size_bytes=8_180_361_440,
        magic=b"GGUF",
        ext_filter="GGUF 模型 (*.gguf)",
    ),
    # 与同机 subgen 项目声明的 repo/文件名保持完全一致，两个项目共用 F:\hf_cache 下的同一份权重。
    # 改这里前先确认 subgen 那边（src/translate/mod.rs）用的是不是同一个文件名，否则会各下一份。
    "llm": ModelSpec(
        key="llm",
        label="通用本地翻译（多语言）",
        purpose="非日文漫画翻译",
        # Qwen3 没有官方 -Instruct-2507 纯指令刷新版本（那一波刷新只出了 4B/30B-A3B/235B-A22B），
        # 14B 只有原始混合思考/非思考版，靠 GeneralLLMTranslator 的 system_prompt 追加 /no_think
        # 关闭思考模式（translate.py 的 _LLM_SYS_TMPL），并在 _call 里兜底剥掉可能漏出的 <think> 块。
        # 这是默认推荐的下载项，不是唯一允许的文件——size_bytes 故意留 0（按 ModelSpec 的既有
        # 约定，见上面字段注释），换成别的通用指令 GGUF 模型（比如想要更快的 4B、或更平衡的
        # MoE 模型）时直接在「模型管理」页「定位…」绑定过去即可，不需要改这份 manifest、
        # 不需要重新打包 EXE。resolve_status/validate_candidate 只查 magic 头确认是 GGUF 文件，
        # 不再拿这里的具体某个模型的字节数卡死绑定——2026-08-22 实测吃过这个亏：换个模型
        # 要改代码+重新打包+清绑定，三步缺一都会导致"改了却没生效"或"报文件大小异常"，
        # 详见 comic-gen 项目 memory 的 translation-model-4b-to-14b.md。
        repo="unsloth/Qwen3-14B-GGUF",
        kind="llm_gguf",
        layout=LAYOUT_FILE,
        approx_size="~8.4GB",
        required=False,
        filename="Qwen3-14B-Q4_K_M.gguf",
        size_bytes=0,
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
