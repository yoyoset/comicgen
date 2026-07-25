"""单次翻译任务的请求与结果。Qt-free，供 UI 与测试共用。"""
from __future__ import annotations

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
