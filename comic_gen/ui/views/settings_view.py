"""设置页：持久化偏好的编辑界面。改动即时生效并保存（成熟软件常见模式，无需显式"保存"按钮）。

纯视图：只读写 AppSettings 字段，不触碰 Runner/Config/线程。
按用途分组（模型与存储/识别与检测/排版与渲染），每项下面配一行说明——
照 subgen 设置窗口的写法：说清楚这个选项对用户意味着什么，不是复述参数名。
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ...core.paths import settings_path
from ...core.settings import AppSettings


def _hint(text: str) -> QtWidgets.QLabel:
    lbl = QtWidgets.QLabel(text)
    lbl.setStyleSheet("color: gray; font-size: 11px;")
    lbl.setWordWrap(True)
    return lbl


class SettingsView(QtWidgets.QWidget):
    changed = QtCore.Signal()  # 任意字段变化；MainWindow 监听后持久化

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False  # 程序化 load_from 时不触发 changed，避免启动即写盘
        self._build()

    def _build(self):
        outer = QtWidgets.QVBoxLayout(self)

        self.running_summary_lbl = QtWidgets.QLabel()
        self.running_summary_lbl.setWordWrap(True)
        self.running_summary_lbl.setStyleSheet("color: gray;")
        self.running_summary_lbl.setVisible(False)
        outer.addWidget(self.running_summary_lbl)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        self.form_container = QtWidgets.QWidget()
        scroll.setWidget(self.form_container)
        outer.addWidget(scroll, 1)

        lay = QtWidgets.QVBoxLayout(self.form_container)
        lay.addWidget(self._build_storage_group())
        lay.addWidget(self._build_detect_group())
        lay.addWidget(self._build_render_group())
        lay.addWidget(self._build_maintenance_group())
        lay.addStretch(1)

    def _build_storage_group(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("模型与存储")
        lay = QtWidgets.QFormLayout(box)

        self.model_dir_edit = QtWidgets.QLineEdit()
        self.model_dir_edit.setPlaceholderText("留空 = 默认 HF 缓存")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.model_dir_edit, 1)
        b = QtWidgets.QPushButton("浏览…")
        b.clicked.connect(self._pick_model_dir)
        row.addWidget(b)
        w = QtWidgets.QWidget()
        w.setLayout(row)
        lay.addRow("模型目录", w)
        lay.addRow("", _hint("模型可以跨项目复用：指向已有 HF 缓存或别的项目下载过的位置即可直接用，不必重新下载。"
                              "单个模型想用别的路径，去「模型管理」页逐项定位。"))
        self.model_dir_edit.editingFinished.connect(self._emit_changed)

        self.device_combo = QtWidgets.QComboBox()
        self.device_combo.addItem("GPU（CUDA，若可用）", "cuda")
        self.device_combo.addItem("CPU", "cpu")
        self.device_combo.currentIndexChanged.connect(self._emit_changed)
        lay.addRow("推理设备", self.device_combo)
        lay.addRow("", _hint("没有可用 GPU 时会自动退回 CPU，但速度明显变慢，仅建议临时应急用。"))

        return box

    def _build_detect_group(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("识别与检测")
        lay = QtWidgets.QFormLayout(box)

        self.threshold_spin = QtWidgets.QDoubleSpinBox()
        self.threshold_spin.setRange(0.05, 0.95)
        self.threshold_spin.setSingleStep(0.05)
        # editingFinished（失焦/回车才触发）而不是 valueChanged（每一步都触发）——
        # _emit_changed 会连带写盘 + 触发一次全盘模型扫描，拖动/滚轮连续调整这个
        # 数值会在很短时间内连开几十上百个扫描线程。和 model_dir_edit/font_edit
        # 用的是同一个模式。
        self.threshold_spin.editingFinished.connect(self._emit_changed)
        lay.addRow("检测阈值", self.threshold_spin)
        lay.addRow("", _hint("调低能检出更多文字区域但误检也会增加；调高更严格，但可能漏掉字号小或不清晰的文字。"))

        return box

    def _build_render_group(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("排版与渲染")
        lay = QtWidgets.QFormLayout(box)

        self.dpi_spin = QtWidgets.QSpinBox()
        self.dpi_spin.setRange(72, 600)
        self.dpi_spin.setSuffix(" DPI")
        self.dpi_spin.editingFinished.connect(self._emit_changed)
        lay.addRow("PDF DPI", self.dpi_spin)
        lay.addRow("", _hint("仅影响 PDF 输入的渲染分辨率：150 速度快且够用，300 画质更好但明显更慢。"))

        self.font_edit = QtWidgets.QLineEdit()
        frow = QtWidgets.QHBoxLayout()
        frow.addWidget(self.font_edit, 1)
        fb = QtWidgets.QPushButton("浏览…")
        fb.clicked.connect(self._pick_font)
        frow.addWidget(fb)
        fw = QtWidgets.QWidget()
        fw.setLayout(frow)
        lay.addRow("渲染字体", fw)
        self.font_edit.editingFinished.connect(self._emit_changed)
        lay.addRow("", _hint("译文贴回时使用的字体，需要支持中文字符（如系统自带的微软雅黑）。"))

        self.min_font_spin = QtWidgets.QSpinBox()
        self.min_font_spin.setRange(4, 200)
        # 跨字段约束（min<=max）要实时响应 valueChanged 才能在用户拖动时即时纠正另一
        # 个 spinbox；但落盘+触发扫描挪到 editingFinished，理由同上面 threshold_spin。
        self.min_font_spin.valueChanged.connect(self._on_min_font_changed)
        self.min_font_spin.editingFinished.connect(self._emit_changed)
        lay.addRow("最小字号", self.min_font_spin)

        self.max_font_spin = QtWidgets.QSpinBox()
        self.max_font_spin.setRange(4, 200)
        self.max_font_spin.valueChanged.connect(self._on_max_font_changed)
        self.max_font_spin.editingFinished.connect(self._emit_changed)
        lay.addRow("最大字号", self.max_font_spin)
        lay.addRow("", _hint("译文按气泡大小自动缩放字号，超出这个区间就不再继续缩放；最小字号不能大于最大字号。"))

        return box

    def _build_maintenance_group(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("维护")
        lay = QtWidgets.QVBoxLayout(box)

        self.reset_btn = QtWidgets.QPushButton("恢复默认")
        self.reset_btn.clicked.connect(self._reset_defaults)
        lay.addWidget(self.reset_btn, alignment=QtCore.Qt.AlignLeft)

        path_lbl = QtWidgets.QLabel(f"设置文件：{settings_path()}")
        path_lbl.setStyleSheet("color: gray;")
        path_lbl.setWordWrap(True)
        lay.addWidget(path_lbl)

        return box

    # ------------------------------------------------------------ 字号约束
    def _on_min_font_changed(self, v: int):
        """只做跨字段约束（min<=max），不在这里落盘/触发扫描——那是 editingFinished 的事。"""
        if v > self.max_font_spin.value():
            self.max_font_spin.blockSignals(True)
            self.max_font_spin.setValue(v)
            self.max_font_spin.blockSignals(False)

    def _on_max_font_changed(self, v: int):
        if v < self.min_font_spin.value():
            self.min_font_spin.blockSignals(True)
            self.min_font_spin.setValue(v)
            self.min_font_spin.blockSignals(False)

    def _pick_model_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择模型目录", self.model_dir_edit.text())
        if d:
            self.model_dir_edit.setText(d)
            self._emit_changed()

    def _pick_font(self):
        d, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择字体文件", self.font_edit.text(), "字体文件 (*.ttf *.ttc *.otf);;所有文件 (*.*)")
        if d:
            self.font_edit.setText(d)
            self._emit_changed()

    def _reset_defaults(self):
        self.load_from(AppSettings())
        self._emit_changed()

    def _emit_changed(self):
        if not self._loading:
            self.changed.emit()

    # ------------------------------------------------------------ 读写
    def load_from(self, s: AppSettings):
        self._loading = True
        try:
            self.model_dir_edit.setText(s.model_dir)
            idx = self.device_combo.findData(s.device)
            self.device_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.dpi_spin.setValue(s.pdf_dpi)
            self.font_edit.setText(s.font_path)
            self.min_font_spin.setValue(s.min_font_size)
            self.max_font_spin.setValue(s.max_font_size)
            self.threshold_spin.setValue(s.det_threshold)
        finally:
            self._loading = False

    def apply_to(self, s: AppSettings) -> AppSettings:
        s.model_dir = self.model_dir_edit.text().strip()
        s.device = self.device_combo.currentData()
        s.pdf_dpi = self.dpi_spin.value()
        s.font_path = self.font_edit.text().strip()
        s.min_font_size = self.min_font_spin.value()
        s.max_font_size = self.max_font_spin.value()
        s.det_threshold = self.threshold_spin.value()
        return s

    # ------------------------------------------------------------ 任务运行时的只读摘要
    def set_running(self, running: bool, summary: str = ""):
        """任务运行中显示这次任务实际在用的设置摘要，而不是一片禁用的表单——
        改动本来就要等下一次任务才生效，禁用整个表单反而让人以为改不了、而不是"改了也不影响这次"。
        """
        self.running_summary_lbl.setVisible(running)
        self.form_container.setEnabled(not running)
        if running:
            self.running_summary_lbl.setText(
                "这次任务正在使用的设置（改动会在下一次任务生效）：\n" + summary
            )
