"""模型管理页：模型清单来自 core.manifest.MODEL_MANIFEST（与流水线共用同一份 repo 定义）。"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ...core.manifest import MODEL_MANIFEST


class ModelView(QtWidgets.QWidget):
    checkRequested = QtCore.Signal(str, bool, bool)     # model_dir, want_sakura, want_llm
    downloadRequested = QtCore.Signal(str, bool, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)

        sizes = "、".join(f"{s.label}({s.approx_size})" for s in MODEL_MANIFEST.values())
        tip = QtWidgets.QLabel(
            "模型可跨项目复用：填写已有 HF 缓存目录即可复用，不必下载。\n" + sizes
        )
        tip.setWordWrap(True)
        lay.addWidget(tip)

        row = QtWidgets.QHBoxLayout()
        self.dir_edit = QtWidgets.QLineEdit()
        self.dir_edit.setPlaceholderText("留空 = 用设置页里的模型目录")
        row.addWidget(QtWidgets.QLabel("目录"))
        row.addWidget(self.dir_edit, 1)
        b = QtWidgets.QPushButton("浏览…")
        b.clicked.connect(self._pick_dir)
        row.addWidget(b)
        lay.addLayout(row)

        self.sakura_check = QtWidgets.QCheckBox(f"涉及 {MODEL_MANIFEST['sakura'].label}（{MODEL_MANIFEST['sakura'].approx_size}）")
        lay.addWidget(self.sakura_check)
        self.llm_check = QtWidgets.QCheckBox(f"涉及 {MODEL_MANIFEST['llm'].label}（{MODEL_MANIFEST['llm'].approx_size}）")
        lay.addWidget(self.llm_check)

        brow = QtWidgets.QHBoxLayout()
        self.check_btn = QtWidgets.QPushButton("仅检查现有")
        self.check_btn.clicked.connect(self._emit_check)
        self.dl_btn = QtWidgets.QPushButton("补全缺失模型")
        self.dl_btn.clicked.connect(self._emit_download)
        brow.addWidget(self.check_btn)
        brow.addWidget(self.dl_btn)
        lay.addLayout(brow)

        self.status = QtWidgets.QPlainTextEdit()
        self.status.setReadOnly(True)
        lay.addWidget(self.status)

    def _pick_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择模型目录", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def _emit_check(self):
        self.checkRequested.emit(self.dir_edit.text().strip(), self.sakura_check.isChecked(), self.llm_check.isChecked())

    def _emit_download(self):
        self.downloadRequested.emit(self.dir_edit.text().strip(), self.sakura_check.isChecked(), self.llm_check.isChecked())

    def set_default_dir(self, d: str):
        if not self.dir_edit.text():
            self.dir_edit.setPlaceholderText(d or "留空 = 默认 HF 缓存")

    def set_busy(self, busy: bool):
        self.check_btn.setEnabled(not busy)
        self.dl_btn.setEnabled(not busy)

    def show_status(self, text: str):
        self.status.setPlainText(text)
