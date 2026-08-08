"""批量队列：一组翻译任务的顺序编排数据模型。Qt-free，核心逻辑可独立单测。

为什么是顺序执行：同一块 GPU 上并行跑多个模型任务会显存不足（RTX 3060 12GB），
这是刻意的产品约束，不是实现偷懒。队列只管"下一个该跑谁"，真正的执行（Runner +
线程）由 ui/batch_controller.py 驱动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

QUEUE_STATUS_PENDING = "pending"      # 排队中
QUEUE_STATUS_RUNNING = "running"      # 运行中
QUEUE_STATUS_OK = "ok"                # 完成
QUEUE_STATUS_PARTIAL = "partial"      # 部分完成（有失败页）
QUEUE_STATUS_FAILED = "failed"        # 失败
QUEUE_STATUS_CANCELLED = "cancelled"  # 已取消（可续跑）

_RERUNNABLE = {QUEUE_STATUS_FAILED, QUEUE_STATUS_PARTIAL, QUEUE_STATUS_CANCELLED}


@dataclass
class BatchJob:
    """队列里的一个任务。input_path 是原始输入，output_path 为空时按默认规则推导。"""

    input_path: str
    output_path: str | None = None
    status: str = QUEUE_STATUS_PENDING
    summary: str = ""                       # JobReport.summary 或错误描述
    resolved_output: str | None = None      # 任务跑完后 JobReport.output_path 的解析值

    @property
    def display_output(self) -> str:
        return self.resolved_output or self.output_path or "（默认推导）"


class BatchQueue:
    """顺序任务队列。同一次开始里每个任务最多出现一次（按解析后的绝对路径去重）。"""

    def __init__(self):
        self._jobs: list[BatchJob] = []

    def jobs(self) -> list[BatchJob]:
        return list(self._jobs)

    def add(self, input_path: str, output_path: str | None = None) -> str | None:
        """加入队列。成功返回 None；拒绝返回人话原因（重复任务）。"""
        p = Path(input_path).resolve()
        for job in self._jobs:
            if Path(job.input_path).resolve() == p:
                return f"已在队列中：{input_path}"
        self._jobs.append(BatchJob(input_path=input_path, output_path=output_path))
        return None

    def remove(self, job: BatchJob) -> str | None:
        """移除一个还没开始运行的任务。成功返回 None；拒绝返回人话原因。"""
        if job.status == QUEUE_STATUS_RUNNING:
            return "正在运行的任务不能移除，请先停止队列"
        if job in self._jobs:
            self._jobs.remove(job)
            return None
        return "任务不在队列中"

    def next_pending(self) -> BatchJob | None:
        """按加入顺序返回下一个排队中的任务。"""
        for job in self._jobs:
            if job.status == QUEUE_STATUS_PENDING:
                return job
        return None

    def reset_for_rerun(self) -> int:
        """重新开始队列前，把失败/部分完成/已取消的任务打回排队中（已完成的保持不动）。

        打回后 Runner 从各自 pages.json 的断点续跑——已完成的页不会重做，只重试
        失败/未完成的页。返回被打回的任务数。
        """
        count = 0
        for job in self._jobs:
            if job.status in _RERUNNABLE:
                job.status = QUEUE_STATUS_PENDING
                job.summary = ""
                count += 1
        return count

    def clear_finished(self) -> int:
        """清掉所有已结束（非排队/运行）的任务，返回清掉的数量。"""
        before = len(self._jobs)
        self._jobs = [j for j in self._jobs if j.status in (QUEUE_STATUS_PENDING, QUEUE_STATUS_RUNNING)]
        return before - len(self._jobs)

    def counts(self) -> dict:
        """供界面显示队列概要：总数 / 已完成 / 失败 / 排队中。"""
        total = len(self._jobs)
        done = sum(1 for j in self._jobs if j.status in (QUEUE_STATUS_OK, QUEUE_STATUS_PARTIAL))
        failed = sum(1 for j in self._jobs if j.status in (QUEUE_STATUS_FAILED, QUEUE_STATUS_CANCELLED))
        pending = sum(1 for j in self._jobs if j.status in (QUEUE_STATUS_PENDING, QUEUE_STATUS_RUNNING))
        return {"total": total, "done": done, "failed": failed, "pending": pending}

    def has_work(self) -> bool:
        """队列里还有没有要跑的：排队中的，或失败/部分完成/已取消可重跑的。

        全队都是「完成」时点「开始队列」没有意义（已完成的任务不会被重跑）——
        UI 用这个判断是直接开始还是先给一句内联提示，避免"点开始却瞬间结束"的困惑。
        """
        return any(j.status == QUEUE_STATUS_PENDING or j.status in _RERUNNABLE for j in self._jobs)
