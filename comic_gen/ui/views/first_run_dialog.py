"""首次运行弹窗：确认模型目录、看一眼哪些模型已经在本机、还差多少。

只在第一次启动（AppSettings.setup_completed == False）时出现。"开始使用"和
"稍后再说"都会把这个标记置位——用户跳过了也不会每次启动都被打断。
"""
from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtWidgets

from ...core.manifest import LAYOUT_FILE, MODEL_MANIFEST
from ...core.model_locator import (
    STATUS_MISSING,
    ModelStatus,
    plan_download_bytes,
    status_text,
)
from ...core.paths import detect_default_model_dir, probe_writable


def _human_gb(n: int) -> str:
    return f"{n / (1024 ** 3):.1f} GB"


class FirstRunDialog(QtWidgets.QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("欢迎使用 comic-gen")
        self.setMinimumWidth(640)
        self._statuses: dict[str, ModelStatus] = {}
        self._scan_thread: QtCore.QThread | None = None
        self._scan_worker = None
        self._build()
        self._start_scan()

    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)

        intro = QtWidgets.QLabel(
            "模型可以跨项目复用——如果你已经用别的工具下载过 Sakura/Qwen 之类的模型，"
            "把下面的目录指向那个位置即可直接复用，不用重新下载。"
        )
        intro.setWordWrap(True)
        lay.addWidget(intro)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("模型目录"))
        self.dir_edit = QtWidgets.QLineEdit(detect_default_model_dir())
        self.dir_edit.editingFinished.connect(self._start_scan)
        row.addWidget(self.dir_edit, 1)
        browse_btn = QtWidgets.QPushButton("浏览…")
        browse_btn.clicked.connect(self._pick_dir)
        row.addWidget(browse_btn)
        lay.addLayout(row)

        self.table = QtWidgets.QTableWidget(len(MODEL_MANIFEST), 3)
        self.table.setHorizontalHeaderLabels(["模型", "用途", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        self._keys = list(MODEL_MANIFEST)
        self._status_items = {}
        for row_i, key in enumerate(self._keys):
            spec = MODEL_MANIFEST[key]
            self.table.setItem(row_i, 0, QtWidgets.QTableWidgetItem(spec.label))
            self.table.setItem(row_i, 1, QtWidgets.QTableWidgetItem(spec.purpose))
            item = QtWidgets.QTableWidgetItem("检测中…")
            self.table.setItem(row_i, 2, item)
            self._status_items[key] = item
        lay.addWidget(self.table, 1)

        self.summary_lbl = QtWidgets.QLabel()
        self.summary_lbl.setWordWrap(True)
        lay.addWidget(self.summary_lbl)

        self.warning_lbl = QtWidgets.QLabel()
        self.warning_lbl.setStyleSheet("color: #b8860b;")
        self.warning_lbl.setWordWrap(True)
        lay.addWidget(self.warning_lbl)

        # 安装包只带 CPU 框架：有 N 卡但还没装 GPU 组件时，首次运行就告诉用户去哪装。
        from ...core import gpu_components as gc
        if gc.has_nvidia_driver() and not gc.pack_status().is_ready:
            gpu_lbl = QtWidgets.QLabel(
                "检测到 NVIDIA 显卡：建议到「组件」页下载 GPU 加速组件"
                f"（约 {gc.total_download_bytes() / 1e9:.1f} GB），否则全部走 CPU，翻译会非常慢。")
            gpu_lbl.setWordWrap(True)
            gpu_lbl.setStyleSheet("color: #1f6feb;")
            lay.addWidget(gpu_lbl)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        self.later_btn = QtWidgets.QPushButton("稍后再说")
        self.later_btn.clicked.connect(self._on_later)
        btn_row.addWidget(self.later_btn)
        self.start_btn = QtWidgets.QPushButton("开始使用")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._on_start)
        btn_row.addWidget(self.start_btn)
        lay.addLayout(btn_row)

    def _pick_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择模型目录", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)
            self._start_scan()

    def _start_scan(self):
        from ...core.settings import AppSettings
        from ..workers import ScanWorker

        # 重入保护：目录框可能在上一次扫描还没跑完时又被改一次（比如用户连续编辑）。
        # 换新线程前不等旧线程收尾，旧的 QThread 对象会在还在跑的时候被丢弃引用——
        # 在这台机器上实测直接把解释器崩了（0xC0000409），不是纸上谈兵的风险。
        if self._scan_thread is not None and self._scan_thread.isRunning():
            return

        probe = AppSettings(model_dir=self.dir_edit.text().strip(), model_files=self.settings.model_files)
        self.start_btn.setEnabled(False)
        self._scan_thread = QtCore.QThread()
        self._scan_worker = ScanWorker(probe)
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.finished.connect(self._on_scan_done)
        self._scan_worker.finished.connect(self._scan_thread.quit)
        self._scan_thread.start()

    def _on_scan_done(self, statuses: dict, dupes: dict):
        self._statuses = statuses
        self.start_btn.setEnabled(True)
        for key, st in statuses.items():
            item = self._status_items.get(key)
            if not item:
                continue
            if not st.is_ready and st.state == STATUS_MISSING:
                item.setText("未找到")
                continue
            text, _ = status_text(st)
            item.setText(text)

        ready = sum(1 for s in statuses.values() if s.is_ready)
        missing = [k for k, s in statuses.items() if not s.is_ready]
        need_bytes = plan_download_bytes(missing)
        extra = "、".join(MODEL_MANIFEST[k].label for k in missing
                          if MODEL_MANIFEST[k].layout != LAYOUT_FILE)
        parts = [f"已发现 {ready}/{len(statuses)} 个模型"]
        if need_bytes:
            parts.append(f"还需下载约 {_human_gb(need_bytes)}")
        if extra:
            parts.append(f"另需下载：{extra}（大小见上表）")
        self.summary_lbl.setText("　·　".join(parts) + "。")

        target = Path(self.dir_edit.text().strip() or ".")
        try:
            import shutil
            free = shutil.disk_usage(target if target.exists() else target.anchor or ".").free
            if need_bytes and free < need_bytes:
                self.warning_lbl.setText(
                    f"⚠ 目标磁盘剩余空间可能不够：预计需要约 {_human_gb(need_bytes)}，"
                    f"当前剩余约 {_human_gb(free)}。"
                )
            else:
                self.warning_lbl.setText("")
        except OSError:
            self.warning_lbl.setText("")

    def _commit(self):
        """把扫描结果落到真正的 settings：目录 + 已发现模型的绑定，都在这里一次性写。"""
        self.settings.model_dir = self.dir_edit.text().strip()
        for key, st in self._statuses.items():
            if st.is_ready and st.path:
                self.settings.model_files.setdefault(key, str(st.path))
        self.settings.setup_completed = True
        self.settings.save()

    def _on_start(self):
        d = self.dir_edit.text().strip()
        if d:
            err = probe_writable(d)
            if err:
                QtWidgets.QMessageBox.warning(self, "目录不可用", err)
                return
        self._commit()
        self.accept()

    def _on_later(self):
        """跳过也要落盘 setup_completed，否则每次启动都会再弹一遍。目录/绑定不写——
        用户还没确认过，不能替他做主。"""
        self.settings.setup_completed = True
        self.settings.save()
        self.reject()

    def closeEvent(self, event):
        # 扫描线程用的是 tmp/独立的 AppSettings 副本读写本地文件，不碰真正的 settings；
        # 但线程对象本身若还在跑就被销毁，在少数环境下会在解释器退出时崩溃——
        # 等它跑完（本身就是纯本地文件系统操作，通常几十毫秒到一两秒）比冒这个险划算。
        if self._scan_thread is not None and self._scan_thread.isRunning():
            self._scan_thread.quit()
            self._scan_thread.wait(3000)
        super().closeEvent(event)
