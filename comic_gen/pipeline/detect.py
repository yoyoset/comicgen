"""文本区域检测：RT-DETRv2 漫画文本/气泡检测模型（HuggingFace，Apache-2.0）。"""
from __future__ import annotations

import torch
from PIL import Image

from ..core.models import DIRECTION_HORIZONTAL, DIRECTION_VERTICAL, Region

# 模型类别（见模型卡片 ogkalu/comic-text-and-bubble-detector）
LABEL_BUBBLE = 0
LABEL_TEXT_BUBBLE = 1
LABEL_TEXT_FREE = 2

# T1 实测的 493 页样本中，score < 0.4 占 7.0%。这是基于该样本的保守起点值，尚未
# 在其它漫画素材上校准；先只使用 score 规则，不把未验证的面积/长宽比规则带入生产。
REGION_QUALITY_MIN_SCORE = 0.4


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

    实现用均匀网格空间索引（cell 边长取各框最大边长的中位数，漫画同页的气泡尺度接近）
    把"每个候选只和同 cell 里的已保留框比较"从对全部已保留框的 O(n²) 扫描降成期望
    O(n)。正确性依据：两框有重叠 ⇔ 重叠区域里的任意点所在 cell 同时被两框占据；a
    完全包含 b 时 b 占据的每个 cell 也必被 a 占据。因此候选框只需在自己占到的 cell
    里找，漏不了任何应被判重复的框。病态布局（一个整页大框）最坏仍退化回 O(n)，但
    只针对那一个框，实测样本里不会出现（见 tests/test_detect_dedupe.py 的差分测试）。
    """
    ordered = sorted(regions, key=lambda r: r.score, reverse=True)
    if not ordered:
        return []

    boxes = [_to_xyxy(r.bbox) for r in ordered]
    dims = sorted(max(b[2] - b[0], b[3] - b[1]) for b in boxes)
    cell = max(1, dims[len(dims) // 2])

    grid: dict[tuple[int, int], list[int]] = {}
    kept: list[Region] = []
    for i, rbox in enumerate(boxes):
        x1, y1, x2, y2 = rbox
        c_x1, c_y1 = x1 // cell, y1 // cell
        c_x2, c_y2 = x2 // cell, y2 // cell
        cand_ids = {
            j
            for cy in range(c_y1, c_y2 + 1)
            for cx in range(c_x1, c_x2 + 1)
            for j in grid.get((cx, cy), ())
        }
        is_dup = any(
            # 包含判据要双向检查：处理顺序按 score 降序，若不检查"已保留框被新框包含"
            # 这个方向，一个 score 更高但面积更小的框会先入选，导致后面 score 更低的
            # 外层大框因为"新框包含已保留框"而不是"新框被已保留框包含"漏判为不重复。
            _iou(rbox, boxes[j]) > iou_thr
            or _containment(rbox, boxes[j]) > contain_thr
            or _containment(boxes[j], rbox) > contain_thr
            for j in cand_ids
        )
        if is_dup:
            continue
        kept.append(ordered[i])
        for cy in range(c_y1, c_y2 + 1):
            for cx in range(c_x1, c_x2 + 1):
                grid.setdefault((cx, cy), []).append(i)
    return kept


def filter_region_quality(
    regions: list[Region], min_score: float = REGION_QUALITY_MIN_SCORE
) -> list[Region]:
    """丢弃低于保守起点分数的区域，返回新列表且不修改输入。"""
    return [region for region in regions if region.score >= min_score]


def reading_order(regions: list[Region], rtl: bool = False, band: int | None = None) -> list[Region]:
    """按阅读顺序排序：先按行分组（y 相近的框算同一行），行内按方向排序。

    默认（rtl=False）行内左→右，适合西式/中文横排漫画。日漫需要 rtl=True（右→左）——
    旧实现固定用 (y, +x) 排序，对日漫是反的：实测同一行两个气泡会把"回复"排在"提问"前面，
    整页的编号批量翻译因此拿到错乱的对话顺序，上下文推断（人称代词等）全错。

    band 未指定时取本页所有框高度的中位数——漫画气泡大小差异很大，写死一个像素常数
    对小字页/大字页两头不讨好。
    """
    if not regions:
        return []
    if band is None:
        heights = sorted(r.bbox[3] for r in regions)
        band = heights[len(heights) // 2]
    band = max(1, band)

    def sort_key(r: Region):
        row = r.bbox[1] // band
        x = -r.bbox[0] if rtl else r.bbox[0]
        return (row, x)

    return sorted(regions, key=sort_key)


class ComicDetector:
    def __init__(self, repo: str, device: str = "cuda", threshold: float = 0.3):
        from transformers import AutoImageProcessor, RTDetrV2ForObjectDetection

        self.device = device if (device == "cuda" and torch.cuda.is_available()) else "cpu"
        self.processor = AutoImageProcessor.from_pretrained(repo)
        # transformers 的存根把 from_pretrained 类方法误标成实例 __call__，repo 字符串
        # 会被判成"不是 PreTrainedModel"——这是存根缺陷不是真实问题，运行时行为正常。
        self.model = RTDetrV2ForObjectDetection.from_pretrained(repo).to(self.device).eval()  # type: ignore[arg-type]
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
            x1, y1, x2, y2 = [round(v) for v in box.tolist()]
            x1, y1 = max(0, x1), max(0, y1)
            bw, bh = max(1, x2 - x1), max(1, y2 - y1)
            direction = DIRECTION_VERTICAL if bh > bw * 1.15 else DIRECTION_HORIZONTAL
            kind = "text_bubble" if label == LABEL_TEXT_BUBBLE else "text_free"
            regions.append(
                Region(bbox=[x1, y1, bw, bh], direction=direction, kind=kind, score=float(score))
            )
        regions = filter_region_quality(suppress_overlaps(regions))
        # 不在这里排序：唯一的调用方（runner._detect_page）会用 reading_order 按
        # 阅读顺序重排（rtl 时还和这里的固定 (y, x) 顺序相反），这里的排序是纯粹的
        # 死工作，还会造成"detect 已经排过序"的错觉。排序职责只属于 reading_order。
        return regions
