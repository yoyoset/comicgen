"""抹字：文本掩码生成 + LaMa 修复，同时采样文字颜色供渲染使用。"""
from __future__ import annotations

import os

import cv2
import numpy as np
from PIL import Image

from ..core.models import Region


def resolve_device(requested: str, cuda_available: bool) -> str:
    """请求 cuda 但没有可用 CUDA 时退回 cpu——和 detect.ComicDetector 同一条规则。"""
    return "cuda" if requested == "cuda" and cuda_available else "cpu"


class Inpainter:
    def __init__(self, device: str = "cuda", model_path: str | None = None):
        import torch
        from simple_lama_inpainting import SimpleLama
        from simple_lama_inpainting.models.model import LAMA_MODEL_URL
        from simple_lama_inpainting.utils import download_model

        # 不走 SimpleLama.__init__：它 torch.jit.load(path) 不带 map_location，而 big-lama.pt
        # 里的张量是在 CUDA 上存的——CPU 版 torch 加载直接报 "Could not run
        # 'aten::empty_strided' with arguments from the 'CUDA' backend"（2026-11-05 在 CPU 安装包
        # 上实测：--device cpu 也一样整页抹字失败）。以前只发 CUDA 整包，这条路径从没跑到过。
        # 它也不做"请求 cuda 但没 CUDA"的回退，这里补上。
        # LAMA_MODEL 环境变量曾用来给 SimpleLama 传路径；现在直接传，顺手清掉防止旧值劫持别的调用方。
        os.environ.pop("LAMA_MODEL", None)
        dev = resolve_device(device, torch.cuda.is_available())
        path = model_path or download_model(LAMA_MODEL_URL)
        if not os.path.exists(path):
            raise FileNotFoundError(f"LaMa 模型文件不存在：{path}")
        lama = SimpleLama.__new__(SimpleLama)
        lama.model = torch.jit.load(path, map_location=dev)
        lama.model.eval()
        lama.device = torch.device(dev)
        self.device = dev
        self._lama = lama

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
            # 文字应是区域内的少数派：0.005~0.6 与目标占比 0.15 是保守的视觉启发式，
            # 未在真实漫画素材上校准（最坏后果是取色/抹字效果差，不会出错）；
            # 有素材后可用 scripts/diagnose_page.py 对同一批页比对两种阈值产出再定。
            if 0.005 < ratio < 0.6:
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
        # 上游（open_page / review_session）打开图时已经统一转成 RGB，这里直接复用，
        # 不再 convert("RGB")——PIL 的 convert 在同模式下也会复制像素缓冲，每页白做两次
        # 整图拷贝（实测 800x1200 每页约 1.3ms）。非 RGB 输入（其它调用方）仍走 convert。
        rgb = image if image.mode == "RGB" else image.convert("RGB")
        img_rgb = np.array(rgb)
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
        return self._lama(rgb, Image.fromarray(mask_all))
