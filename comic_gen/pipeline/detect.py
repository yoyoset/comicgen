"""文本区域检测：RT-DETRv2 漫画文本/气泡检测模型（HuggingFace，Apache-2.0）。"""
from __future__ import annotations

import torch
from PIL import Image

from ..core.models import DIRECTION_HORIZONTAL, DIRECTION_VERTICAL, Region

# 模型类别（见模型卡片 ogkalu/comic-text-and-bubble-detector）
LABEL_BUBBLE = 0
LABEL_TEXT_BUBBLE = 1
LABEL_TEXT_FREE = 2


class ComicDetector:
    def __init__(self, repo: str, device: str = "cuda", threshold: float = 0.3):
        from transformers import AutoImageProcessor, RTDetrV2ForObjectDetection

        self.device = device if (device == "cuda" and torch.cuda.is_available()) else "cpu"
        self.processor = AutoImageProcessor.from_pretrained(repo)
        self.model = RTDetrV2ForObjectDetection.from_pretrained(repo).to(self.device).eval()
        self.threshold = threshold

    @torch.no_grad()
    def detect(self, image: Image.Image) -> list[Region]:
        w, h = image.size
        inputs = self.processor(images=image, return_tensors="pt").to(self.device)
        outputs = self.model(**inputs)
        res = self.processor.post_process_object_detection(
            outputs, target_sizes=[(h, w)], threshold=self.threshold
        )[0]
        regions = []
        for box, label in zip(res["boxes"], res["labels"]):
            label = int(label)
            if label == LABEL_BUBBLE:
                continue
            x1, y1, x2, y2 = [int(round(v)) for v in box.tolist()]
            x1, y1 = max(0, x1), max(0, y1)
            bw, bh = max(1, x2 - x1), max(1, y2 - y1)
            direction = DIRECTION_VERTICAL if bh > bw * 1.15 else DIRECTION_HORIZONTAL
            kind = "text_bubble" if label == LABEL_TEXT_BUBBLE else "text_free"
            regions.append(Region(bbox=[x1, y1, bw, bh], direction=direction, kind=kind))
        regions.sort(key=lambda r: (r.bbox[1], r.bbox[0]))
        return regions
