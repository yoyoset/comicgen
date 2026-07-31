"""流水线各阶段的接口契约。

用 `Protocol`（结构化类型）而不是 ABC——现有实现类不需要显式声明继承关系就能
满足契约，改动面最小；主要给类型检查/IDE 补全用，运行时不强制检查任何东西。

之前没有任何接口层：`docs/PROPOSAL.md` 写的"每个阶段都是可插拔模块（接口 + 多实现）"
是空头支票，实际只有翻译有基类 + 一张 dict 当"注册表"，且那张 dict 已经有特例了
（`MockTranslator` 不接受 `cfg`，`get_translator` 里手写 `if cls is MockTranslator`
绕过）；detect/ocr/inpaint/render 各只有一个具体类/函数，没有接口可言。加一个新的
OCR 后端要同时改 `ocr.py` 的构造分支、`runner.py` 的离线检查判断、`ui/workers.py`
的下载面板——`pipeline/registry.py` 把其中翻译引擎、OCR 后端两处收敛成单一数据源，
这里的 Protocol 是那份数据源的类型契约。
"""
from __future__ import annotations

from typing import Protocol

from PIL import Image

from .config import Config
from .models import Region


class Detector(Protocol):
    def detect(self, image: Image.Image) -> list[Region]: ...


class OcrBackend(Protocol):
    def recognize_regions(self, image: Image.Image, regions: list[Region]) -> None: ...


class Translator(Protocol):
    name: str

    def translate(self, texts: list[str], cfg: Config) -> list[str]: ...
