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

        if entry.key == "ocr_ja":
            self._mocr = entry.factory(cfg)
            self._easy = None
        else:
            try:
                self._easy = entry.factory(cfg)
            except ImportError as e:
                raise ModelError(
                    "非日文 OCR 需要 easyocr，请运行: .\\.venv\\Scripts\\python.exe -m pip install easyocr",
                    technical=str(e),
                ) from e
            self._mocr = None

    def recognize_regions(self, image: Image.Image, regions: list[Region], pad: int = 6):
        w, h = image.size
        for r in regions:
            x, y, bw, bh = r.bbox
            crop = image.crop(
                (max(0, x - pad), max(0, y - pad), min(w, x + bw + pad), min(h, y + bh + pad))
            )
            try:
                if self._mocr is not None:
                    r.source_text = self._mocr(crop).strip()
                else:
                    res = self._easy.readtext(np.array(crop))
                    r.source_text = assemble_easyocr_text(res)
            except Exception as e:
                logger.warning("OCR 区域异常 bbox=%s: %s", r.bbox, e)
                r.source_text = ""