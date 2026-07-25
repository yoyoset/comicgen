"""翻译页：纯视图。只建 widget、暴露取值/信号，不碰 Runner/Config/线程。

"少改项"（模型目录/设备/DPI/字体/检测阈值）不在这里——它们在 settings_view，
这里只留跑一次任务必需的项，参照成熟软件"主操作面板从不塞配置项"的惯例。
"""
from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ...core.job import JobReport, JobRequest
from ...pipeline.translate import ENGINE_RECOMMENDATION

_LANGS = [("日文", "ja"), ("英文", "en"), ("简体中文", "zh"), ("其它", "other")]
_ENGINES = [("Sakura（本地，日文）", "sakura"), ("通用本地 LLM（多语言）", "llm"), ("Mock（测试）", "mock")]
_DIRS = [("自动", "auto"), ("横排", "horizontal"), ("竖排", "vertical")]


class TranslateView(QtWidgets.QWidget):
    startRequested = QtCore.Signal()
    stopRequested = QtCore.Signal()
    rerenderRequested = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_browse_dir = ""
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
        br = QtWidgets.QHBoxLayout()
        br.addWidget(self.start_btn, 2)
        br.addWidget(self.stop_btn, 1)
        br.addWidget(self.rerender_btn, 1)
        lay.addRow(br)

        self.progress_bar = QtWidgets.QProgressBar()
        lay.addRow("进度", self.progress_bar)

        self.log_edit = QtWidgets.QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.preview_list = QtWidgets.QListWidget()
        self.preview_list.setViewMode(QtWidgets.QListWidget.IconMode)
        self.preview_list.setIconSize(QtCore.QSize(160, 220))
        self.preview_list.setResizeMode(QtWidgets.QListWidget.Adjust)
        sp = QtWidgets.QSplitter()
        sp.addWidget(self.log_edit)
        sp.addWidget(self.preview_list)
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
        self.start_btn.setEnabled(not running)
        self.rerender_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def append_log(self, msg: str):
        self.log_edit.appendPlainText(msg)

    def clear_log(self):
        self.log_edit.clear()

    def set_progress(self, fraction: float, desc: str = ""):
        self.progress_bar.setValue(int(max(0.0, min(1.0, fraction)) * 100))

    def show_result(self, report: JobReport):
        self.load_preview(report.output_path)

    def load_preview(self, out):
        self.preview_list.clear()
        if out is None:
            return
        d = Path(out)
        if d.is_file():
            d = d.parent / d.stem
        trans = d / "translated"
        if not trans.exists():
            return
        imgs = sorted([p for p in trans.rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")])[:24]
        for p in imgs:
            it = QtWidgets.QListWidgetItem(QtGui.QIcon(str(p)), p.name)
            self.preview_list.addItem(it)

    def rerender_source_path(self) -> str:
        """重渲染的定位来源：优先输出路径，为空则用输入路径（同一次任务的产物）。"""
        return self.output_edit.text().strip().strip('"') or self.input_edit.text().strip().strip('"')
