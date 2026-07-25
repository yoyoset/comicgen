"""抹字：文本掩码生成 + LaMa 修复，同时采样文字颜色供渲染使用。"""
from __future__ import annotations

import os

import cv2
import numpy as np
from PIL import Image

from ..core.models import Region


class Inpainter:
    def __init__(self, device: str = "cuda", model_path: str | None = None):
        from simple_lama_inpainting import SimpleLama

        # simple_lama_inpainting 读 LAMA_MODEL 环境变量决定用哪个权重文件；
        # 没绑定时必须 pop 掉，否则上一次运行/别的程序留下的同名变量会悄悄劫持这次加载。
        if model_path:
            os.environ["LAMA_MODEL"] = model_path
        else:
            os.environ.pop("LAMA_MODEL", None)

        try:
            self._lama = SimpleLama(device=device)
        except TypeError:
            self._lama = SimpleLama()

    @staticmethod
    def _region_mask(gray: np.ndarray, r: Region, pad: int = 4) -> tuple[np.ndarray, np.ndarray, tuple]:
        """在区域框内做 Otsu 二值化取文字像素，两种极性择优。

        返回 (膨胀后的抹字 mask, 未膨胀的取色 mask, 区域框)。
        """
        h, w = gray.shape
        x, y, bw, bh = r.bbox
        x1, y1 = max(0, x - pad), max(0, y - pad)
        x2, y2 = min(w, x + bw + pad), min(h, y + bh + pad)
        crop = gray[y1:y2, x1:x2]

        best = None
        for mode in (cv2.THRESH_BINARY_INV, cv2.THRESH_BINARY):
            _, m = cv2.threshold(crop, 0, 255, mode + cv2.THRESH_OTSU)
            ratio = float((m > 0).mean())
            if 0.005 < ratio < 0.6:  # 文字应是区域内的少数派
                score = abs(ratio - 0.15)
                if best is None or score < best[0]:
                    best = (score, m)
        raw = best[1] if best else np.zeros_like(crop)
        if best is None:  # 退化：整框视为待抹区域
            raw[pad : pad + bh, pad : pad + bw] = 255

        dilated = cv2.dilate(raw, np.ones((5, 5), np.uint8), iterations=2)
        full = np.zeros((h, w), np.uint8)
        full[y1:y2, x1:x2] = dilated
        return full, raw, (x1, y1, x2, y2)

    def inpaint_page(self, image: Image.Image, regions: list[Region]) -> Image.Image:
        """regions 应只含有文字的区域（上游已过滤空文本）。"""
        if not regions:
            return image.copy()
        img_rgb = np.array(image.convert("RGB"))
        gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
        mask_all = np.zeros(gray.shape, np.uint8)
        for r in regions:
            m, raw, (x1, y1, x2, y2) = self._region_mask(gray, r)
            # 取色用未膨胀 mask 的核心像素（腐蚀掉边缘抗锯齿过渡色），中位数更抗噪
            core = cv2.erode(raw, np.ones((3, 3), np.uint8), iterations=1)
            sel = core > 0 if (core > 0).any() else raw > 0
            pixels = img_rgb[y1:y2, x1:x2][sel]
            if len(pixels):
                r.color = [int(v) for v in np.median(pixels, axis=0)]
            mask_all |= m
        return self._lama(image.convert("RGB"), Image.fromarray(mask_all))
