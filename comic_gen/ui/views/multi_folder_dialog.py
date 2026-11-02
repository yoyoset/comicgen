"""多选文件夹对话框：批量页「添加文件夹…」用。

Qt 自带的 QFileDialog.getExistingDirectory 是单选（Windows 原生对话框同样不支持
一次选多个目录），批量添加文件夹需要一次勾选多个，所以这里用
QFileSystemModel + QTreeView（MultiSelection，只显示目录）自建。
树根挂在文件系统根（本机所有盘符/网络共享），打开时自动展开到上次浏览目录。
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets


class MultiFolderDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, start_dir: str = ""):
        super().__init__(parent)
        self.setWindowTitle("批量添加文件夹（可多选）")
        self.resize(680, 500)

        layout = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel(
            "按住 Ctrl 或 Shift 点选可一次选择多个文件夹，已勾选的会全部加入队列。"
        )
        hint.setStyleSheet("color: gray;")
        layout.addWidget(hint)

        self._model = QtWidgets.QFileSystemModel(self)
        self._model.setFilter(QtCore.QDir.AllDirs | QtCore.QDir.NoDotAndDotDot)
        self._model.setRootPath("")

        self._tree = QtWidgets.QTreeView(self)
        self._tree.setModel(self._model)
        self._tree.setRootIndex(QtCore.QModelIndex())  # 文件系统根：显示所有盘符
        self._tree.setHeaderHidden(True)
        self._tree.setSelectionMode(QtWidgets.QAbstractItemView.MultiSelection)
        self._tree.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self._tree.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        for col in range(1, self._model.columnCount()):
            self._tree.setColumnHidden(col, True)
        self._tree.selectionModel().selectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self._tree, 1)

        self._count_lbl = QtWidgets.QLabel("已选择 0 个文件夹")
        layout.addWidget(self._count_lbl)

        btns = QtWidgets.QHBoxLayout()
        btns.addStretch(1)
        ok = QtWidgets.QPushButton("确定")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        cancel = QtWidgets.QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        layout.addLayout(btns)

        if start_dir:
            self._expand_to(start_dir)

    # ------------------------------------------------------------ 内部控制
    def _expand_to(self, start_dir: str):
        """自动展开到上次浏览目录并滚动到它。QFileSystemModel 的目录列表是异步加载的，
        每展开一层 pump 一次事件，等子目录到位再继续下一层。"""
        idx = self._model.index(start_dir)
        if not idx.isValid():
            return
        chain = []
        cur = idx
        while cur.isValid():
            chain.append(cur)
            cur = cur.parent()
        chain.reverse()
        for item in chain:
            self._tree.expand(item)
            QtCore.QCoreApplication.processEvents()
        self._tree.scrollTo(idx)

    def _on_selection_changed(self, *_):
        n = len(self.selected_folders())
        self._count_lbl.setText(f"已选择 {n} 个文件夹")

    # ------------------------------------------------------------ 取值
    def selected_folders(self) -> list[str]:
        """选中的文件夹绝对路径列表（按行去重：SelectRows 模式下隐藏列也会进
        selectedIndexes，只取第 0 列即可）。"""
        folders = []
        for idx in self._tree.selectionModel().selectedIndexes():
            if idx.isValid() and idx.column() == 0:
                folders.append(self._model.filePath(idx))
        return folders
