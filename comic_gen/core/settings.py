"""持久化用户偏好（AppSettings）+ 唯一的 Config 组装点。

Qt-free：路径解析用 core.paths（不依赖 QStandardPaths），可脱离 PySide6 单测。
持久化的是"跨任务不变的偏好"（模型目录/设备/DPI/字体/窗口位置/每个模型的路径绑定等）；
"本次任务参数"（输入/输出/源语言/引擎/方向）属于 core.job.JobRequest，不在此处。
"""
from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .config import Config
from .job import JobRequest
from .paths import legacy_settings_path, settings_path
from .utils import atomic_write_text


@dataclass
class AppSettings:
    # ---- 模型与运行环境 ----
    model_dir: str = ""  # 留空 = 用全局 HF 缓存
    model_files: dict[str, str] = field(default_factory=dict)  # manifest key -> 绝对路径绑定
    setup_completed: bool = False  # 首次运行弹窗是否已走过（无论用户选了什么，都算走过）
    device: str = "cuda"
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
    def parse_or_recover(cls, text: str) -> tuple["AppSettings", list[str]]:
        """纯函数，不碰文件系统：解析 JSON 文本为 AppSettings，返回 (结果, 警告列表)。

        逐字段兜底而不是整份重置：某个字段类型不对，只让那个字段回落到默认值并记一条警告，
        其余字段（尤其是 model_dir/model_files，可能对应几十 GB 的共享缓存）不受牵连。
        只有整份 JSON 都解析不出字典时才整体回落到默认值。
        """
        warnings: list[str] = []
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("顶层不是对象")
        except Exception as e:
            return cls(), [f"配置文件解析失败，已使用默认设置：{e}"]

        defaults = cls()
        known = {f.name: f for f in fields(cls)}
        kwargs: dict = {}
        for name, f in known.items():
            if name not in data:
                continue
            value = data[name]
            expected_type = type(getattr(defaults, name))
            if not isinstance(value, expected_type):
                warnings.append(f"字段 {name} 类型不对（期望 {expected_type.__name__}），已用默认值")
                continue
            kwargs[name] = value

        result = cls(**kwargs)
        result = _migrate_legacy_filenames(result, data, warnings)
        return result, warnings

    @classmethod
    def load(cls, path: Path | None = None) -> "AppSettings":
        path = path or settings_path()
        if not path.exists():
            legacy = legacy_settings_path()
            if legacy.exists():
                migrate_legacy_settings(path, legacy)
            else:
                return cls()

        if not path.exists():
            return cls()

        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return cls()

        result, warnings = cls.parse_or_recover(text)
        if warnings:
            try:
                shutil.copy(path, path.with_name(path.name + ".bak"))
            except OSError:
                pass
        return result

    def save(self, path: Path | None = None) -> None:
        path = path or settings_path()
        # 原子写入：设置文件被进程杀在半途会损坏，下次启动 parse_or_recover 只能整份
        # 回退默认值（model_dir/model_files 等几十 GB 指向的绑定全丢）。见 core.utils.atomic_write_text。
        atomic_write_text(path, json.dumps(asdict(self), ensure_ascii=False, indent=2))

    # ------------------------------------------------------------ 唯一的 Config 组装点
    def to_config(self, request: JobRequest) -> Config:
        """合并"本次任务参数"(JobRequest)与"持久化偏好"(self)为流水线运行时 Config。

        这是全项目唯一应该构造 Config 的地方。model_paths 传的是 dict 的副本——
        任务开始后再改 settings.model_files 不应该影响已经在跑的这次任务
        （JobController/QThread 场景下，设置随时可能被 UI 线程改动）。
        """
        return Config(
            translator=request.engine,
            source_lang=request.source_lang,
            target_lang=self.target_lang,
            model_paths=dict(self.model_files),
            det_threshold=self.det_threshold,
            direction=request.direction,
            font_path=self.font_path,
            min_font_size=self.min_font_size,
            max_font_size=self.max_font_size,
            device=self.device,
            pdf_dpi=self.pdf_dpi,
        )


def _migrate_legacy_filenames(result: AppSettings, data: dict, warnings: list[str]) -> AppSettings:
    """旧版 sakura_filename/llm_filename 迁移进 model_files。

    绝对路径存在 → 迁移；裸文件名（旧版"留空自动挑量化"语义下的部分填写）信息量不够，
    丢弃并提示——"定位…"流程能让用户重新指认，比硬凑一个大概率错的路径更安全。
    """
    for old_key, new_key in (("sakura_filename", "sakura"), ("llm_filename", "llm")):
        value = data.get(old_key)
        if not value or new_key in result.model_files:
            continue
        if Path(value).is_absolute():
            result.model_files[new_key] = value
            warnings.append(f"已将旧设置 {old_key} 迁移为模型路径绑定")
        else:
            warnings.append(f"旧设置 {old_key}={value!r} 不是完整路径，已丢弃，请到模型管理页重新指定")
    return result


def migrate_legacy_settings(new_path: Path, legacy_path: Path) -> bool:
    """把旧位置（%LOCALAPPDATA%）的配置复制到新位置（exe 同级）。只复制，不删旧文件。"""
    try:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(legacy_path, new_path)
        return True
    except OSError:
        return False
