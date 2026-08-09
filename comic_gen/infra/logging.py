"""统一走标准 logging 模块，替代 pipeline 叶子模块里散落的
`print(..., file=sys.stderr)`。

之前只有 Runner 有注入的 `log` 回调（默认 print，UI 会重绑成 Qt 信号），叶子模块
（ocr.py/translate.py/render.py）没有这个回调可用，只能各自 print 到 stderr——
在冻结 exe 里落进 comicgen.log（和 PyTorch/transformers 自己的输出混在一起，无
时间戳无级别），在源码运行时可能哪儿都不去（取决于启动方式）。UI 日志面板从来
看不到这些消息。

这里不重新发明一套机制，而是把 logging 桥接到 Runner 已经在用、UI 也已经接好线
的那个 `self.log` 回调上：叶子模块只需要 `logging.getLogger(__name__)`，不需要
知道自己是被 UI 调用还是被脚本/测试调用。
"""
from __future__ import annotations

import logging
from pathlib import Path

LOGGER_NAME = "comic_gen"
logger = logging.getLogger(LOGGER_NAME)
logger.setLevel(logging.INFO)
# 不给它挂默认 handler——没人 attach 时应该安静，不该越俎代庖替调用方决定输出格式。
logger.propagate = False


class _CallbackHandler(logging.Handler):
    """把日志记录转发给一个 callable(str) -> None。Runner.log/Qt 信号都是这个形状。"""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback
        self.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._callback(self.format(record))
        except Exception:
            pass  # 转发失败不该反过来打断流水线


def attach_callback_handler(callback) -> logging.Handler:
    """把 comic_gen 这棵 logger 的记录转发给 callback。

    返回的 handler 用完必须传给 detach_handler——不 detach 会在多次调用之间累积，
    同一条日志被转发好几遍。callback 在每次 emit() 时才被调用（不是在这里捕获），
    所以调用方事后重新赋值 `runner.log = xxx` 也能被后续记录正确感知到。
    """
    handler = _CallbackHandler(callback)
    logger.addHandler(handler)
    return handler


def attach_job_file_handler(work_dir: Path) -> logging.Handler:
    """额外挂一个写进 `<work_dir>/run.log` 的文件 handler，带时间戳和级别——
    之前的 print(file=sys.stderr) 完全没有这些，事后没法知道某条警告是什么时候
    发生的。同样需要之后调用 detach_handler 卸载。

    mode="w"：run.log 是"本次运行"的日志。文件夹输入的工作目录（即输出目录）会在
    多次运行间复用，用追加模式的话每跑一次就叠一轮，长年使用无上限增长；改成每次
    运行从干净开始，恢复/排错以 pages.json/job.json 为准，run.log 只负责当次现场。
    """
    path = Path(work_dir) / "run.log"
    handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(handler)
    return handler


def detach_handler(handler: logging.Handler) -> None:
    logger.removeHandler(handler)
    handler.close()
