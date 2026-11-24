#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""找出「检测阶段漏框」的气泡：对原始页重跑检测，与现有 text.jsonl 区域做差集。

背景（2026-11-06 实测）：七龙珠 17 卷 0044 上有「僕は——」这种正儿八经的台词气泡，
`text.jsonl` 里根本没有对应区域——RT-DETR 当初没框到。回填脚本只处理已有区域，
所以这类气泡会原样留着日文。**这不是翻译或渲染能修的，必须补框。**

做法不是自动补：本脚本只产出**候选**（bbox + 分数 + 裁图），
由人逐张看裁图判断「这确实是漏掉的气泡/文字」还是「花纹、拟声、误检」，
然后把确认的写进 `<卷名>.extra.json`：
  {"0044.jpg": [{"bbox": [x, y, w, h], "text": "我……"}, ...]}
`retext_volume.py` 会把这些区域一起抹字+排字。

用法：
  python scripts/detect_extra.py "<系列目录>" "<卷名>" 0044 0045
  python scripts/detect_extra.py "<系列目录>" "<卷名>" 0044 --threshold 0.15
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

DEFAULT_RAW_ROOT = os.environ.get("COMICGEN_RAW_ROOT", ".")
REPO = "ogkalu/comic-text-and-bubble-detector"


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    return inter / float(aw * ah + bw * bh - inter)


def _containment(a, b) -> float:
    """a 有多少比例落在 b 里（漏检常是「已有区域旁边多出一条」）。"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    return (x2 - x1) * (y2 - y1) / float(aw * ah)


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="detect_extra.py")
    ap.add_argument("series_dir")
    ap.add_argument("stem")
    ap.add_argument("pages", nargs="+")
    ap.add_argument("--raw-root", default=DEFAULT_RAW_ROOT)
    ap.add_argument("--threshold", type=float, default=0.18,
                    help="比生产用的 0.3 低，宁多勿漏，再由人筛")
    ap.add_argument("--out-dir", default=str(Path(tempfile.gettempdir()) / "opencode" / "extra"))
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args(argv[1:])

    from comic_gen.pipeline.detect import ComicDetector

    series_dir = Path(args.series_dir)
    raw = Path(args.raw_root) / series_dir.name / f"{args.stem}.cbz"
    text_path = series_dir / f"{args.stem}.text.jsonl"
    if not raw.exists() or not text_path.exists():
        print(f"缺少输入：{raw} / {text_path}")
        return 1

    known: dict[str, list[list[int]]] = {}
    for line in text_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        known[row["page"]] = [list(m["bbox"]) for m in row["regions"]]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    det = ComicDetector(REPO, device=args.device, threshold=args.threshold)

    report = {}
    with zipfile.ZipFile(raw) as z:
        for page in args.pages:
            name = page if page.endswith(".jpg") else f"{page}.jpg"
            if name not in z.namelist():
                print(f"原始卷里没有 {name}")
                continue
            with z.open(name) as f:
                img = Image.open(f).convert("RGB")
            boxes = known.get(name, [])
            cands = []
            for r in det.detect(img):
                b = list(r.bbox)
                if any(_iou(b, k) > 0.25 or _containment(b, k) > 0.6 for k in boxes):
                    continue
                cands.append({"bbox": b, "score": round(r.score, 3)})
            cands.sort(key=lambda c: (c["bbox"][1], c["bbox"][0]))
            if cands:
                _save_sheet(img, cands, out_dir / f"{Path(name).stem}_extra.png")
            report[name] = cands
            print(f"{name}: 候选 {len(cands)} 个（已有区域 {len(boxes)} 个）")

    out_json = out_dir / f"{args.stem}.extra_candidates.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> {out_json}")
    return 0


def _save_sheet(img: Image.Image, cands: list[dict], path: Path) -> None:
    cells = []
    for i, c in enumerate(cands):
        x, y, w, h = c["bbox"]
        crop = img.crop((x, y, x + w, y + h)).convert("RGB")
        scale = min(200 / max(1, crop.height), 420 / max(1, crop.width))
        if scale < 1:
            crop = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))),
                               Image.LANCZOS)
        cells.append((i, crop))
    gap, label_h = 8, 22
    cell_w = max(c.width for _, c in cells) + gap
    cols = 4
    rows = (len(cells) + cols - 1) // cols
    cell_h = max(c.height for _, c in cells) + label_h
    sheet = Image.new("RGB", (cell_w * cols, cell_h * rows), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
    for n, (idx, crop) in enumerate(cells):
        cx, cy = (n % cols) * cell_w, (n // cols) * cell_h
        draw.text((cx + 4, cy + 2), f"#{idx}", fill="red", font=font)
        sheet.paste(crop, (cx, cy + label_h))
    sheet.save(path)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
