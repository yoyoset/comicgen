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
