"""OCR：日文走 manga-ocr（横排/竖排均支持）；其它语言走 EasyOCR。"""
from __future__ import annotations

import numpy as np
from PIL import Image

from ..core.config import Config
from ..core.models import Region


class OcrEngine:
    def __init__(self, cfg: Config):
        self.lang = cfg.source_lang
        if self.lang == "ja":
            from manga_ocr import MangaOcr

            self._mocr = MangaOcr()
            self._easy = None
        else:
            try:
                import easyocr
                import torch
            except ImportError as e:
                raise ImportError(
                    "非日文 OCR 需要 easyocr，请运行: .\\.venv\\Scripts\\python.exe -m pip install easyocr"
                ) from e
            code = {"en": ["en"], "zh": ["ch_sim", "en"]}.get(self.lang, ["en"])
            self._easy = easyocr.Reader(code, gpu=torch.cuda.is_available())
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
                    r.source_text = " ".join(t for _, t, _ in res).strip()
            except Exception as e:
                import sys
                print(f"[OCR 区域异常] bbox={r.bbox}: {e}", file=sys.stderr)
                r.source_text = ""