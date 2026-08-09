"""comic-gen 桌面界面入口（PySide6/Qt）。

原生窗口、原生文件/文件夹选择（不经浏览器/temp）。
实际界面在 main_window.MainWindow；views/ 是纯展示，job_controller 是唯一的任务生命周期控制点。
"""
from __future__ import annotations

import os
import sys


def _install_excepthook() -> None:
    """未预期异常不再让程序无声闪退：完整堆栈落盘 + 弹一个说明在哪找日志的框。

    之前任何信号槽/事件处理器里的裸异常会直接打 Python 堆栈并终止进程，用户看到的
    是"程序突然没了"，不知道发生了什么、去哪找信息。这个钩子只是兜底——业务路径
    里的异常都应该被各处 try/except 接住，能走到这里的都是真 bug，需要完整诊断信息。
    """

    def _hook(exc_type, exc_value, exc_tb):
        import traceback

        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.getcwd()
        try:
            with open(os.path.join(base, "comicgen.log"), "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass
        try:
            from PySide6 import QtWidgets

            QtWidgets.QMessageBox.critical(
                None, "程序出错了",
                f"发生了未预期的错误，程序需要退出。完整信息已写入 comicgen.log：\n\n{exc_value}",
            )
        except Exception:
            pass

    sys.excepthook = _hook


def main():
    if getattr(sys, "frozen", False):
        # EXE：无控制台时把日志写文件，便于排错。mode="w"：每次启动从头开始，
        # 只保留最近一次会话——追加模式会让模型加载的 tqdm 进度条、torch/llama-cpp
        # 噪音等每次运行都叠进同一个文件，长期使用无上限增长。
        if sys.stdout is None:
            log = open(os.path.join(os.path.dirname(sys.executable), "comicgen.log"), "w", encoding="utf-8", buffering=1)
            sys.stdout = sys.stderr = log
        try:
            from ..pipeline._cuda_runtime import preload_cuda_runtime
            preload_cuda_runtime()
        except Exception:
            pass

    _install_excepthook()

    from PySide6 import QtWidgets

    from .main_window import MainWindow

    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
