#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把某一卷某一页的「已检测区域」裁出来拼成一张编号图，交给视觉模型逐格转录。

为什么要这样做：本项目的中文版源书是**繁体中文**排版，走 manga-ocr（日文 OCR）+ 翻译
这条路会把「吧」认成「咆」、「了」认成「聊」、「怎么」认成「凭么」，错误是系统性的，
不是偶发。视觉模型直接读图能绕开这一层，但要保证「读出来的第 k 段字」对应「pages.json
里的第 k 个区域」，否则回填会错位。所以先把每个区域单独裁出来、标上序号、拼成联系人
表（contact sheet），模型只需回答 {序号: 文字}，序号天然对上，不存在对位歧义。

用法：
  python scripts/make_region_sheet.py "<系列目录>" "<卷名>" <页码...>
  python scripts/make_region_sheet.py "<系列目录>" "<卷名>" --all --out-dir <目录>

输入：
  <系列目录>/<卷名>.text.jsonl      每页每个区域的 bbox（retext_volume.py 也在用）
  原始卷：<原始卷根目录>\\<系列名>\\<卷名>.cbz

输出：
  <out-dir>/<页码>_sheet.png         编号联系人表（每格上方印序号）
  <out-dir>/<页码>.regions.json      序号 → bbox/原 OCR 文本，供回填与比对

之后把视觉模型返回的 {序号: 文本} 存成 fix json（格式与 retext_volume.py 一致），
再跑 retext_volume.py 即可只重排这一页。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CELL_H = 220          # 每格高度（像素），足够看清竖排小字
LABEL_H = 26          # 序号条高度
MAX_CELL_W = 520      # 单格最大宽度，过宽的气泡按比例缩放
COLS = 4              # 每行几格
GAP = 10
FONT = r"C:\Windows\Fonts\arialbd.ttf"


def load_texts(text_path: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for line in text_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        out[row["page"]] = row["regions"]
    return out


def build_sheet(page_img: Image.Image, regions: list[dict]) -> tuple[Image.Image, list[dict]]:
    """返回 (联系人表, 每格元数据)。region 的顺序就是 pages.json 里的下标顺序。"""
    crops = []
    for i, meta in enumerate(regions):
        x, y, w, h = meta["bbox"]
        pad = 4
        box = (max(0, x - pad), max(0, y - pad),
               min(page_img.width, x + w + pad), min(page_img.height, y + h + pad))
        crop = page_img.crop(box).convert("RGB")
        if crop.height == 0 or crop.width == 0:
            continue
        scale = min(CELL_H / crop.height, MAX_CELL_W / crop.width)
        crop = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))),
                           Image.LANCZOS)
        crops.append((i, crop))

    if not crops:
        return Image.new("RGB", (10, 10), "white"), []

    cell_w = max(c.width for _, c in crops) + GAP
    rows = (len(crops) + COLS - 1) // COLS
    sheet = Image.new("RGB", (cell_w * COLS, (CELL_H + LABEL_H + GAP) * rows), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype(FONT, 20)
    except OSError:                                    # pragma: no cover - 字体缺失时退默认
        font = ImageFont.load_default()

    meta_out = []
    for n, (idx, crop) in enumerate(crops):
        cx = (n % COLS) * cell_w
        cy = (n // COLS) * (CELL_H + LABEL_H + GAP)
        draw.text((cx + 4, cy + 2), f"#{idx}", fill="red", font=font)
        sheet.paste(crop, (cx, cy + LABEL_H))
        meta_out.append({"cell": n, "region": idx, "bbox": regions[idx]["bbox"]})
    return sheet, meta_out


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="make_region_sheet.py",
        description="把每页已检测区域裁出、编号、拼成联系人表，供视觉模型逐格转录。")
    ap.add_argument("series_dir", help="系列输出目录（含 <卷名>.text.jsonl）")
    ap.add_argument("stem", help="卷名（不含扩展名），如「死神 第18卷」")
    ap.add_argument("pages", nargs="*", help="页码，如 0084 0137；配合 --all 时忽略")
    ap.add_argument("--all", action="store_true", help="本卷全部有字页")
    ap.add_argument("--out-dir", default=str(Path(tempfile.gettempdir()) / "opencode" / "sheets"),
                    help="输出目录（默认 %%TEMP%%\\opencode\\sheets）")
    ap.add_argument("--raw-root", default=os.environ.get("COMICGEN_RAW_ROOT", "."),
                    help="原始卷根目录（其下有 <系列名>/<卷名>.cbz，默认读环境变量 COMICGEN_RAW_ROOT）")
    args = ap.parse_args(argv[1:])

    series_dir = Path(args.series_dir)
    stem = args.stem
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    text_path = series_dir / f"{stem}.text.jsonl"
    raw_cbz = Path(args.raw_root) / series_dir.name / f"{stem}.cbz"
    if not text_path.exists() or not raw_cbz.exists():
        print(f"缺少输入：{text_path} / {raw_cbz}")
        return 1

    texts = load_texts(text_path)
    if args.all:
        pages = sorted(texts)
    else:
        if not args.pages:
            ap.error("要么给页码，要么加 --all")
        pages = [p if p.endswith(".jpg") else f"{p}.jpg" for p in args.pages]

    made = 0
    with zipfile.ZipFile(raw_cbz) as z:
        for page in pages:
            regions = texts.get(page)
            if not regions:
                continue
            try:
                with z.open(page) as f:
                    img = Image.open(f).convert("RGB")
            except KeyError:
                print(f"  原始卷里没有 {page}")
                continue
            sheet, meta = build_sheet(img, regions)
            if not meta:
                continue
            sheet.save(out_dir / f"{Path(page).stem}_sheet.png")
            (out_dir / f"{Path(page).stem}.regions.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            made += 1
    print(f"生成 {made} 张联系人表 -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
