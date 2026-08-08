"""校对界面：页面画布 + 检测框叠加 + 区域编辑 + 单页重渲染 + 需要注意列表。

之前"校对"约等于退出应用、在文本编辑器里对着 pages.json 的坐标数组猜哪个区域是
哪个气泡。这里画布上的检测框可以直接点，选中后侧栏显示原文/可编辑译文，"应用译文"
只改文字+存盘（快），"重渲染本页"才真的跑一次抹字+渲染（慢，GPU）——两个动作分开，
方便一次改完好几个区域的译文再统一重渲染一次，不用每改一个字都等一次 GPU。

纯视图：只建控件、发信号、暴露"push 数据进来"的方法，不碰 ReviewSession/Config——
和 model_view.py/settings_view.py 同样的分工，业务对象由 MainWindow 持有。
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets


class _RegionItem(QtWidgets.QGraphicsRectItem):
    """一个检测框。画布坐标系直接用图片像素坐标（bbox 本来就是这个单位），
    不需要额外的缩放换算——QGraphicsView 自己处理视图缩放。
    """

    _NORMAL = QtGui.QColor("#e0a800")
    _SELECTED = QtGui.QColor("#d9363e")

    def __init__(self, index: int, x: float, y: float, w: float, h: float):
        super().__init__(x, y, w, h)
        self.index = index
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.set_selected_style(False)

    def set_selected_style(self, selected: bool):
        color = self._SELECTED if selected else self._NORMAL
        self.setPen(QtGui.QPen(color, 3 if selected else 2))
        fill = QtGui.QColor(color)
        fill.setAlpha(50 if selected else 30)
        self.setBrush(QtGui.QBrush(fill))


class ReviewView(QtWidgets.QWidget):
    openRequested = QtCore.Signal()
    pageSelected = QtCore.Signal(str)                    # image
    applyTextRequested = QtCore.Signal(str, int, str)     # image, region_index, translated_text
    rerenderRequested = QtCore.Signal(str)                # image
    attentionItemActivated = QtCore.Signal(str, int)      # image, region_index（-1 = 整页问题，只跳页不选框）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._regions: list = []
        self._region_items: dict[int, _RegionItem] = {}
        self._selected_index: int | None = None
        self._current_image: str | None = None
        self._build()

    def _build(self):
        root = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        self.open_btn = QtWidgets.QPushButton("打开工作目录…")
        self.open_btn.clicked.connect(self.openRequested)
        top.addWidget(self.open_btn)
        self.dir_lbl = QtWidgets.QLabel("未打开任何工作目录")
        self.dir_lbl.setStyleSheet("color: gray;")
        top.addWidget(self.dir_lbl, 1)
        root.addLayout(top)

        split = QtWidgets.QSplitter()
        root.addWidget(split, 1)

        self.page_list = QtWidgets.QListWidget()
        self.page_list.setMaximumWidth(200)
        self.page_list.currentTextChanged.connect(self._on_page_row_changed)
        split.addWidget(self.page_list)

        self.scene = QtWidgets.QGraphicsScene()
        self.view = QtWidgets.QGraphicsView(self.scene)
        self.view.setRenderHint(QtGui.QPainter.Antialiasing)
        self.scene.selectionChanged.connect(self._on_selection_changed)
        split.addWidget(self.view)

        side = QtWidgets.QWidget()
        side_lay = QtWidgets.QVBoxLayout(side)
        side_lay.addWidget(QtWidgets.QLabel("原文"))
        self.source_edit = QtWidgets.QPlainTextEdit()
        self.source_edit.setReadOnly(True)
        self.source_edit.setMaximumHeight(80)
        side_lay.addWidget(self.source_edit)
        side_lay.addWidget(QtWidgets.QLabel("译文"))
        self.translated_edit = QtWidgets.QPlainTextEdit()
        self.translated_edit.setMaximumHeight(80)
        side_lay.addWidget(self.translated_edit)

        btn_row = QtWidgets.QHBoxLayout()
        self.apply_btn = QtWidgets.QPushButton("应用译文")
        self.apply_btn.setToolTip("只保存文字修改，不重新生成图片")
        self.apply_btn.clicked.connect(self._on_apply_clicked)
        btn_row.addWidget(self.apply_btn)
        self.rerender_btn = QtWidgets.QPushButton("重渲染本页")
        self.rerender_btn.setToolTip("只重新生成这一页的图片，不影响其它页，也不重新打包最终产物")
        self.rerender_btn.clicked.connect(self._on_rerender_clicked)
        btn_row.addWidget(self.rerender_btn)
        side_lay.addLayout(btn_row)

        side_lay.addWidget(QtWidgets.QLabel("需要注意"))
        self.attention_list = QtWidgets.QListWidget()
        self.attention_list.itemActivated.connect(self._on_attention_activated)
        side_lay.addWidget(self.attention_list, 1)

        split.addWidget(side)
        split.setStretchFactor(1, 1)

        self._set_editing_enabled(False)
        self.rerender_btn.setEnabled(False)

    # ------------------------------------------------------------ 供 MainWindow 推数据进来
    def set_work_dir_label(self, text: str):
        self.dir_lbl.setText(text)

    def current_image(self) -> str | None:
        return self._current_image

    def load_pages(self, images: list[str], attention: list[tuple[str, int, str]]):
        self.page_list.blockSignals(True)
        self.page_list.clear()
        self.page_list.addItems(images)
        self.page_list.blockSignals(False)
        self.refresh_attention(attention)
        if images:
            self.page_list.setCurrentRow(0)

    def refresh_attention(self, attention: list[tuple[str, int, str]]):
        self.attention_list.clear()
        if not attention:
            empty = QtWidgets.QListWidgetItem("（没有需要注意的区域，译文质量不错）")
            empty.setFlags(QtCore.Qt.NoItemFlags)
            self.attention_list.addItem(empty)
            return
        for image, region_index, reason in attention:
            item = QtWidgets.QListWidgetItem(f"{image}　{reason}")
            item.setData(QtCore.Qt.UserRole, (image, region_index))
            self.attention_list.addItem(item)

    def show_page(self, image: str, pixmap: QtGui.QPixmap, regions: list):
        """regions：这一页的 core.models.Region 列表，用于叠加检测框 + 回显原文。"""
        self._current_image = image
        self._regions = regions
        self._selected_index = None
        # 先清空这个引用字典，再 scene.clear()——scene.clear() 删除旧的 _RegionItem 时
        # 会同步触发 selectionChanged（原来选中的框被删除等于"选择变化"），如果这时
        # _region_items 还指着这些刚被 C++ 层删除的对象，_on_selection_changed 里遍历
        # 它们调 set_selected_style 会撞上 libshiboken 的"already deleted" RuntimeError
        # （实测复现：点开一页、重渲染后再跳去另一处选区才会触发，离屏测试脚本抓到的）。
        self._region_items = {}
        self.scene.clear()
        self.scene.addPixmap(pixmap)
        self.scene.setSceneRect(0, 0, max(1, pixmap.width()), max(1, pixmap.height()))
        for i, r in enumerate(regions):
            x, y, w, h = r.bbox
            item = _RegionItem(i, x, y, w, h)
            self.scene.addItem(item)
            self._region_items[i] = item
        self.view.fitInView(self.scene.sceneRect(), QtCore.Qt.KeepAspectRatio)
        self._set_editing_enabled(False)
        self.rerender_btn.setEnabled(True)
        self.source_edit.clear()
        self.translated_edit.clear()

    def select_page(self, image: str):
        items = self.page_list.findItems(image, QtCore.Qt.MatchExactly)
        if items:
            self.page_list.setCurrentItem(items[0])

    def select_region(self, index: int):
        item = self._region_items.get(index)
        if item is not None:
            item.setSelected(True)

    def set_busy(self, busy: bool):
        self.open_btn.setEnabled(not busy)
        self.rerender_btn.setEnabled(not busy and self._current_image is not None)

    # ------------------------------------------------------------ 内部事件 -> 对外信号
    def _on_page_row_changed(self, image: str):
        if image:
            self.pageSelected.emit(image)

    def _on_selection_changed(self):
        selected = [it for it in self.scene.selectedItems() if isinstance(it, _RegionItem)]
        for item in self._region_items.values():
            item.set_selected_style(item in selected)
        if not selected:
            self._selected_index = None
            self._set_editing_enabled(False)
            return
        index = selected[0].index
        self._selected_index = index
        region = self._regions[index]
        self.source_edit.setPlainText(region.source_text)
        self.translated_edit.setPlainText(region.translated_text)
        self._set_editing_enabled(True)

    def _on_apply_clicked(self):
        if self._current_image is None or self._selected_index is None:
            return
        self.applyTextRequested.emit(self._current_image, self._selected_index, self.translated_edit.toPlainText())

    def _on_rerender_clicked(self):
        if self._current_image is None:
            return
        self.rerenderRequested.emit(self._current_image)

    def _on_attention_activated(self, item: QtWidgets.QListWidgetItem):
        image, region_index = item.data(QtCore.Qt.UserRole)
        self.attentionItemActivated.emit(image, region_index)

    def _set_editing_enabled(self, enabled: bool):
        self.translated_edit.setEnabled(enabled)
        self.apply_btn.setEnabled(enabled)
