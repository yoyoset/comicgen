"""环境自检页：展示 EnvReport，首次运行/随时手动核对依赖与模型状态。"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ...core.environment import EnvReport
from ...core.manifest import MODEL_MANIFEST


class DiagnosticsView(QtWidgets.QWidget):
    recheckRequested = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)

        self.summary_lbl = QtWidgets.QLabel("尚未检测")
        self.summary_lbl.setWordWrap(True)
        lay.addWidget(self.summary_lbl)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["项目", "状态"])
        self.tree.setColumnWidth(0, 260)
        lay.addWidget(self.tree, 1)

        self.warnings_lbl = QtWidgets.QLabel()
        self.warnings_lbl.setWordWrap(True)
        self.warnings_lbl.setStyleSheet("color: #b8860b;")
        lay.addWidget(self.warnings_lbl)

        btn = QtWidgets.QPushButton("重新检测")
        btn.clicked.connect(self.recheckRequested)
        lay.addWidget(btn, alignment=QtCore.Qt.AlignLeft)

    def show_report(self, report: EnvReport):
        self.tree.clear()

        def add(label: str, ok: bool, extra: str = ""):
            item = QtWidgets.QTreeWidgetItem([label, ("✓ 就绪" if ok else "✗ 缺失") + (f"  {extra}" if extra else "")])
            item.setForeground(1, QtCore.Qt.darkGreen if ok else QtCore.Qt.red)
            self.tree.addTopLevelItem(item)

        add("CUDA（GPU 加速）", report.cuda_available, "" if report.cuda_available else "将使用 CPU，速度变慢")
        add("llama-cpp-python（本地 LLM 引擎）", report.llama_cpp_available)
        add("WinRAR / UnRAR（RAR/CBR 输入）", report.unrar_available, report.unrar_path or "")

        for key, spec in MODEL_MANIFEST.items():
            cached = report.models_cached.get(key, False)
            optional = "" if spec.required else "（按需）"
            # 显示实际命中的权重路径：用户才能看出是不是复用到了和其它项目共享的那份，
            # 否则一个"已缓存"看不出用的到底是哪个文件
            add(f"{spec.label}（{spec.approx_size}）{optional}", cached, report.model_paths.get(key, ""))

        self.summary_lbl.setText(
            f"磁盘剩余空间：{report.disk_free_gb} GB　|　"
            + ("最小可跑集合已就绪" if report.ready_for_offline_run else "缺少最小可跑集合所需模型，请到「模型管理」补全")
        )

        if report.warnings:
            self.warnings_lbl.setText("注意：\n" + "\n".join(f"· {w}" for w in report.warnings))
        else:
            self.warnings_lbl.setText("")
