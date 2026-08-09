"""OCR：日文走 manga-ocr（横排/竖排均支持）；其它语言走 EasyOCR。"""
from __future__ import annotations

import numpy as np
from PIL import Image

from ..core.config import Config
from ..core.errors import InputError, ModelError
from ..core.models import Region
from ..infra.logging import logger

# EasyOCR 对每个识别片段都会给一个置信度分数，原实现完全丢弃了它，装饰性花纹/logo
# 被幻觉成的乱码片段会和真文字原样拼在一起。0.3 是一个保守的起点阈值（不是从真实
# 问题页实测调出来的——那两张页面只有截图，不是可供流水线读取的文件；等有真实素材
# 能跑 scripts/diagnose_page.py --ocr 时再回来校准）。
EASYOCR_MIN_CONF = 0.3


def assemble_easyocr_text(results, min_conf: float = EASYOCR_MIN_CONF) -> str:
    """把 EasyOCR 的逐片段结果拼成一段文字：按位置（先上后左）排序，丢弃低置信度片段。

    EasyOCR 的 readtext() 不保证按阅读顺序返回片段，原实现直接顺序拼接会把句子打乱；
    results 里每项是 (四角点坐标, 文本, 置信度)。
    """
    kept = [(box, text, conf) for box, text, conf in results if conf >= min_conf]
    kept.sort(key=lambda item: (item[0][0][1], item[0][0][0]))  # 左上角 (y, x)
    return " ".join(text for _, text, _ in kept).strip()


class EasyOcrBackend:
    """把 easyocr.Reader.readtext 适配成与 manga-ocr 一致的接口：输入裁剪图、输出文字。

    有了这层适配，OcrEngine 不再需要按后端身份分支（旧代码 `if self._mocr is not None`）
    ——两个后端都是"可调用对象"，加第三个后端也只是在 registry 里加一条 factory。
    """

    def __init__(self, reader):
        self._reader = reader

    def __call__(self, crop: Image.Image) -> str:
        return assemble_easyocr_text(self._reader.readtext(np.array(crop)))


class OcrEngine:
    def __init__(self, cfg: Config):
        # 用哪个后端由 registry.OCR_REGISTRY 的 languages 集合决定，不再是这里手写
        # 的 if/else——加一个新后端只需要在 registry.py 里加一条 Entry，不需要再回
        # 来改这个构造函数。
        from .registry import ocr_backend_for

        self.lang = cfg.source_lang
        entry = ocr_backend_for(self.lang)
        if entry is None:
            raise InputError(f"不支持的源语言：{self.lang}")

        try:
            self._recognizer = entry.factory(cfg)
        except ImportError as e:
            # 兜底：第三方后端自己的工厂没把依赖缺失转成 ModelError 时，统一转一次，
            # 不能把"缺依赖"伪装成"流水线崩溃"糊给用户。自带后端的错误已由各工厂
            # 自行翻译成人话（如 easyocr 的安装指引），不会走到这里。
            raise ModelError(f"OCR 后端加载失败：{e}", technical=str(e)) from e

    def recognize_regions(self, image: Image.Image, regions: list[Region], pad: int = 6):
        w, h = image.size
        for r in regions:
            x, y, bw, bh = r.bbox
            crop = image.crop(
                (max(0, x - pad), max(0, y - pad), min(w, x + bw + pad), min(h, y + bh + pad))
            )
            try:
                r.source_text = self._recognizer(crop).strip()
            except Exception as e:
                logger.warning("OCR 区域异常 bbox=%s: %s", r.bbox, e)
                r.source_text = ""