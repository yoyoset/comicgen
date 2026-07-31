"""单次翻译任务的请求与结果。Qt-free，供 UI 与测试共用。"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

JobStatus = Literal["ok", "partial", "failed", "cancelled"]


@dataclass
class JobRequest:
    """一次翻译/重渲染任务的可变参数（"常改项"）。

    模型目录/设备/DPI/字体等偏好不在此处——它们来自 AppSettings，
    经 AppSettings.to_config(request) 合并成最终 Config。
    """

    input_path: str
    output_path: str | None = None
    source_lang: str = "ja"
    engine: str = "sakura"
    direction: str = "auto"
    is_rerender: bool = False
    work_dir_override: str | None = None  # 仅重渲染的跨会话回退路径使用，见 resolve_pages_json/read_original_input


@dataclass
class PageFailure:
    image: str
    stage: str  # core.errors.Stage 的字符串值：detect | ocr | translate | render
    message: str


def _legacy_stage_from_message(message: str) -> str:
    """兼容没有 error_stage 字段的旧 pages.json：从"检测失败: xxx"这类固定格式的
    中文消息里切出阶段短语。"""
    return message.split(":", 1)[0].strip() if ":" in message else "未知阶段"


@dataclass
class JobReport:
    status: JobStatus
    output_path: Path | None = None
    total_pages: int = 0
    failed_pages: list[PageFailure] = field(default_factory=list)
    summary: str = ""

    @classmethod
    def from_pages(cls, pages: list, output_path: Path | None) -> "JobReport":
        """从 Runner 跑完后的 Page 列表汇总结果。

        阶段标签优先读 Page.error_stage——Runner 在对应阶段的 except 块里直接写入
        （core.errors.Stage 的字符串值），不再靠切开中文错误消息猜阶段。旧版
        pages.json 没有这个字段（读回来是空串）时，退回旧启发式："检测失败: xxx"
        这类固定格式，取冒号前的中文短语，保证读旧数据不报错、不丢阶段信息。
        """
        total = len(pages)
        failures: list[PageFailure] = []
        for p in pages:
            if getattr(p, "error", None):
                stage = getattr(p, "error_stage", "") or _legacy_stage_from_message(p.error)
                failures.append(PageFailure(image=p.image, stage=stage, message=p.error))

        if not failures:
            status: JobStatus = "ok"
            summary = f"完成，共 {total} 页。"
        elif len(failures) >= total and total > 0:
            status = "failed"
            summary = f"全部 {total} 页均失败，请查看日志。"
        else:
            status = "partial"
            summary = f"完成 {total - len(failures)}/{total} 页，{len(failures)} 页失败（详见下方列表）。"

        return cls(status=status, output_path=output_path, total_pages=total,
                    failed_pages=failures, summary=summary)

    @classmethod
    def cancelled(cls, pages: list) -> "JobReport":
        """用户主动停止。当前页已写完、pages.json 完好，可重跑续上。"""
        return cls(
            status="cancelled",
            output_path=None,
            total_pages=len(pages),
            failed_pages=[],
            summary="已停止。当前进度已保存，可重新运行以继续。",
        )


def build_job_snapshot(cfg, app_version: str, resolved_model_paths: dict, stages: dict) -> dict:
    """构造要写进 <work_dir>/job.json 的快照：产生这批结果的完整配置 + 各模型实际
    解析到的绝对路径 + 各阶段耗时。纯函数，不做任何 IO。

    之前 pages.json 只记了 input 一个字段，出问题后无法知道当时用的是哪个翻译
    引擎、检测阈值、字体、PDF DPI，或者某个模型实际是从哪个路径加载的——这是事后
    诊断的空白。cfg 用 dataclasses.asdict 展开，不需要 cfg 自己知道怎么序列化。
    """
    return {
        "config": dataclasses.asdict(cfg),
        "app_version": app_version,
        "resolved_model_paths": dict(resolved_model_paths),
        "stages": dict(stages),
    }


def resolve_pages_json(work_dir_or_output: Path) -> Path:
    """给定用户选的"翻译输出目录/文件"，定位其 pages.json 所在工作目录。

    文件夹输入 → 工作目录就是它自己；压缩包/PDF 输入 → 工作目录是 <输出文件名去扩展>/。
    与 Runner._work_dir 的目录命名规则保持一致。
    """
    p = Path(work_dir_or_output)
    candidate = p / "pages.json" if p.is_dir() else p.parent / p.stem / "pages.json"
    return candidate


def read_original_input(pages_json_path: Path) -> str | None:
    """从 pages.json 里读回最初翻译时记录的原始输入路径（Runner._save_pages 写入的 "input" 指纹）。

    跨会话重渲染场景用：用户只记得工作目录在哪，不记得当初输入的是哪个文件/文件夹，
    这里直接从产物里读回来，而不必让用户重新指认。
    """
    try:
        data = json.loads(Path(pages_json_path).read_text(encoding="utf-8"))
        return data.get("input")
    except Exception:
        return None
