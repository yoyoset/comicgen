"""单次翻译任务的请求与结果。Qt-free，供 UI 与测试共用。"""
from __future__ import annotations

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


@dataclass
class PageFailure:
    image: str
    stage: str  # detect | ocr | translate | render
    message: str


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

        Page.error 形如 "检测失败: xxx" / "OCR 失败: xxx" / "翻译失败: xxx" / "抹字渲染失败: xxx"，
        取冒号前的中文短语作为 stage 标签（直接可读，无需再翻译成阶段名给用户看）。
        """
        total = len(pages)
        failures: list[PageFailure] = []
        for p in pages:
            if getattr(p, "error", None):
                stage = p.error.split(":", 1)[0].strip() if ":" in p.error else "未知阶段"
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
