"""翻译页：纯视图。只建 widget、暴露取值/信号，不碰 Runner/Config/线程。

"少改项"（模型目录/设备/DPI/字体/检测阈值）不在这里——它们在 settings_view，
这里只留跑一次任务必需的项，参照成熟软件"主操作面板从不塞配置项"的惯例。
"""
from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ...core.job import JobReport, JobRequest
from ...core.utils import natural_key
from ...pipeline.translate import ENGINE_RECOMMENDATION

_LANGS = [("日文", "ja"), ("英文", "en"), ("简体中文", "zh"), ("其它", "other")]
_ENGINES = [("Sakura（本地，日文）", "sakura"), ("通用本地 LLM（多语言）", "llm"), ("Mock（测试）", "mock")]
_DIRS = [("自动", "auto"), ("横排", "horizontal"), ("竖排", "vertical")]
_PREVIEW_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_PREVIEW_PAGE_SIZE = 24


def list_preview_images(output: str | None) -> list[Path]:
    """列出输出产物目录 translated/ 下的图片，自然排序（page2 在 page10 前）。

    纯函数，不碰 Qt：返回全部路径，是否分页、每页几张由视图决定——493 页的任务
    一次性把几百张缩略图全塞进内存/IconMode 列表会拖慢甚至卡死界面（T4 实测）。
    """
    if not output:
        return []
    d = Path(output)
    if d.is_file():
        d = d.parent / d.stem
    trans = d / "translated"
    if not trans.exists():
        return []
    return sorted(
        (p for p in trans.rglob("*") if p.suffix.lower() in _PREVIEW_IMAGE_EXTS),
        key=lambda p: natural_key(str(p.relative_to(trans))),
    )


