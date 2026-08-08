"""批量队列执行控制器：顺序驱动共享 Runner 跑完队列里的每个任务，自动推进。

与 JobController 的分工：JobController 管"单任务生命周期"，这里管"一组任务怎么排着跑"。
两者共用同一个 Runner（模型缓存跨入口复用），且必须互斥（MainWindow 里互相拦 busy）——
同一块 GPU 上不能同时跑两个任务。这里的执行复用 PipelineWorker（同样的线程 + 信号），
关键差异是：任务结束不弹任何模态框，状态写回队列，下个任务自动开始——"睡一觉回来
全部处理完"不需要人盯着点确认框。
"""
from __future__ import annotations

import threading
from typing import Callable

from PySide6 import QtCore

from ..app.batch_queue import (
    QUEUE_STATUS_CANCELLED,
    QUEUE_STATUS_FAILED,
    QUEUE_STATUS_OK,
    QUEUE_STATUS_PARTIAL,
    QUEUE_STATUS_PENDING,
    QUEUE_STATUS_RUNNING,
    BatchJob,
    BatchQueue,
)
from ..core.job import JobReport
from ..pipeline.runner import Runner
from .workers import PipelineWorker

STATE_IDLE = "idle"
STATE_RUNNING = "running"


class BatchController(QtCore.QObject):
    queueChanged = QtCore.Signal()          # 列表/状态变化，视图据此刷新
    jobStarted = QtCore.Signal(int, int)    # (当前任务下标 0 基, 总数)
    jobProgress = QtCore.Signal(float, str) # 当前任务进度 (0~1, 描述)
    stateChanged = QtCore.Signal(str)       # idle / running
    queueFinished = QtCore.Signal()         # 队列全部结束
    log = QtCore.Signal(str)

    def __init__(self, runner: Runner, parent=None):
        super().__init__(parent)
        self.runner = runner
        self.queue = BatchQueue()
        self._thread: QtCore.QThread | None = None
        self._worker: PipelineWorker | None = None
        self._cancel_event = threading.Event()
        self._prepare: Callable[[BatchJob], tuple] | None = None  # job -> (JobRequest, Config)
        self._current_index = -1
        self._stopped = False

    # ------------------------------------------------------------ 状态
    @property
    def is_busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def _set_state(self, state: str):
        self.stateChanged.emit(state)

    # ------------------------------------------------------------ 队列操作
    def enqueue(self, input_path: str, output_path: str | None = None) -> str | None:
        err = self.queue.add(input_path, output_path)
        if err is None:
            self.queueChanged.emit()
        return err

    def remove(self, job: BatchJob) -> str | None:
        err = self.queue.remove(job)
        if err is None:
            self.queueChanged.emit()
        return err

    def clear_finished(self) -> int:
        n = self.queue.clear_finished()
        if n:
            self.queueChanged.emit()
        return n

    def clear_all(self) -> None:
        """清空整个队列（含已结束的）。运行中不可清（先停止）。"""
        if self.is_busy:
            return
        self.queue = BatchQueue()
        self.queueChanged.emit()

    # ------------------------------------------------------------ 执行
    def start(self, prepare: Callable[[BatchJob], tuple]):
        """开始/继续队列。prepare(job) 返回 (JobRequest, Config)：
        MainWindow 用它把队列任务 + 批量页的引擎/语言选择 + 持久化设置合成一次运行的配置。
        已失败/部分完成/取消的任务会被打回排队中（Runner 从各自断点续跑）。
        """
        if self.is_busy:
            return
        self._prepare = prepare
        self._stopped = False
        self.queue.reset_for_rerun()
        self._set_state(STATE_RUNNING)
        self._run_next()

    def stop(self) -> None:
        """停止队列：当前任务在页边界协作式取消，之后不再自动推进。排队中的任务保留。"""
        if not self.is_busy:
            return
        self._stopped = True
        self._cancel_event.set()

    def _run_next(self) -> None:
        job = self.queue.next_pending()
        if job is None:
            self._set_state(STATE_IDLE)
            self.queueFinished.emit()
            return
        job.status = QUEUE_STATUS_RUNNING
        self._current_index = self.queue.jobs().index(job)
        self.queueChanged.emit()
        self.jobStarted.emit(self._current_index, len(self.queue.jobs()))

        request, cfg = self._prepare(job)
        self.runner.cfg = cfg
        self._cancel_event.clear()
        self._thread = QtCore.QThread()
        self._worker = PipelineWorker(self.runner, request, self._cancel_event)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.jobProgress)
        self._worker.log.connect(self.log)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.start()

    def _on_worker_finished(self, result) -> None:
        if self._current_index < 0 or self._current_index >= len(self.queue.jobs()):
            return
        job = self.queue.jobs()[self._current_index]
        if isinstance(result, JobReport):
            status = {
                "ok": QUEUE_STATUS_OK,
                "partial": QUEUE_STATUS_PARTIAL,
                "cancelled": QUEUE_STATUS_CANCELLED,
                "failed": QUEUE_STATUS_FAILED,
            }.get(result.status, QUEUE_STATUS_FAILED)
            job.status = status
            job.summary = result.summary
            if result.output_path is not None:
                job.resolved_output = str(result.output_path)
        else:
            job.status = QUEUE_STATUS_FAILED
            job.summary = str(result)
        self.queueChanged.emit()

        if self._stopped:
            self._stopped = False
            self._set_state(STATE_IDLE)
            return
        self._run_next()
