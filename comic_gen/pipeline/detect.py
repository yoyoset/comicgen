"""文本区域检测：RT-DETRv2 漫画文本/气泡检测模型（HuggingFace，Apache-2.0）。"""
from __future__ import annotations

import torch
from PIL import Image

from ..core.models import DIRECTION_HORIZONTAL, DIRECTION_VERTICAL, Region

# 模型类别（见模型卡片 ogkalu/comic-text-and-bubble-detector）
LABEL_BUBBLE = 0
LABEL_TEXT_BUBBLE = 1
LABEL_TEXT_FREE = 2


def _to_xyxy(bbox: list[int]) -> list[int]:
    x, y, w, h = bbox
    return [x, y, x + w, y + h]


def _iou(a: list[int], b: list[int]) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _containment(a: list[int], b: list[int]) -> float:
    """a 被 b 包含的比例（交集 / a 的面积）。"""
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    return inter / area_a if area_a > 0 else 0.0


def suppress_overlaps(
    regions: list[Region], iou_thr: float = 0.5, contain_thr: float = 0.9
) -> list[Region]:
    """类无关去重。

    RT-DETRv2 的 `post_process_object_detection` 没有 NMS：取 top-k 前把 queries×classes
    拍平，导致同一个 query 只要在多个类别上都超阈值，就会以完全相同的框输出多次
    （已实测复现：同一 query 在 text_bubble/text_free 两个类都超阈值时输出两个逐字节相同
    的框）。detect() 只丢弃 label 0（气泡形状），text_bubble/text_free 的重复框会原样
    保留，导致同一块文字被 OCR/翻译/渲染两次，在输出图上精确叠印。

    按 score 降序保留，丢弃与已保留框 IoU 超过 iou_thr、或被已保留框包含比例超过
    contain_thr 的框（包含判据覆盖"大框套小框"的情况，纯 IoU 会漏判）。
    """
    ordered = sorted(regions, key=lambda r: r.score, reverse=True)
    kept: list[Region] = []
    kept_boxes: list[list[int]] = []
    for r in ordered:
        rbox = _to_xyxy(r.bbox)
        # 包含判据要双向检查：处理顺序按 score 降序，若不检查"已保留框被新框包含"这个
        # 方向，一个 score 更高但面积更小的框会先入选，导致后面 score 更低的外层大框
        # 因为"新框包含已保留框"而不是"新框被已保留框包含"漏判为不重复。
        is_dup = any(
            _iou(rbox, kb) > iou_thr
            or _containment(rbox, kb) > contain_thr
            or _containment(kb, rbox) > contain_thr
            for kb in kept_boxes
        )
        if not is_dup:
            kept.append(r)
            kept_boxes.append(rbox)
    return kept


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
        for box, label, score in zip(res["boxes"], res["labels"], res["scores"]):
            label = int(label)
            if label == LABEL_BUBBLE:
                continue
            x1, y1, x2, y2 = [int(round(v)) for v in box.tolist()]
            x1, y1 = max(0, x1), max(0, y1)
            bw, bh = max(1, x2 - x1), max(1, y2 - y1)
            direction = DIRECTION_VERTICAL if bh > bw * 1.15 else DIRECTION_HORIZONTAL
            kind = "text_bubble" if label == LABEL_TEXT_BUBBLE else "text_free"
            regions.append(
                Region(bbox=[x1, y1, bw, bh], direction=direction, kind=kind, score=float(score))
            )
        regions = suppress_overlaps(regions)
        regions.sort(key=lambda r: (r.bbox[1], r.bbox[0]))
        return regions
