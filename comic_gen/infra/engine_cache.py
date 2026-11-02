"""按依赖字段签名判断的懒加载缓存：签名不变则复用，变了就重建。

从 `pipeline.runner.Runner._get_or_rebuild` 搬出来，成为独立、可单测的组件——它
不需要知道 Runner 是什么，只需要"对象怎么建"（factory）和"什么时候该重建"
（比较签名）。原来内嵌在 Runner 里、靠 `getattr(self, cache_attr)` 这种字符串属性名
拼接的写法，挪出来后每个引擎持有自己的 `EngineCache()` 实例，不再需要成对的
`_xxx`/`_xxx_sig` 两个属性名手动维护。

这里没有把"哪个字段算依赖"这件事也搬进 pipeline/registry.py 做成声明式的东西——
detect/inpaint 都只有一个实现，不像翻译引擎/OCR 后端那样有"选哪个"的问题，硬凑
一层"注册表声明依赖字段"只会绕得更远，消灭不了任何真实重复。
"""
from __future__ import annotations

from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class EngineCache(Generic[T]):
    def __init__(self):
        self._value: T | None = None
        self._sig: object = None

    def get_or_rebuild(self, sig: object, factory: Callable[[], T]) -> T:
        if self._value is None or self._sig != sig:
            self._value = factory()
            self._sig = sig
        return self._value

    def clear(self) -> None:
        """丢弃缓存值并清空签名。调用方负责先对旧值做资源清理（如释放显存）——
        这里只管"下一次 get_or_rebuild 必然重建"这一条契约。"""
        self._value = None
        self._sig = None
