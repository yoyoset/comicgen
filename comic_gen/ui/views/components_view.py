"""组件页：GPU 加速组件的状态、下载（进度/失败原因/重试）、删除。

纯视图：只建控件、暴露信号；下载在 MainWindow 起的 ComponentWorker 线程里跑。
"""
from __future__ import annotations

import subprocess

from PySide6 import QtCore, QtWidgets

from ...core import gpu_components as gc


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


class ComponentsView(QtWidgets.QWidget):
    installRequested = QtCore.Signal()
    cancelRequested = QtCore.Signal()
    removeRequested = QtCore.Signal()
    restartRequested = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False
        self._cuda_available: bool | None = None
        self._build()
        self.refresh()

    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)

        title = QtWidgets.QLabel("<b>GPU 加速组件（NVIDIA 显卡）</b>")
        lay.addWidget(title)
        intro = QtWidgets.QLabel(
            "安装包只带 CPU 运行框架。有 NVIDIA 显卡的话，下载 GPU 组件后检测/OCR/抹字/翻译都走显卡，"
            "速度快很多（本地 14B 翻译模型在 CPU 上非常慢）。<br>"
            f"需要下载约 <b>{_gb(gc.total_download_bytes())}</b>，安装后占用约 <b>{_gb(gc.EXTRACTED_BYTES)}</b>"
            "（下载的压缩包装完即删）。不需要另外安装 CUDA：组件自带 CUDA 12.4 运行库，"
            "只需要 NVIDIA 显卡驱动（版本 ≥ 551）。<br>"
            "来源：PyTorch 官方（download.pytorch.org）、llama-cpp-python 官方 GitHub Release；"
            "每个文件下载后核对 SHA256，不一致会拒绝安装。官方源连不上时自动换镜像，断网后再点会从断点继续。"
        )
        intro.setWordWrap(True)
        intro.setTextFormat(QtCore.Qt.RichText)
        lay.addWidget(intro)

        form = QtWidgets.QFormLayout()
        self.driver_lbl = QtWidgets.QLabel()
        self.pack_lbl = QtWidgets.QLabel()
        self.pack_lbl.setWordWrap(True)
        self.runtime_lbl = QtWidgets.QLabel()
        form.addRow("显卡驱动", self.driver_lbl)
        form.addRow("GPU 组件", self.pack_lbl)
        form.addRow("本次运行", self.runtime_lbl)
        lay.addLayout(form)

        btns = QtWidgets.QHBoxLayout()
        self.install_btn = QtWidgets.QPushButton("下载并安装 GPU 组件")
        self.install_btn.clicked.connect(self.installRequested)
        self.cancel_btn = QtWidgets.QPushButton("取消")
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.restart_btn = QtWidgets.QPushButton("重启程序以启用")
        self.restart_btn.clicked.connect(self.restartRequested)
        self.remove_btn = QtWidgets.QPushButton("删除 GPU 组件")
        self.remove_btn.clicked.connect(self._on_remove)
        self.open_btn = QtWidgets.QPushButton("打开组件目录")
        self.open_btn.clicked.connect(self._on_open_dir)
        for b in (self.install_btn, self.cancel_btn, self.restart_btn, self.remove_btn, self.open_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        lay.addLayout(btns)

        self.step_lbl = QtWidgets.QLabel()
        lay.addWidget(self.step_lbl)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setVisible(False)
        lay.addWidget(self.progress)

        self.error_lbl = QtWidgets.QLabel()
        self.error_lbl.setWordWrap(True)
        self.error_lbl.setStyleSheet("color: #c0392b;")
        self.error_lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        lay.addWidget(self.error_lbl)

        self.log_edit = QtWidgets.QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(2000)
        lay.addWidget(self.log_edit, 1)

    # ------------------------------------------------------------ 状态
    def set_cuda_available(self, ok: bool | None):
        """环境自检的结果（torch.cuda.is_available()）——"本次运行实际用没用上 GPU"的唯一权威来源。"""
        self._cuda_available = ok
        self.refresh()

    def refresh(self):
        driver = gc.has_nvidia_driver()
        self.driver_lbl.setText("已检测到 NVIDIA 驱动" if driver
                                else "未检测到 NVIDIA 驱动（没有 N 卡就不用下载 GPU 组件）")
        st = gc.pack_status()
        active = gc.active_dir() is not None
        if st.state == gc.STATE_READY:
            self.pack_lbl.setText(f"已安装：{st.path}")
        elif st.state == gc.STATE_BROKEN:
            self.pack_lbl.setText(f"<span style='color:#c0392b'>需要重新安装：{st.reason}</span>")
        else:
            self.pack_lbl.setText("未安装")
        if self._cuda_available is None:
            run = "检测中…"
        elif self._cuda_available:
            run = "GPU（CUDA 可用）"
        elif active:
            run = "CPU —— GPU 组件已加载，但 CUDA 不可用（显卡驱动过旧或没有 N 卡？见「环境自检」）"
        else:
            run = "CPU"
        self.runtime_lbl.setText(run)

        needs_restart = st.is_ready and not active
        self.restart_btn.setVisible(needs_restart)
        if self._busy:
            self.install_btn.setEnabled(False)
            self.cancel_btn.setEnabled(True)
            self.remove_btn.setEnabled(False)
        else:
            self.install_btn.setEnabled(not st.is_ready)
            self.install_btn.setText("重试" if self.error_lbl.text() else
                                     ("重新安装 GPU 组件" if st.state == gc.STATE_BROKEN else "下载并安装 GPU 组件"))
            self.cancel_btn.setEnabled(False)
            self.remove_btn.setEnabled(st.state != gc.STATE_MISSING)
        self.open_btn.setEnabled(gc.components_root().exists())

    def set_busy(self, busy: bool):
        self._busy = busy
        if busy:
            self.error_lbl.setText("")
            self.progress.setValue(0)
            self.progress.setVisible(True)
        self.refresh()

    # ------------------------------------------------------------ worker 回调
    def on_progress(self, stage: str, done: int, total: int):
        frac = done / total if total else 0.0
        self.progress.setValue(int(frac * 1000))
        self.step_lbl.setText(f"{stage}：{done / 1e6:,.0f} / {total / 1e6:,.0f} MB（{frac:.0%}）")

    def append_log(self, text: str):
        self.log_edit.appendPlainText(text)

    def on_finished(self, ok: bool, message: str, technical: str):
        self._busy = False
        self.progress.setVisible(False)
        if ok:
            self.error_lbl.setText("")
            self.step_lbl.setText(message)
        else:
            self.step_lbl.setText("")
            self.error_lbl.setText(message)
            if technical:
                self.append_log(f"[技术细节] {technical}")
        self.refresh()

    # ------------------------------------------------------------ 按钮
    def _on_cancel(self):
        self.cancel_btn.setEnabled(False)
        self.step_lbl.setText("正在取消…")
        self.cancelRequested.emit()

    def _on_remove(self):
        r = QtWidgets.QMessageBox.question(
            self, "删除 GPU 组件", "删除后程序回到 CPU 运行，需要时可以重新下载。确定删除？")
        if r == QtWidgets.QMessageBox.Yes:
            self.removeRequested.emit()

    def _on_open_dir(self):
        d = gc.components_root()
        if d.exists():
            subprocess.Popen(["explorer", str(d)])
