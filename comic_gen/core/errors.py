"""项目自定义异常层次：分用户文案与技术细节两层，替换裸 ValueError/RuntimeError。

之前全项目零自定义异常类型，一律是 ValueError/RuntimeError 包一句中文消息，调用方
只能靠 message 里的关键词猜错误类型（例如旧版 `pytest.raises(ValueError,
match="覆盖原图")`）；job 级别的异常会被 ui/workers.py 整个 `traceback.format_exc()`
糊进模态框，用户看到的第一件事是 Python 堆栈，而 translate.py 里其实已经写好了很
清楚的中文提示（比如"设置里绑定的 X 文件已找不到，请到「模型管理」页重新定位"），
只是被堆栈盖住了。

`ComicGenError` 把这两层显式分开：user_message 是可以直接进对话框的人话，
technical 是原始异常信息（默认等于 user_message），供日志使用。
"""
from __future__ import annotations

import traceback
from enum import Enum


class Stage(str, Enum):
    """流水线的四个阶段，字符串值与 Page.status 前缀一致，用于结构化标注每页的失败阶段。"""

    DETECT = "detect"
    OCR = "ocr"
    TRANSLATE = "translate"
    RENDER = "render"


class ComicGenError(Exception):
    def __init__(self, user_message: str, technical: str | None = None):
        super().__init__(user_message)
        self.user_message = user_message
        self.technical = technical if technical is not None else user_message


class ModelError(ComicGenError):
    """模型文件缺失/绑定失效/大小不符，或所需的推理运行时（如 llama-cpp-python、
    easyocr）未安装。"""


class InputError(ComicGenError):
    """输入文件/格式不支持、内容为空，或任务参数（引擎/语言组合等）不合法。"""


class OutputError(ComicGenError):
    """输出路径不安全（会覆盖原图）或无法校验。"""


def format_job_failure(e: Exception) -> str:
    """异常 -> 呈现给用户的一行/多行字符串。UI（ui/workers.py）和 CLI
    （app/job_service.py）共用同一个函数，放在这里（而不是 ui/workers.py）是因为
    CLI 不该为了这一个函数去依赖 PySide6——这个函数本身从来没有用到过 Qt。

    ComicGenError 已经带了 pipeline 各层费心写好的人话文案（比如"设置里绑定的 X
    文件已找不到，请到「模型管理」页重新定位"），不分青红皂白地把
    traceback.format_exc() 糊在后面会把这句话埋在一堆 Python 堆栈下面。其它未预期
    的异常仍然带全部 traceback——那类失败往往是真的程序 bug，需要完整诊断信息，
    不能藏起来。

    OSError 单独处理：用户最常撞的"路径无效 / 无写权限 / 磁盘满"都落在这里，不是
    程序 bug。OSError 消息本身就带失败路径和 errno，够定位；完整堆栈仍会进
    run.log（ui/workers.py 的 except 分支负责落盘），对话框不需要再糊一段堆栈。
    """
    if isinstance(e, ComicGenError):
        return f"失败：{e.user_message}"
    if isinstance(e, OSError):
        return (
            f"失败：文件操作失败：{e}\n"
            "请检查输出位置是否可写、路径是否有效、磁盘空间是否充足。"
        )
    return f"失败：{e}\n{traceback.format_exc()}"
