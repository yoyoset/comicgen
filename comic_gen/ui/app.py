"""comic-gen 桌面界面入口（PySide6/Qt）。

原生窗口、原生文件/文件夹选择（不经浏览器/temp）。
实际界面在 main_window.MainWindow；views/ 是纯展示，job_controller 是唯一的任务生命周期控制点。
"""
from __future__ import annotations

import os
import sys


def main():
    if getattr(sys, "frozen", False):
        # EXE：无控制台时把日志写文件，便于排错
        if sys.stdout is None:
            log = open(os.path.join(os.path.dirname(sys.executable), "comicgen.log"), "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stderr = log
        try:
            from ..pipeline._cuda_runtime import preload_cuda_runtime
            preload_cuda_runtime()
        except Exception:
            pass

    from PySide6 import QtWidgets

    from .main_window import MainWindow

    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
