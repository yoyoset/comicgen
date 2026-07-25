"""设置页：持久化偏好的编辑界面。改动即时生效并保存（成熟软件常见模式，无需显式"保存"按钮）。

纯视图：只读写 AppSettings 字段，不触碰 Runner/Config/线程。
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ...core.paths import settings_path
from ...core.settings import AppSettings


class SettingsView(QtWidgets.QWidget):
    changed = QtCore.Signal()  # 任意字段变化；MainWindow 监听后持久化

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False  # 程序化 load_from 时不触发 changed，避免启动即写盘
        self._build()

    def _build(self):
        lay = QtWidgets.QFormLayout()
        self.setLayout(lay)

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
        self.model_dir_edit.editingFinished.connect(self._emit_changed)

        self.device_combo = QtWidgets.QComboBox()
        self.device_combo.addItem("GPU（CUDA，若可用）", "cuda")
        self.device_combo.addItem("CPU", "cpu")
        self.device_combo.currentIndexChanged.connect(self._emit_changed)
        lay.addRow("推理设备", self.device_combo)

        self.dpi_spin = QtWidgets.QSpinBox()
        self.dpi_spin.setRange(72, 600)
        self.dpi_spin.setSuffix(" DPI")
        self.dpi_spin.setToolTip("PDF 渲染分辨率；150 快且够用，300 画质好但慢")
        self.dpi_spin.valueChanged.connect(self._emit_changed)
        lay.addRow("PDF DPI", self.dpi_spin)

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

        self.min_font_spin = QtWidgets.QSpinBox()
        self.min_font_spin.setRange(4, 200)
        self.min_font_spin.valueChanged.connect(self._emit_changed)
        lay.addRow("最小字号", self.min_font_spin)

        self.max_font_spin = QtWidgets.QSpinBox()
        self.max_font_spin.setRange(4, 200)
        self.max_font_spin.valueChanged.connect(self._emit_changed)
        lay.addRow("最大字号", self.max_font_spin)

        self.threshold_spin = QtWidgets.QDoubleSpinBox()
        self.threshold_spin.setRange(0.05, 0.95)
        self.threshold_spin.setSingleStep(0.05)
        self.threshold_spin.setToolTip("文本检测置信度阈值；调低可检出更多但误检增加，调高更严格但可能漏检")
        self.threshold_spin.valueChanged.connect(self._emit_changed)
        lay.addRow("检测阈值", self.threshold_spin)

        self.reset_btn = QtWidgets.QPushButton("恢复默认")
        self.reset_btn.clicked.connect(self._reset_defaults)
        lay.addRow(self.reset_btn)

        path_lbl = QtWidgets.QLabel(f"设置文件：{settings_path()}")
        path_lbl.setStyleSheet("color: gray;")
        path_lbl.setWordWrap(True)
        lay.addRow(path_lbl)

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
