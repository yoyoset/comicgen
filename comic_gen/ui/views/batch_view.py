"""批量队列页：纯视图。一次加入多个任务、顺序跑完、逐个查看结果，全程零模态弹窗。

和翻译页的分工：翻译页是一次"单任务"的主操作面板；这里是一次"一组任务"的主操作面板。
两者共用同一个 Runner（模型缓存跨入口复用），MainWindow 里互相拦 busy，不会同时跑。
关键行为差异在 MainWindow/批控制器：任务结束不弹任何对话框，状态写回本页表格，
下个任务自动开始——"睡一觉回来全部处理完"不需要人守着点确认框。

纯视图：只建控件、暴露取值/信号、接收 MainWindow 推进来的队列快照。不碰
BatchController/线程/Config，和 translate_view.py 同样的分工。
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from ...app.batch_queue import (
    QUEUE_STATUS_CANCELLED,
    QUEUE_STATUS_FAILED,
    QUEUE_STATUS_OK,
    QUEUE_STATUS_PARTIAL,
    QUEUE_STATUS_PENDING,
    QUEUE_STATUS_RUNNING,
    BatchJob,
)
from ...pipeline.translate import ENGINE_RECOMMENDATION

# 与翻译页共用的同一个选项来源：源语言/引擎/排版方向的列表一旦两处各维护一份就必然
# 漂移（加一种语言只改一处），这里直接引用翻译页的模块常量，单一数据源。
from .translate_view import _DIRS, _ENGINES, _LANGS

# 状态 → 显示文案 / 前景色。色码与状态栏文案同一套，让"排队/运行/完成/部分完成/失败/
# 取消"在一张表里一目了然，不用靠读英文状态值猜。
_STATUS_TEXT = {
    QUEUE_STATUS_PENDING: "排队中",
    QUEUE_STATUS_RUNNING: "运行中",
    QUEUE_STATUS_OK: "完成",
    QUEUE_STATUS_PARTIAL: "部分完成",
    QUEUE_STATUS_FAILED: "失败",
    QUEUE_STATUS_CANCELLED: "已取消",
}
_STATUS_COLOR = {
    QUEUE_STATUS_PENDING: "#000000",
    QUEUE_STATUS_RUNNING: "#1565c0",
    QUEUE_STATUS_OK: "#2e7d32",
    QUEUE_STATUS_PARTIAL: "#ef6c00",
    QUEUE_STATUS_FAILED: "#c62828",
    QUEUE_STATUS_CANCELLED: "#757575",
}


def status_text(status: str) -> str:
    return _STATUS_TEXT.get(status, status)


def status_color(status: str) -> str:
    return _STATUS_COLOR.get(status, "#000000")


class BatchView(QtWidgets.QWidget):
    addFilesRequested = QtCore.Signal()
    addFolderRequested = QtCore.Signal()
    removeSelectedRequested = QtCore.Signal()
    clearFinishedRequested = QtCore.Signal()
    startRequested = QtCore.Signal()
    stopRequested = QtCore.Signal()
    openOutputRequested = QtCore.Signal(object)  # BatchJob（选中行）
    reviewRequested = QtCore.Signal(object)      # BatchJob（选中行）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._jobs: list[BatchJob] = []
        self._batch_running = False   # 本队列自己是否正在执行
        self._blocked = False         # 单任务正在跑，队列入口整体不可用
        self._last_browse_dir = ""
        self._build()

    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(6)

        # ---- 与翻译页一致的批量运行参数 ----
        sel = QtWidgets.QWidget()
        sel_lay = QtWidgets.QHBoxLayout(sel)
        sel_lay.setContentsMargins(0, 0, 0, 0)
        sel_lay.setSpacing(6)
        sel_lay.addWidget(QtWidgets.QLabel("源语言"))
        self.lang_combo = QtWidgets.QComboBox()
        for n, v in _LANGS:
            self.lang_combo.addItem(n, v)
        self.lang_combo.currentIndexChanged.connect(self._recommend_engine)
        sel_lay.addWidget(self.lang_combo)
        sel_lay.addWidget(QtWidgets.QLabel("翻译引擎"))
        self.engine_combo = QtWidgets.QComboBox()
        for n, v in _ENGINES:
            self.engine_combo.addItem(n, v)
        sel_lay.addWidget(self.engine_combo)
        sel_lay.addWidget(QtWidgets.QLabel("排版方向"))
        self.dir_combo = QtWidgets.QComboBox()
        for n, v in _DIRS:
            self.dir_combo.addItem(n, v)
        sel_lay.addWidget(self.dir_combo)
        self.engine_hint = QtWidgets.QLabel("")
        self.engine_hint.setStyleSheet("color: gray;")
        sel_lay.addWidget(self.engine_hint)
        sel_lay.addStretch(1)
        root.addWidget(sel)
        self._recommend_engine()

        # ---- 队列操作 ----
        btns = QtWidgets.QHBoxLayout()
        btns.setSpacing(6)
        self.add_files_btn = QtWidgets.QPushButton("添加文件…")
        self.add_files_btn.clicked.connect(self.addFilesRequested)
        btns.addWidget(self.add_files_btn)
        self.add_folder_btn = QtWidgets.QPushButton("添加文件夹…")
        self.add_folder_btn.clicked.connect(self.addFolderRequested)
        btns.addWidget(self.add_folder_btn)
        btns.addSpacing(12)
        self.remove_btn = QtWidgets.QPushButton("移除选中")
        self.remove_btn.setToolTip("移除选中的任务（运行中的不能移除，先停止队列）")
        self.remove_btn.clicked.connect(self.removeSelectedRequested)
        btns.addWidget(self.remove_btn)
        self.clear_finished_btn = QtWidgets.QPushButton("清空已完成")
        self.clear_finished_btn.setToolTip("把已结束（完成/部分完成/失败/取消）的任务从列表里清掉")
        self.clear_finished_btn.clicked.connect(self.clearFinishedRequested)
        btns.addWidget(self.clear_finished_btn)
        btns.addStretch(1)
        self.start_btn = QtWidgets.QPushButton("开始队列")
        self.start_btn.setToolTip("顺序跑完队列里的任务；失败/取消的任务会从断点继续重跑")
        self.start_btn.clicked.connect(self.startRequested)
        btns.addWidget(self.start_btn)
        self.stop_btn = QtWidgets.QPushButton("停止")
        self.stop_btn.setToolTip("停止队列：当前任务在页边界停止，之后不再自动推进，排队中的任务保留")
        self.stop_btn.clicked.connect(self.stopRequested)
        btns.addWidget(self.stop_btn)
        root.addLayout(btns)

        # ---- 进度：第 X/Y 个任务 + 当前任务进度条 + 描述 ----
        prog = QtWidgets.QHBoxLayout()
        prog.setSpacing(6)
        self.position_lbl = QtWidgets.QLabel("共 0 个任务")
        self.position_lbl.setStyleSheet("color: gray;")
        prog.addWidget(self.position_lbl)
        self.progress_bar = QtWidgets.QProgressBar()
        prog.addWidget(self.progress_bar, 1)
        self.progress_lbl = QtWidgets.QLabel("")
        self.progress_lbl.setStyleSheet("color: gray;")
        prog.addWidget(self.progress_lbl)
        root.addLayout(prog)

        # ---- 任务列表 ----
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["状态", "输入", "输出", "结果"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._refresh_controls)
        root.addWidget(self.table, 1)

        # ---- 选中行的动作 + 内联提示（批量不弹模态框，用这行字代替） ----
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self.open_output_btn = QtWidgets.QPushButton("打开输出目录")
        self.open_output_btn.setToolTip("在文件管理器中打开选中任务的输出位置")
        self.open_output_btn.clicked.connect(self._on_open_output_clicked)
        row.addWidget(self.open_output_btn)
        self.review_btn = QtWidgets.QPushButton("校对…")
        self.review_btn.setToolTip("打开选中任务的翻译工作目录进行校对（不需要重新定位）")
        self.review_btn.clicked.connect(self._on_review_clicked)
        row.addWidget(self.review_btn)
        self.notice_lbl = QtWidgets.QLabel("")
        self.notice_lbl.setStyleSheet("color: #b8860b;")
        row.addWidget(self.notice_lbl, 1)
        root.addLayout(row)

        # ---- 日志 ----
        log_panel = QtWidgets.QWidget()
        lp = QtWidgets.QVBoxLayout(log_panel)
        lp.setContentsMargins(0, 0, 0, 0)
        lp.addWidget(QtWidgets.QLabel("批量日志"))
        self.log_edit = QtWidgets.QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(4000)
        lp.addWidget(self.log_edit, 1)
        lp.addWidget(QtWidgets.QLabel("此面板日志最多保留最近 4000 条，完整日志见各任务工作目录下的 run.log"))
        root.addWidget(log_panel, 1)

        self._refresh_controls()

    # ------------------------------------------------------------ 取值（供 MainWindow 读写）
    def default_dir(self) -> str:
        return self._last_browse_dir

    def set_last_browse_dir(self, d: str):
        self._last_browse_dir = d or ""

    def selected_job(self) -> BatchJob | None:
        if not self._jobs:
            return None
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        if 0 <= row < len(self._jobs):
            return self._jobs[row]
        return None

    def _recommend_engine(self):
        lang = self.lang_combo.currentData()
        rec = ENGINE_RECOMMENDATION.get(lang, "llm")
        for i in range(self.engine_combo.count()):
            if self.engine_combo.itemData(i) == rec:
                self.engine_combo.setCurrentIndex(i)
                break
        label = next((n for n, v in _ENGINES if v == rec), rec)
        self.engine_hint.setText(f"该语言默认使用：{label}")

    # ------------------------------------------------------------ 供 MainWindow 推数据进来
    def set_jobs(self, jobs: list[BatchJob]):
        prev = self.selected_job()
        prev_input = prev.input_path if prev else None
        self._jobs = list(jobs)
        self.table.blockSignals(True)
        self.table.clearContents()
        self.table.clearSpans()
        self.table.setRowCount(0)
        if not self._jobs:
            self.table.setRowCount(1)
            item = QtWidgets.QTableWidgetItem(
                "队列为空——点击「添加文件…」或「添加文件夹…」把漫画加进来，然后点「开始队列」。"
            )
            item.setFlags(QtCore.Qt.NoItemFlags)
            self.table.setItem(0, 0, item)
            self.table.setSpan(0, 0, 1, 4)
        else:
            self.table.setRowCount(len(self._jobs))
            for row, job in enumerate(self._jobs):
                self._set_job_row(row, job)
            if prev_input is not None:
                for row, job in enumerate(self._jobs):
                    if job.input_path == prev_input:
                        self.table.selectRow(row)
                        break
        self.table.blockSignals(False)
        self._refresh_controls()

    def _set_job_row(self, row: int, job: BatchJob):
        status_item = QtWidgets.QTableWidgetItem(status_text(job.status))
        status_item.setForeground(QtGui.QBrush(QtGui.QColor(status_color(job.status))))
        self.table.setItem(row, 0, status_item)
        self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(job.input_path))
        self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(job.display_output))
        self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(job.summary or ""))

    def set_batch_running(self, running: bool):
        self._batch_running = running
        self._refresh_controls()

    def set_blocked(self, blocked: bool):
        """单任务运行中：队列的增删/开始入口整体禁用，避免用户对着灰态按钮以为队列坏了。"""
        self._blocked = blocked
        self._refresh_controls()

    def set_position_text(self, text: str):
        self.position_lbl.setText(text)

    def set_progress(self, fraction: float, desc: str = ""):
        self.progress_bar.setValue(int(max(0.0, min(1.0, fraction)) * 100))
        self.progress_lbl.setText(desc)

    def clear_progress(self):
        self.progress_bar.setValue(0)
        self.progress_lbl.setText("")

    def set_notice(self, text: str):
        self.notice_lbl.setText(text)

    def append_log(self, msg: str):
        self.log_edit.appendPlainText(msg)

    def clear_log(self):
        self.log_edit.clear()

    # ------------------------------------------------------------ 内部控制
    def _refresh_controls(self):
        can_edit = not self._batch_running and not self._blocked
        self.add_files_btn.setEnabled(can_edit)
        self.add_folder_btn.setEnabled(can_edit)
        self.clear_finished_btn.setEnabled(can_edit)
        self.start_btn.setEnabled(can_edit)
        self.stop_btn.setEnabled(self._batch_running)
        job = self.selected_job()
        has_selection = job is not None
        self.remove_btn.setEnabled(
            can_edit and has_selection and job.status != QUEUE_STATUS_RUNNING
        )
        has_output = has_selection and bool(job.resolved_output or job.output_path)
        self.open_output_btn.setEnabled(has_selection and has_output)
        # 校对要加载模型（单页重渲染），批量正在跑 GPU 时不能开——但打开输出目录无害，保留。
        self.review_btn.setEnabled(has_selection and has_output and not self._batch_running)

    def _on_open_output_clicked(self):
        job = self.selected_job()
        if job is not None:
            self.openOutputRequested.emit(job)

    def _on_review_clicked(self):
        job = self.selected_job()
        if job is not None:
            self.reviewRequested.emit(job)
