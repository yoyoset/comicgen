"""首次运行弹窗：一屏看清"要下什么、多大、放哪"，点「开始下载」就开始。

只在第一次启动（AppSettings.setup_completed == False）时出现。"开始下载/开始使用"和
"稍后再说"都会把这个标记置位——用户跳过了也不会每次启动都被打断。

2026-11-05 之前这里只能选目录、看状态，汇总写"还需下载约 7.6 GB … 大小见上表"（表里其实没有
大小列，而且 7.6 是 GiB、只算了 Sakura 一项），也没有下载按钮——用户得自己找到「模型管理 →
补全全部缺失」和「组件」页。现在：表里每行有勾选框和大小，GPU 组件是一个勾选项并写明装在哪，
主按钮直接开始下载；真正的下载仍走主窗口里既有的 ModelWorker / ComponentWorker（这个弹窗只
产出一个下载计划 download_plan()，不自己下载），关窗后主窗口切到对应页面显示进度。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from ...core import first_run_plan as frp
from ...core import gpu_components as gc
from ...core.manifest import MODEL_MANIFEST
from ...core.model_locator import STATUS_MISSING, ModelStatus, status_text
from ...core.paths import detect_default_model_dir, probe_writable

_COL_CHECK, _COL_LABEL, _COL_PURPOSE, _COL_SIZE, _COL_STATUS = range(5)


def _free_bytes(p: Path) -> int | None:
    probe = p
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


class _FitTable(QtWidgets.QTableWidget):
    """尺寸提示 = 表头 + 全部行 + 边框（宽度 = 全部列宽），竖直方向 Fixed。

    0.4.1 先后两种做法都翻车（150% 缩放真机截图）：AdjustToContents + 关滚动条只露出 4 行；
    改成扫描后 setMinimumHeight/setMaximumHeight 定高，布局却仍按旧的高度摆后面的控件
    （实测表格 y=113..287，GPU 勾选框却被摆在 y=214），勾选框盖住最后两行。让布局直接从
    sizeHint 拿到内容尺寸，就没有"控件高度和布局缓存不一致"这回事。
    """

    def _content_size(self) -> QtCore.QSize:
        frame = 2 * self.frameWidth()
        w = self.horizontalHeader().length() + frame
        h = self.horizontalHeader().sizeHint().height() + self.verticalHeader().length() + frame + 2
        return QtCore.QSize(w, h)

    def sizeHint(self) -> QtCore.QSize:
        return self._content_size()

    def minimumSizeHint(self) -> QtCore.QSize:
        return self._content_size()


class FirstRunDialog(QtWidgets.QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("欢迎使用 comic-gen")
        self.setMinimumWidth(760)
        self._statuses: dict[str, ModelStatus] = {}
        self._scan_thread: QtCore.QThread | None = None
        self._scan_worker = None
        self._plan: tuple[list[str], bool] = ([], False)
        self._offer_gpu = gc.has_nvidia_driver() and not gc.pack_status().is_ready
        self._build()
        self._start_scan()

    # ------------------------------------------------------------ 搭建
    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)
        # 窗口最小尺寸跟着内容走：扫描完汇总变长（换行）时窗口自己长高，而不是把布局压扁——
        # 0.4.1 默认约束下，程序目录/模型目录路径很长时汇总和提示各折成 3 行，内容高过窗口，
        # Qt 压缩布局，定高的表格被 GPU 勾选框和提示文字盖住最后两行（150% 缩放真机截图复现）。
        lay.setSizeConstraint(QtWidgets.QLayout.SetMinimumSize)

        intro = QtWidgets.QLabel(
            "首次使用需要下载模型。下面勾选的会在点「开始下载」后自动下载，进度在主窗口里显示。<br>"
            "已经用别的工具下载过 Sakura/Qwen 之类的模型？把「模型保存到」指向那个目录，找到的就不用重下。"
        )
        intro.setWordWrap(True)
        intro.setTextFormat(QtCore.Qt.RichText)
        lay.addWidget(intro)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("模型保存到"))
        self.dir_edit = QtWidgets.QLineEdit(detect_default_model_dir())
        self.dir_edit.editingFinished.connect(self._start_scan)
        row.addWidget(self.dir_edit, 1)
        browse_btn = QtWidgets.QPushButton("更改…")
        browse_btn.clicked.connect(self._pick_dir)
        row.addWidget(browse_btn)
        lay.addLayout(row)

        self._keys = list(MODEL_MANIFEST)
        self.table = _FitTable(len(self._keys), 5)
        self.table.setSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Fixed)
        self.table.setHorizontalHeaderLabels(["下载", "模型", "用途", "大小", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        header = self.table.horizontalHeader()
        for col in (_COL_CHECK, _COL_LABEL, _COL_PURPOSE, _COL_SIZE):
            header.setSectionResizeMode(col, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(_COL_STATUS, QtWidgets.QHeaderView.Stretch)
        self._check_items: dict[str, QtWidgets.QTableWidgetItem] = {}
        self._status_items: dict[str, QtWidgets.QTableWidgetItem] = {}
        for r, key in enumerate(self._keys):
            spec = MODEL_MANIFEST[key]
            chk = QtWidgets.QTableWidgetItem()
            chk.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
            chk.setCheckState(QtCore.Qt.Unchecked)
            self.table.setItem(r, _COL_CHECK, chk)
            self._check_items[key] = chk
            label = spec.label + ("" if frp.default_selected(key) else "（按需）")
            self.table.setItem(r, _COL_LABEL, QtWidgets.QTableWidgetItem(label))
            self.table.setItem(r, _COL_PURPOSE, QtWidgets.QTableWidgetItem(spec.purpose))
            size_item = QtWidgets.QTableWidgetItem(frp.human_size(frp.approx_bytes(key)))
            size_item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.table.setItem(r, _COL_SIZE, size_item)
            st_item = QtWidgets.QTableWidgetItem("检测中…")
            self.table.setItem(r, _COL_STATUS, st_item)
            self._status_items[key] = st_item
        self.table.itemChanged.connect(lambda *_: self._update_summary())
        # 表的尺寸由 _FitTable.sizeHint 决定（全部 6 行 + 全部列宽），不留空白、不藏行
        self.table.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.table.resizeRowsToContents()
        lay.addWidget(self.table)

        # GPU 组件：只在有 N 卡驱动且还没装时出现。装在程序目录（不是模型目录），写清楚。
        self.gpu_chk: QtWidgets.QCheckBox | None = None
        if self._offer_gpu:
            self.gpu_chk = QtWidgets.QCheckBox(
                f"下载 GPU 加速组件（约 {frp.human_size(gc.total_download_bytes())}，"
                f"安装后占 {frp.human_size(gc.EXTRACTED_BYTES)}）")
            self.gpu_chk.setChecked(True)
            self.gpu_chk.toggled.connect(lambda *_: self._update_summary())
            lay.addWidget(self.gpu_chk)
            # 完整路径放进提示框：路径可能很长，直接写进正文会折成好几行
            gpu_note = QtWidgets.QLabel(
                "检测到 NVIDIA 显卡。装在程序文件夹下的 components 里（鼠标停在这里看完整路径）；"
                "不需要另装 CUDA，装完重启程序生效。不装也能用，但全部走 CPU，翻译会非常慢。")
            gpu_note.setToolTip(str(gc.components_root()))
            self.gpu_chk.setToolTip(str(gc.components_root()))
            gpu_note.setWordWrap(True)
            gpu_note.setStyleSheet("color: gray;")
            lay.addWidget(gpu_note)

        self.summary_lbl = QtWidgets.QLabel()
        self.summary_lbl.setWordWrap(True)
        lay.addWidget(self.summary_lbl)

        self.warning_lbl = QtWidgets.QLabel()
        self.warning_lbl.setStyleSheet("color: #b8860b;")
        self.warning_lbl.setWordWrap(True)
        lay.addWidget(self.warning_lbl)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        self.later_btn = QtWidgets.QPushButton("稍后再说")
        self.later_btn.clicked.connect(self._on_later)
        btn_row.addWidget(self.later_btn)
        self.start_btn = QtWidgets.QPushButton("开始下载")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._on_start)
        btn_row.addWidget(self.start_btn)
        lay.addLayout(btn_row)

    def _fit_table_height(self):
        """状态文字变了（列宽/行高跟着变）之后让布局重新问一次 _FitTable.sizeHint。"""
        # 不调 resizeColumnsToContents：它会把"状态"那列的 Stretch 宽度也改掉，表格右边留一条空白
        self.table.resizeRowsToContents()
        self.table.updateGeometry()

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_table_height()

    # ------------------------------------------------------------ 扫描
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
        self.table.blockSignals(True)
        for key, st in statuses.items():
            item = self._status_items.get(key)
            chk = self._check_items.get(key)
            if not item or not chk:
                continue
            if st.is_ready:
                text, _ = status_text(st)
                item.setText(text)
                chk.setCheckState(QtCore.Qt.Unchecked)
                chk.setFlags(QtCore.Qt.ItemIsUserCheckable)  # 已就绪：不可勾（灰掉）
            else:
                item.setText("未找到" if st.state == STATUS_MISSING else status_text(st)[0])
                chk.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
                chk.setCheckState(QtCore.Qt.Checked if frp.default_selected(key) else QtCore.Qt.Unchecked)
        self.table.blockSignals(False)
        self._fit_table_height()
        self._update_summary()

    # ------------------------------------------------------------ 汇总
    def selected_model_keys(self) -> list[str]:
        return [k for k in self._keys
                if self._check_items[k].checkState() == QtCore.Qt.Checked
                and not (self._statuses.get(k) and self._statuses[k].is_ready)]

    def _gpu_selected(self) -> bool:
        return bool(self.gpu_chk is not None and self.gpu_chk.isChecked())

    def _update_summary(self):
        if not self._statuses:
            return
        keys = self.selected_model_keys()
        gpu = self._gpu_selected()
        ready = sum(1 for s in self._statuses.values() if s.is_ready)
        lines = [f"已找到 {ready}/{len(self._statuses)} 个模型。"]
        model_bytes = frp.total_bytes(keys)
        if keys:
            names = "、".join(MODEL_MANIFEST[k].label for k in keys)
            lines.append(f"将下载模型：{names}，共约 {frp.human_size(model_bytes)} → 存到上面的「模型保存到」目录")
        if gpu:
            lines.append(f"将下载 GPU 组件：约 {frp.human_size(gc.total_download_bytes())} → 装到程序文件夹下的 components")
        if keys or gpu:
            lines.append(f"合计下载约 {frp.human_size(model_bytes + (gc.total_download_bytes() if gpu else 0))}。")
        else:
            lines.append("没有勾选要下载的东西。缺的模型以后可以在「模型管理」页下载。")
        self.summary_lbl.setText("<br>".join(lines))
        self.summary_lbl.setToolTip(
            f"模型目录：{self.dir_edit.text().strip() or '（默认 HF 缓存）'}\nGPU 组件：{gc.components_root()}")
        self.start_btn.setText("开始下载" if (keys or gpu) else "开始使用")

        # 磁盘空间：按盘汇总（模型目录和程序目录可能在不同盘，也可能同一块盘要两份加起来）
        need: dict[str, int] = {}
        if keys:
            anchor = Path(self.dir_edit.text().strip() or Path.home()).anchor
            need[anchor] = need.get(anchor, 0) + model_bytes
        if gpu:
            space = gc.check_disk_space()
            gpu_need = space[0] if space else gc.EXTRACTED_BYTES + gc.total_download_bytes()
            anchor = gc.components_root().anchor
            need[anchor] = need.get(anchor, 0) + gpu_need
        warns = []
        for anchor, n in need.items():
            free = _free_bytes(Path(anchor))
            if free is not None and free < n:
                warns.append(f"⚠ {anchor} 剩余 {frp.human_size(free)}，预计需要约 {frp.human_size(n)}，空间可能不够。")
        self.warning_lbl.setText("\n".join(warns))

    # ------------------------------------------------------------ 提交
    def download_plan(self) -> tuple[list[str], bool]:
        """(要下载的模型 key 列表, 是否安装 GPU 组件)。只有点了「开始下载」才非空。"""
        return self._plan

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
        self._plan = (self.selected_model_keys(), self._gpu_selected())
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
