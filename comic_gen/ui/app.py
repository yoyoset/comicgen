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


def _cmd_self_check(_args: list[str]) -> int:
    """打印本次运行实际用的 torch / llama.cpp 是哪一份、GPU 能不能用。退出码 0 = CUDA 可用。"""
    from ..core import gpu_components
    from ..pipeline._cuda_runtime import preload_cuda_runtime

    print(f"[self-check] gpu components active: {gpu_components.active_dir()}")
    print(f"[self-check] nvidia driver: {gpu_components.has_nvidia_driver()}")
    import torch

    ok = torch.cuda.is_available()
    print(f"[self-check] torch {torch.__version__} from {torch.__file__}")
    print(f"[self-check] torch.version.cuda={torch.version.cuda} cuda_available={ok}"
          + (f" device={torch.cuda.get_device_name(0)}" if ok else ""))
    preload_cuda_runtime()
    try:
        import llama_cpp

        print(f"[self-check] llama_cpp lib={llama_cpp.llama_cpp._base_path} "
              f"gpu_offload={llama_cpp.llama_supports_gpu_offload()}")
    except Exception as e:  # noqa: BLE001
        print(f"[self-check] llama_cpp failed: {e!r}")
    return 0 if ok else 1


def _cmd_install_gpu(_args: list[str]) -> int:
    """和「组件」页按钮调用的是同一个 gpu_components.install。"""
    import time

    from ..core import gpu_components

    last = [0.0, ""]

    def progress(stage: str, done: int, total: int) -> None:
        now = time.monotonic()
        if stage != last[1] or now - last[0] > 15 or done >= total:
            print(f"[install] {stage} {done / 1e6:,.0f}/{total / 1e6:,.0f} MB")
            last[0], last[1] = now, stage

    t0 = time.monotonic()
    try:
        st = gpu_components.install(progress=progress, log=lambda m: print(f"[install] {m}"))
    except gpu_components.InstallError as e:
        print(f"[install] FAILED: {e} | {e.technical}")
        return 1
    print(f"[install] {st.state} in {time.monotonic() - t0:.0f}s -> {st.path}")
    return 0


def _cmd_cli(args: list[str]) -> int:
    """`comicgen-ui.exe --cli <输入> -o <输出> ...`：参数同 python -m comic_gen.app.job_service。"""
    from ..app.job_service import main as cli_main
    from ..pipeline._cuda_runtime import preload_cuda_runtime

    preload_cuda_runtime()
    return cli_main(args)


_HEADLESS_COMMANDS = {
    "--self-check": _cmd_self_check,
    "--install-gpu-components": _cmd_install_gpu,
    "--cli": _cmd_cli,
}


def main():
    if getattr(sys, "frozen", False):
        # EXE：无控制台时把日志写文件，便于排错。mode="w"：每次启动从头开始，
        # 只保留最近一次会话——追加模式会让模型加载的 tqdm 进度条、torch/llama-cpp
        # 噪音等每次运行都叠进同一个文件，长期使用无上限增长。
        if sys.stdout is None:
            log = open(os.path.join(os.path.dirname(sys.executable), "comicgen.log"), "w", encoding="utf-8", buffering=1)
            sys.stdout = sys.stderr = log
    # 必须在任何 import torch / llama_cpp 之前：已下载的 GPU 组件接到导入路径最前面。
    # 开发态（仓库根下没有 components/）是空操作。
    try:
        from ..core import gpu_components
        st = gpu_components.activate()
        print(f"[gpu-components] {st.state} {st.path} {st.reason}".rstrip())
    except Exception as e:  # 激活失败只意味着这次用 CPU 版，不该让程序起不来
        print(f"[gpu-components] activate failed: {e!r}")
    if getattr(sys, "frozen", False):
        try:
            from ..pipeline._cuda_runtime import preload_cuda_runtime
            preload_cuda_runtime()
        except Exception:
            pass

    # 无界面子命令（EXE 没有控制台，输出都在 exe 同级 comicgen.log）。给排障和自动化验收用：
    # 打包后的 EXE 只有这一个入口，没有这几个开关就只能点界面才能验证冻结环境里的行为。
    argv = sys.argv[1:]
    if argv and argv[0] in _HEADLESS_COMMANDS:
        sys.exit(_HEADLESS_COMMANDS[argv[0]](argv[1:]))

    _install_excepthook()

    from PySide6 import QtWidgets

    from .main_window import MainWindow

    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
