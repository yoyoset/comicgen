r"""只读诊断：对单张图片跑检测(+OCR)，输出重复框/低置信度等排版错乱线索。

不写任何产物、不修改任何文件。用于在改动 detect.py 的去重逻辑前，
先在真实（尤其是宣传/情报类）漫画页上量化"同一块文字被检测器输出了几次"这类问题。

用法：
    .\.venv\Scripts\python.exe scripts\diagnose_page.py <图片路径> [--lang ja|en|zh|other] [--threshold 0.3]
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from comic_gen.core.config import Config
from comic_gen.pipeline.detect import LABEL_BUBBLE, ComicDetector
from scripts.region_quality import format_batch_report


def batch_report_main(pages_path: Path) -> None:
    """Read pages.json and optional sibling job.json without writing either file."""
    data = json.loads(pages_path.read_text(encoding="utf-8"))
    pages = data.get("pages", data) if isinstance(data, dict) else data
    stage_seconds = {}
    stage_ranges = []
    job_path = pages_path.with_name("job.json")
    if job_path.exists():
        job = json.loads(job_path.read_text(encoding="utf-8"))
        for name, stage in job.get("stages", {}).items():
            if isinstance(stage, dict) and isinstance(stage.get("duration_s"), (int, float)):
                page_count = stage.get("page_count")
                label = f"{name}({page_count}页)" if page_count else name
                stage_seconds[label] = float(stage["duration_s"])
                stage_ranges.append(f"{name}={stage['duration_s']}s/{page_count or '?'}页")
    missing = [name for name in ("detect", "ocr") if name not in stage_seconds]
    if missing:
        stage_ranges.append(f"缺失: {', '.join(missing)}")
    note = "；".join(stage_ranges)
    print(format_batch_report(pages, stage_seconds=stage_seconds, stage_note=note))


def iou(a: list[int], b: list[int]) -> float:
    """a/b 为 [x1,y1,x2,y2]。"""
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def containment(a: list[int], b: list[int]) -> float:
    """a 被 b 包含的比例（交集 / a 的面积）。"""
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    return inter / area_a if area_a > 0 else 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("image", nargs="?", help="待诊断的图片路径")
    ap.add_argument("--pages-json", type=Path, help="对 pages.json 做只读批量质量统计")
    ap.add_argument("--lang", default="ja", choices=["ja", "en", "zh", "other"])
    ap.add_argument("--threshold", type=float, default=0.3)
    ap.add_argument("--iou-thr", type=float, default=0.5, help="判定为重叠对的 IoU 阈值")
    ap.add_argument("--contain-thr", type=float, default=0.9, help="判定为包含对的比例阈值")
    ap.add_argument("--ocr", action="store_true", help="附带跑 OCR（较慢，默认关闭）")
    args = ap.parse_args()

    if args.pages_json:
        batch_report_main(args.pages_json)
        return
    if not args.image:
        ap.error("需要图片路径，或使用 --pages-json")

    img_path = Path(args.image)
    if not img_path.exists():
        print(f"文件不存在: {img_path}", file=sys.stderr)
        sys.exit(1)

    cfg = Config(source_lang=args.lang, det_threshold=args.threshold)
    print(f"加载检测模型（repo={cfg.detector_repo}, device={cfg.device}, threshold={args.threshold}）…")
    det = ComicDetector(cfg.detector_repo, cfg.device, cfg.det_threshold)

    img = Image.open(img_path).convert("RGB")
    w, h = img.size
    print(f"图片: {img_path.name}  尺寸: {w}x{h}")

    import torch

    inputs = det.processor(images=img, return_tensors="pt").to(det.device)
    with torch.no_grad():
        outputs = det.model(**inputs)
    res = det.processor.post_process_object_detection(
        outputs, target_sizes=[(h, w)], threshold=cfg.det_threshold
    )[0]
    raw_boxes = res["boxes"].tolist()
    raw_labels = [int(x) for x in res["labels"].tolist()]
    raw_scores = [float(x) for x in res["scores"].tolist()]

    from collections import Counter

    print(f"\n检测框总数（阈值 {cfg.det_threshold} 下）: {len(raw_boxes)}")
    print(f"按 label 分布: {dict(Counter(raw_labels))}  (0=bubble 1=text_bubble 2=text_free)")

    kept = [
        {"box": [round(v) for v in b], "label": l, "score": s}
        for b, l, s in zip(raw_boxes, raw_labels, raw_scores)
        if l != LABEL_BUBBLE
    ]
    print(f"丢弃 label 0(bubble) 后剩余: {len(kept)}")

    print("\n逐框明细:")
    for i, r in enumerate(kept):
        print(f"  [{i}] label={r['label']} score={r['score']:.3f} box={r['box']}")

    pairs = list(itertools.combinations(range(len(kept)), 2))
    overlap_pairs = []
    contain_pairs = []
    for i, j in pairs:
        v = iou(kept[i]["box"], kept[j]["box"])
        if v > args.iou_thr:
            overlap_pairs.append((i, j, v))
        ci = containment(kept[i]["box"], kept[j]["box"])
        cj = containment(kept[j]["box"], kept[i]["box"])
        if ci > args.contain_thr and v <= args.iou_thr:
            contain_pairs.append((i, j, ci, "i_in_j"))
        elif cj > args.contain_thr and v <= args.iou_thr:
            contain_pairs.append((i, j, cj, "j_in_i"))

    print(f"\n=== IoU > {args.iou_thr} 的重叠框对: {len(overlap_pairs)} ===")
    for i, j, v in overlap_pairs:
        print(
            f"  IoU={v:.3f}  [{i}] label={kept[i]['label']} score={kept[i]['score']:.2f} {kept[i]['box']}"
            f"  vs  [{j}] label={kept[j]['label']} score={kept[j]['score']:.2f} {kept[j]['box']}"
        )

    print(f"\n=== 包含关系（未计入上面重叠对，比例 > {args.contain_thr}）: {len(contain_pairs)} ===")
    for i, j, ratio, which in contain_pairs:
        small, big = (i, j) if which == "i_in_j" else (j, i)
        print(
            f"  [{small}] 被 [{big}] 包含 {ratio:.1%}  "
            f"{kept[small]['box']} in {kept[big]['box']}"
        )

    if args.ocr:
        print("\n加载 OCR 引擎…")
        from comic_gen.core.models import Region
        from comic_gen.pipeline.ocr import OcrEngine

        ocr = OcrEngine(cfg)
        regions = [
            Region(
                bbox=[r["box"][0], r["box"][1], r["box"][2] - r["box"][0], r["box"][3] - r["box"][1]],
                direction="horizontal",
                kind="text_bubble" if r["label"] == 1 else "text_free",
            )
            for r in kept
        ]
        ocr.recognize_regions(img, regions)
        print("\n=== 逐框 OCR 原文 ===")
        for i, (r, reg) in enumerate(zip(kept, regions)):
            print(f"  [{i}] score={r['score']:.2f} box={r['box']}: {reg.source_text!r}")

    print(
        f"\n汇总: 检测框={len(raw_boxes)} 保留(非bubble)={len(kept)} "
        f"重叠对(IoU>{args.iou_thr})={len(overlap_pairs)} 包含对={len(contain_pairs)}"
    )


if __name__ == "__main__":
    main()
