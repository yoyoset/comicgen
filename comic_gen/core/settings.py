"""持久化用户偏好（AppSettings）+ 唯一的 Config 组装点。

Qt-free：路径解析用纯 Python（不依赖 QStandardPaths），可脱离 PySide6 单测。
持久化的是"跨任务不变的偏好"（模型目录/设备/DPI/字体/窗口位置等）；
"本次任务参数"（输入/输出/源语言/引擎/方向）属于 core.job.JobRequest，不在此处。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Config
from .job import JobRequest

_APP_NAME = "comic-gen"


def default_config_dir() -> Path:
    """跨会话配置目录。冻结/未冻结、Windows/其它平台均落用户目录，不落安装目录。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / _APP_NAME


def default_settings_path() -> Path:
    return default_config_dir() / "settings.json"


@dataclass
class AppSettings:
    # ---- 模型与运行环境 ----
    model_dir: str = ""  # 留空 = 用全局 HF 缓存（冻结时默认 exe 同级 models/hf）
    device: str = "cuda"
    sakura_filename: str = ""
    llm_filename: str = ""
    # ---- 渲染 ----
    pdf_dpi: int = 150
    font_path: str = r"C:\Windows\Fonts\msyh.ttc"
    min_font_size: int = 10
    max_font_size: int = 48
    # ---- 检测 ----
    det_threshold: float = 0.3
    # ---- 目标语言（当前仅简体中文，预留） ----
    target_lang: str = "简体中文"
    # ---- 记忆 ----
    last_input_dir: str = ""
    last_output_dir: str = ""
    window_geometry_hex: str = ""  # QMainWindow.saveGeometry() 的十六进制串，UI 层负责编解码

    # ------------------------------------------------------------ 持久化
    @classmethod
    def load(cls, path: Path | None = None) -> "AppSettings":
        path = path or default_settings_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    def save(self, path: Path | None = None) -> None:
        path = path or default_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------ 唯一的 Config 组装点
    def to_config(self, request: JobRequest) -> Config:
        """合并"本次任务参数"(JobRequest)与"持久化偏好"(self)为流水线运行时 Config。

        这是全项目唯一应该构造 Config 的地方，避免此前 _on_start/_on_rerender
        两处各拼一份且字段遗漏不一致的问题。
        """
        return Config(
            translator=request.engine,
            source_lang=request.source_lang,
            target_lang=self.target_lang,
            sakura_filename=self.sakura_filename,
            llm_filename=self.llm_filename,
            det_threshold=self.det_threshold,
            direction=request.direction,
            font_path=self.font_path,
            min_font_size=self.min_font_size,
            max_font_size=self.max_font_size,
            device=self.device,
            pdf_dpi=self.pdf_dpi,
        )
