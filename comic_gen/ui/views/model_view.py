"""模型管理页：逐项展示每个模型的状态，可单独定位/下载/清理重复。

纯视图：只建控件、暴露信号，不直接改 AppSettings——绑定/清理是有副作用的操作，
交给 MainWindow 校验、写盘、刷新。"在文件夹中显示"/"复制路径"这类只读本地操作
不涉及状态改动，直接在视图内完成。
"""
from __future__ import annotations

import subprocess

from PySide6 import QtCore, QtGui, QtWidgets

from ...core.manifest import LAYOUT_DIR, MODEL_MANIFEST
from ...core.model_locator import ModelStatus, status_text, validate_candidate

_COLUMNS = ["模型", "用途", "状态"]


class ModelView(QtWidgets.QWidget):
    scanRequested = QtCore.Signal()
    downloadRequested = QtCore.Signal(str)              # key
    downloadAllMissingRequested = QtCore.Signal()
    cancelDownloadRequested = QtCore.Signal()
    bindRequested = QtCore.Signal(str, str)              # key, 已通过 validate_candidate 的路径
    clearBindingRequested = QtCore.Signal(str)           # key
    dedupeRequested = QtCore.Signal(str)                 # key

    def __init__(self, parent=None):
        super().__init__(parent)
        self._statuses: dict[str, ModelStatus] = {}
        self._dupes: dict[str, list] = {}
        self._row_widgets: dict[str, dict] = {}
        self._build()

    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        self.dir_lbl = QtWidgets.QLabel("模型目录：（跟随全局 HF 缓存）")
        self.dir_lbl.setWordWrap(True)
        top.addWidget(self.dir_lbl, 1)
        self.scan_btn = QtWidgets.QPushButton("扫描本机已有模型")
        self.scan_btn.clicked.connect(self.scanRequested)
        top.addWidget(self.scan_btn)
        self.download_all_btn = QtWidgets.QPushButton("补全全部缺失")
        self.download_all_btn.clicked.connect(self.downloadAllMissingRequested)
        top.addWidget(self.download_all_btn)
        self.cancel_download_btn = QtWidgets.QPushButton("停止下载")
        self.cancel_download_btn.setEnabled(False)
        self.cancel_download_btn.setToolTip("只能在当前这个模型下载完成后停止，无法打断正在进行的单次下载")
        self.cancel_download_btn.clicked.connect(self._on_cancel_clicked)
        top.addWidget(self.cancel_download_btn)
        lay.addLayout(top)

        self._keys = list(MODEL_MANIFEST)
        self.table = QtWidgets.QTableWidget(len(self._keys), len(_COLUMNS) + 1)
        self.table.setHorizontalHeaderLabels(_COLUMNS + ["操作"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        lay.addWidget(self.table, 1)

        for row, key in enumerate(self._keys):
            spec = MODEL_MANIFEST[key]
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(spec.label))
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(spec.purpose))
            status_item = QtWidgets.QTableWidgetItem("尚未检测")
            self.table.setItem(row, 2, status_item)
            # 先占位再让 _build_actions 用 setdefault().update() 追加按钮引用——
            # 之前这里是整体赋值，_build_actions 刚塞进去的按钮引用会被这行覆盖掉。
            self._row_widgets[key] = {"status_item": status_item}
            self.table.setCellWidget(row, 3, self._build_actions(key))

        self.status_lbl = QtWidgets.QLabel()
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color: gray;")
        lay.addWidget(self.status_lbl)

        # 下载进度：下载器本身没有进度回调，数字来自 MainWindow 对下载目录字节数的旁路估算
        self.dl_lbl = QtWidgets.QLabel()
        self.dl_bar = QtWidgets.QProgressBar()
        self.dl_bar.setRange(0, 1000)
        for w in (self.dl_lbl, self.dl_bar):
            w.setVisible(False)
            lay.addWidget(w)

        self.log_edit = QtWidgets.QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(90)
        self.log_edit.setMaximumBlockCount(2000)  # 批量补全几十个模型的输出会很长，上限防内存无界增长
        lay.addWidget(self.log_edit)

    def _build_actions(self, key: str) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(2, 2, 2, 2)

        locate_btn = QtWidgets.QPushButton("定位…")
        locate_btn.clicked.connect(lambda: self._on_locate(key))
        h.addWidget(locate_btn)

        download_btn = QtWidgets.QPushButton("下载")
        download_btn.clicked.connect(lambda: self.downloadRequested.emit(key))
        h.addWidget(download_btn)

        more_btn = QtWidgets.QToolButton()
        more_btn.setText("更多")
        more_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        menu = QtWidgets.QMenu(more_btn)
        act_show = menu.addAction("在文件夹中显示")
        act_show.triggered.connect(lambda: self._on_open_folder(key))
        act_copy = menu.addAction("复制路径")
        act_copy.triggered.connect(lambda: self._on_copy_path(key))
        act_clear = menu.addAction("清除绑定")
        act_clear.triggered.connect(lambda: self.clearBindingRequested.emit(key))
        act_dedupe = menu.addAction("清理重复")
        act_dedupe.triggered.connect(lambda: self.dedupeRequested.emit(key))
        more_btn.setMenu(menu)
        h.addWidget(more_btn)

        self._row_widgets.setdefault(key, {}).update({
            "locate_btn": locate_btn, "download_btn": download_btn, "more_btn": more_btn,
            "act_show": act_show, "act_copy": act_copy, "act_clear": act_clear, "act_dedupe": act_dedupe,
        })
        return w

    # ------------------------------------------------------------ 本地只读操作（无副作用，视图内直接做）
    def _on_open_folder(self, key: str):
        st = self._statuses.get(key)
        if not st or not st.path:
            return
        path = st.path
        target = path if path.is_dir() else path.parent
        try:
            if path.is_file():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                subprocess.Popen(["explorer", str(target)])
        except OSError:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(target)))

    def _on_copy_path(self, key: str):
        st = self._statuses.get(key)
        if st and st.path:
            QtWidgets.QApplication.clipboard().setText(str(st.path))

    def _on_locate(self, key: str):
        spec = MODEL_MANIFEST[key]
        if spec.layout == LAYOUT_DIR:
            path = QtWidgets.QFileDialog.getExistingDirectory(self, f"定位 {spec.label} 所在目录")
        else:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, f"定位 {spec.label} 文件",
                                                              filter=spec.ext_filter or "所有文件 (*.*)")
        if not path:
            return
        err = validate_candidate(key, path)
        if err:
            QtWidgets.QMessageBox.warning(self, "不像是有效的模型文件", err)
            return
        self.bindRequested.emit(key, path)

    # ------------------------------------------------------------ 读写（供 MainWindow 调用）
    def set_model_dir_label(self, d: str):
        self.dir_lbl.setText(f"模型目录：{d}" if d else "模型目录：（跟随全局 HF 缓存）")

    def set_busy(self, busy: bool):
        self.scan_btn.setEnabled(not busy)
        self.download_all_btn.setEnabled(not busy)
        self.cancel_download_btn.setEnabled(busy)
        for widgets in self._row_widgets.values():
            for name in ("locate_btn", "download_btn", "more_btn"):
                if name in widgets:
                    widgets[name].setEnabled(not busy)

    def _on_cancel_clicked(self):
        self.cancel_download_btn.setEnabled(False)
        self.cancelDownloadRequested.emit()

    def refresh(self, statuses: dict[str, ModelStatus], dupes: dict[str, list] | None = None):
        self._statuses = statuses
        self._dupes = dupes or {}
        ready_count = sum(1 for s in statuses.values() if s.is_ready)
        for key, st in statuses.items():
            widgets = self._row_widgets.get(key)
            if not widgets:
                continue
            text, color = status_text(st)
            widgets["status_item"].setText(text)
            widgets["status_item"].setForeground(QtGui.QColor(color))

            dup_count = len(self._dupes.get(key, []))
            has_path = st.path is not None
            widgets["download_btn"].setEnabled(not st.is_ready)
            widgets["act_show"].setEnabled(has_path)
            widgets["act_copy"].setEnabled(has_path)
            widgets["act_clear"].setEnabled(key in getattr(self, "_current_bindings", {}))
            widgets["act_dedupe"].setEnabled(dup_count > 1)
            widgets["act_dedupe"].setText(f"清理重复（发现 {dup_count} 份）" if dup_count > 1 else "清理重复")

        self.status_lbl.setText(f"{ready_count}/{len(statuses)} 个模型已就绪。")

    def set_current_bindings(self, bindings: dict[str, str]):
        """用于判断"清除绑定"是否该可用；单独设置而非塞进 refresh，避免调用方总要凑两份数据。"""
        self._current_bindings = dict(bindings)

    def missing_keys(self) -> list[str]:
        return [k for k, st in self._statuses.items() if not st.is_ready]

    def set_download_progress(self, label: str, done: int, expected: int):
        """expected 为 0 表示不知道总量（只显示已下载字节数，进度条转圈）。"""
        self.dl_lbl.setVisible(True)
        self.dl_bar.setVisible(True)
        if expected > 0:
            frac = min(done / expected, 0.99)  # 估算总量可能偏小，没下完别显示 100%
            self.dl_bar.setRange(0, 1000)
            self.dl_bar.setValue(int(frac * 1000))
            self.dl_lbl.setText(f"正在下载 {label}：{done / 1e6:,.0f} / 约 {expected / 1e6:,.0f} MB（{frac:.0%}）")
        else:
            self.dl_bar.setRange(0, 0)
            self.dl_lbl.setText(f"正在下载 {label}：已下载 {done / 1e6:,.0f} MB")

    def clear_download_progress(self):
        self.dl_lbl.setVisible(False)
        self.dl_bar.setVisible(False)

    def show_status(self, text: str):
        self.log_edit.appendPlainText(text)
