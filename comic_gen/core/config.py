"""运行配置。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .manifest import MODEL_MANIFEST


@dataclass
class Config:
    # ---- 翻译（全部本地引擎，无需联网 API） ----
    translator: str = "sakura"  # sakura(本地，日译中，开箱即用) | llm(本地，多语言) | mock
    source_lang: str = "ja"  # ja | en | zh | other —— 决定 OCR 后端与推荐引擎
    target_lang: str = "简体中文"
    # 每个模型（manifest 键）到本地绝对路径的显式绑定，来自 AppSettings.model_files。
    # 未绑定的键走 core.model_locator 的自动搜索（本地缓存 → 联网下载兜底）。
    model_paths: dict[str, str] = field(default_factory=dict)
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
    # 全局 DPI：PDF 输入时按此渲染每页；图片输入（文件夹/ZIP/CBZ/RAR/CBR/EPUB）没有
    # 可靠的物理尺寸信息，用它换算出参考开本（B5）长边对应的像素数，长边超过这个数的图
    # 会先等比缩小再进流水线（见 pipeline/loader.py::target_long_edge_px），避免原始
    # 扫描图分辨率过高时 LaMa 抹字/渲染耗时爆炸（2026-08-22 实测：6307px 长边约
    # 4.4 分钟/页）。150 快且够用，300 画质好但慢，两种输入都受这一个值控制。
    pdf_dpi: int = 150
    # 任务完整成功后删除中间产物（translated/、解包原图、pages.json/job.json/run.log），
    # 只保留最终交付物，释放磁盘空间。勾选后该任务无法再校对/断点续跑/跨会话重渲染。
    cleanup_intermediate: bool = False
