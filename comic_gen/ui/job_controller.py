"""任务生命周期控制器：唯一持有流水线线程，强制同时只跑一个任务。

视图不直接碰 Runner/QThread；只发信号请求 start/cancel，观察 state/progress/finished。
这是 MainWindow 从"god object"瘦身出来的核心——状态机与线程编排收敛在这一个类里。
"""
from __future__ import annotations

import threading

from PySide6 import QtCore

from ..core.config import Config
from ..core.job import JobRequest
from ..pipeline.runner import Runner
from .workers import PipelineWorker

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_CANCELLING = "cancelling"


class JobController(QtCore.QObject):
    stateChanged = QtCore.Signal(str)
    progress = QtCore.Signal(float, str)
    log = QtCore.Signal(str)
    loadProgress = QtCore.Signal(int, int)
    finished = QtCore.Signal(object)  # JobReport 或异常字符串

    def __init__(self, runner: Runner | None = None):
        super().__init__()
        self._state = STATE_IDLE
        # 共享的 Runner：和批量队列（batch_controller.BatchController）共用同一个实例，
        # 让单任务和批量跨两种入口复用模型缓存，不会各加载一份几十 GB 的模型。
        # 两者互斥执行（MainWindow 里互相拦 busy），不会同时跑两个任务。
        self._runner = runner
        self._thread: QtCore.QThread | None = None
        self._worker: PipelineWorker | None = None
        self._cancel_event: threading.Event | None = None

    @property
    def state(self) -> str:
        return self._state

    def is_busy(self) -> bool:
        return self._state != STATE_IDLE

    def start(self, request: JobRequest, cfg: Config) -> None:
        if self.is_busy():
            raise RuntimeError("已有任务在运行，请先停止或等待完成。")
        if self._runner is None:
            self._runner = Runner(cfg)
        else:
            self._runner.cfg = cfg

        self._cancel_event = threading.Event()
        self._set_state(STATE_RUNNING)

        self._thread = QtCore.QThread()
        self._worker = PipelineWorker(self._runner, request, self._cancel_event)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self.log)
        self._worker.progress.connect(self.progress)
        self._worker.loadProgress.connect(self.loadProgress)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.start()

    def cancel(self) -> None:
        if not self.is_busy() or self._cancel_event is None:
            return
        self._cancel_event.set()
        self._set_state(STATE_CANCELLING)

    def shutdown(self, wait_ms: int = 3000) -> None:
        """窗口关闭时调用：请求取消并等待线程收尾，避免拖着后台任务强退。"""
        if self._thread and self._thread.isRunning():
            if self._cancel_event:
                self._cancel_event.set()
            self._thread.quit()
            self._thread.wait(wait_ms)

    def _on_worker_finished(self, result) -> None:
        self._set_state(STATE_IDLE)
        self.finished.emit(result)

    def _set_state(self, state: str) -> None:
        self._state = state
        self.stateChanged.emit(state)