class TranslateView(QtWidgets.QWidget):
    startRequested = QtCore.Signal()
    stopRequested = QtCore.Signal()
    rerenderRequested = QtCore.Signal()
    reviewRequested = QtCore.Signal()  # 打开最近一次完成任务的校对
    openOutputRequested = QtCore.Signal()  # 在文件管理器中打开最近一次任务的输出

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_browse_dir = ""
        self._preview_images: list[Path] = []
        self._preview_offset = 0
        self._last_output: str | None = None
        self._running = False      # 单任务自己是否正在跑
        self._blocked = False      # 批量队列运行中，单任务入口整体禁用
        self._review_available = False
        self._build()

    def _build(self):
        lay = QtWidgets.QFormLayout()
        self.setLayout(lay)

        self.input_edit = QtWidgets.QLineEdit()
        lay.addRow("输入路径", self._input_row())

        self.output_edit = QtWidgets.QLineEdit()
        self.output_edit.setPlaceholderText("留空 = 输入同级 _translated")
        lay.addRow("输出路径", self._browse_row(self.output_edit, "save"))

        self.lang_combo = QtWidgets.QComboBox()
        for n, v in _LANGS:
            self.lang_combo.addItem(n, v)
        self.lang_combo.currentIndexChanged.connect(self._recommend_engine)
        lay.addRow("源语言", self.lang_combo)

        self.engine_combo = QtWidgets.QComboBox()
        for n, v in _ENGINES:
            self.engine_combo.addItem(n, v)
        lay.addRow("翻译引擎", self.engine_combo)

        self.engine_hint = QtWidgets.QLabel()
        self.engine_hint.setStyleSheet("color: gray;")
        lay.addRow("", self.engine_hint)

        self.dir_combo = QtWidgets.QComboBox()
        for n, v in _DIRS:
            self.dir_combo.addItem(n, v)
        lay.addRow("排版方向", self.dir_combo)

        self.start_btn = QtWidgets.QPushButton("开始翻译")
        self.start_btn.clicked.connect(self.startRequested)
        self.stop_btn = QtWidgets.QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stopRequested)
        self.rerender_btn = QtWidgets.QPushButton("重渲染")
        self.rerender_btn.setToolTip("编辑 pages.json 后重新渲染译文（需选择上次翻译的输出目录）")
        self.rerender_btn.clicked.connect(self.rerenderRequested)
        self.review_btn = QtWidgets.QPushButton("校对本次结果…")
        self.review_btn.setToolTip("打开最近一次完成任务的工作目录进行校对（无需重新定位）")
        self.review_btn.setEnabled(False)
        self.review_btn.clicked.connect(self.reviewRequested)
        br = QtWidgets.QHBoxLayout()
        br.addWidget(self.start_btn, 2)
        br.addWidget(self.stop_btn, 1)
        br.addWidget(self.rerender_btn, 1)
        br.addWidget(self.review_btn, 1)
        lay.addRow(br)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_lbl = QtWidgets.QLabel("")
        self.progress_lbl.setStyleSheet("color: gray;")
        prog_row = QtWidgets.QHBoxLayout()
        prog_row.addWidget(self.progress_bar, 1)
        prog_row.addWidget(self.progress_lbl)
        lay.addRow("进度", prog_row)

        self.log_edit = QtWidgets.QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        # 不加上限时，几百页任务的告警会无限常驻内存（T4 实测）：进程的内存只会涨不会
        # 回落。4000 条足够容纳一轮完整任务的核心信息，更早的仍在 <工作目录>/run.log。
        self.log_edit.setMaximumBlockCount(4000)

        log_panel = QtWidgets.QWidget()
        lp = QtWidgets.QVBoxLayout(log_panel)
        lp.setContentsMargins(0, 0, 0, 0)
        log_title = QtWidgets.QLabel("运行日志")
        lp.addWidget(log_title)
        lp.addWidget(self.log_edit, 1)
        lp.addWidget(QtWidgets.QLabel("此面板日志最多保留最近 4000 条，完整日志见工作目录下的 run.log"))

        preview_panel = QtWidgets.QWidget()
        pv = QtWidgets.QVBoxLayout(preview_panel)
        pv.setContentsMargins(0, 0, 0, 0)
        top_row = QtWidgets.QHBoxLayout()
        self.preview_lbl = QtWidgets.QLabel("成品预览")
        top_row.addWidget(self.preview_lbl)
        top_row.addStretch(1)
        self.open_out_btn = QtWidgets.QPushButton("打开输出目录")
        self.open_out_btn.setEnabled(False)
        self.open_out_btn.setToolTip("在文件管理器中打开最近一次任务的输出位置")
        self.open_out_btn.clicked.connect(self.openOutputRequested)
        top_row.addWidget(self.open_out_btn)
        self.prev_btn = QtWidgets.QPushButton("上一页")
        self.next_btn = QtWidgets.QPushButton("下一页")
        self.prev_btn.setEnabled(False)
        self.next_btn.setEnabled(False)
        self.prev_btn.clicked.connect(self._preview_prev)
        self.next_btn.clicked.connect(self._preview_next)
        top_row.addWidget(self.prev_btn)
        top_row.addWidget(self.next_btn)
        pv.addLayout(top_row)
        self.preview_list = QtWidgets.QListWidget()
        self.preview_list.setViewMode(QtWidgets.QListWidget.IconMode)
        self.preview_list.setIconSize(QtCore.QSize(160, 220))
        self.preview_list.setResizeMode(QtWidgets.QListWidget.Adjust)
        self.preview_list.itemDoubleClicked.connect(self._on_preview_open)
        pv.addWidget(self.preview_list, 1)

        sp = QtWidgets.QSplitter()
        sp.addWidget(log_panel)
        sp.addWidget(preview_panel)
        sp.setSizes([300, 600])
        lay.addRow(sp)

        self._recommend_engine()

    def _browse_row(self, edit, mode):
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        b = QtWidgets.QPushButton("浏览…")
        b.clicked.connect(lambda: self._pick(edit, mode))
        h.addWidget(b)
        return w

    def _input_row(self):
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.input_edit, 1)
        b_dir = QtWidgets.QPushButton("选文件夹…")
        b_dir.setToolTip("选漫画图片文件夹")
        b_dir.clicked.connect(lambda: self._pick(self.input_edit, "folder"))
        b_file = QtWidgets.QPushButton("选文件…")
        b_file.setToolTip("选 ZIP / CBZ / EPUB / PDF / RAR / CBR")
        b_file.clicked.connect(lambda: self._pick(self.input_edit, "open"))
        h.addWidget(b_dir)
        h.addWidget(b_file)
        return w

    def _pick(self, edit, mode):
        if mode == "folder":
            d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择文件夹", self.default_dir())
            if d:
                edit.setText(d)
        elif mode == "open":
            d, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "选择文件", self.default_dir(),
                "漫画文件 (*.zip *.cbz *.epub *.pdf *.rar *.cbr);;所有文件 (*.*)")
            if d:
                edit.setText(d)
        elif mode == "save":
            d, _ = QtWidgets.QFileDialog.getSaveFileName(self, "选择输出", edit.text() or self.default_dir(), "所有文件 (*.*)")
            if d:
                edit.setText(d)

    def default_dir(self) -> str:
        return self.input_edit.text().strip() or self._last_browse_dir

    def _recommend_engine(self):
        lang = self.lang_combo.currentData()
        rec = ENGINE_RECOMMENDATION.get(lang, "llm")
        for i in range(self.engine_combo.count()):
            if self.engine_combo.itemData(i) == rec:
                self.engine_combo.setCurrentIndex(i)
                break
        label = next((n for n, v in _ENGINES if v == rec), rec)
        self.engine_hint.setText(f"该语言默认使用：{label}")

    # ------------------------------------------------------------ 取值 / 赋值（供 MainWindow 读写）
    def build_request(self) -> JobRequest:
        return JobRequest(
            input_path=self.input_edit.text().strip().strip('"'),
            output_path=self.output_edit.text().strip().strip('"') or None,
            source_lang=self.lang_combo.currentData(),
            engine=self.engine_combo.currentData(),
            direction=self.dir_combo.currentData(),
            is_rerender=False,
        )

    def set_last_browse_dir(self, d: str):
        """启动时从设置恢复：只作为文件对话框的起始目录，不预填输入框（避免误用陈旧路径）。"""
        self._last_browse_dir = d or ""

    def set_running(self, running: bool):
        self._running = running
        self.stop_btn.setEnabled(running)
        if not running:
            # 任务结束/取消后清掉上一轮的阶段描述，避免"还停在 OCR 42/200"的残留误导
            self.progress_lbl.setText("")
        self._refresh_action_buttons()

    def set_blocked(self, blocked: bool):
        """批量队列运行中：单任务入口（开始/重渲染/校对）整体禁用。

        真正的互斥靠 MainWindow 各入口的 is_busy 拦截，这里只做视觉上的"此刻不能点"，
        避免用户对着可点的按钮点下去却被弹窗拒绝——两件事都要有。
        """
        self._blocked = blocked
        self._refresh_action_buttons()

    def _refresh_action_buttons(self):
        enabled = not self._running and not self._blocked
        self.start_btn.setEnabled(enabled)
        self.rerender_btn.setEnabled(enabled)
        self.review_btn.setEnabled(enabled and self._review_available)

    def set_review_available(self, available: bool):
        """任务成功/部分完成后点亮"校对本次结果"，开始新任务时由 MainWindow 关掉。"""
        self._review_available = available
        self._refresh_action_buttons()

    def append_log(self, msg: str):
        self.log_edit.appendPlainText(msg)

    def clear_log(self):
        self.log_edit.clear()

    def set_progress(self, fraction: float, desc: str = ""):
        self.progress_bar.setValue(int(max(0.0, min(1.0, fraction)) * 100))
        # 之前 desc 参数被丢掉了：用户只知道"在跑"，不知道卡在哪个阶段哪一页。
        # 这里同时显示"阶段 页码/总数 预计剩余 xx"，是"现在是什么状态"的关键反馈。
        self.progress_lbl.setText(desc)

    def show_result(self, report: JobReport):
        self.load_preview(report.output_path)

    def load_preview(self, out):
        self._last_output = out
        self._preview_images = list_preview_images(out)
        self._preview_offset = 0
        self._show_preview_page()

    def _show_preview_page(self):
        self.preview_list.clear()
        total = len(self._preview_images)
        if total == 0:
            self.preview_lbl.setText("成品预览")
            empty = QtWidgets.QListWidgetItem("（还没有成品，跑完一次任务后这里会显示预览）")
            empty.setFlags(QtCore.Qt.NoItemFlags)
            self.preview_list.addItem(empty)
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            self.open_out_btn.setEnabled(self._last_output is not None)
            return
        page_size = _PREVIEW_PAGE_SIZE
        page_no = self._preview_offset // page_size + 1
        page_count = (total + page_size - 1) // page_size
        self.preview_lbl.setText(f"成品预览（第 {page_no}/{page_count} 页，共 {total} 张，双击打开大图）")
        for p in self._preview_images[self._preview_offset:self._preview_offset + page_size]:
            it = QtWidgets.QListWidgetItem(QtGui.QIcon(str(p)), p.name)
            it.setData(QtCore.Qt.UserRole, str(p))
            self.preview_list.addItem(it)
        self.prev_btn.setEnabled(self._preview_offset > 0)
        self.next_btn.setEnabled(self._preview_offset + page_size < total)
        self.open_out_btn.setEnabled(True)

    def _preview_prev(self):
        if self._preview_offset > 0:
            self._preview_offset = max(0, self._preview_offset - _PREVIEW_PAGE_SIZE)
            self._show_preview_page()

    def _preview_next(self):
        if self._preview_offset + _PREVIEW_PAGE_SIZE < len(self._preview_images):
            self._preview_offset += _PREVIEW_PAGE_SIZE
            self._show_preview_page()

    def _on_preview_open(self, item):
        path = item.data(QtCore.Qt.UserRole)
        if path:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))
