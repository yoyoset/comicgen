#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校对回填：把人工校对过的文本重新排到已完成的成品卷里（只重做受影响页）。

批量脚本产出：
  <系列>/<卷名> [300dpi 简].cbz     成品
  <系列>/<卷名>.text.jsonl          每页每个框的最终文本（校对依据）

校对产物：
  <系列>/<卷名>.fix.json            {"<页面文件名>": {"0": "修正后的文本", ...}, ...}
  索引对应 .text.jsonl 里该页 regions 的顺序；空字符串 = 该框不排字（保留原图）。

用法：
  python scripts/retext_volume.py "<页面 jsonl 所在目录>" "<卷名>" "<.fix.json>"
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import zhconv
from PIL import Image

TARGET_LONG_EDGE = 3030
FONT = r"C:\Windows\Fonts\msyh.ttc"


def guess_direction(page: Image.Image, bbox) -> str:
    import numpy as np

    x, y, w, h = bbox
    crop = page.crop((x, y, x + w, y + h)).convert("L")
    if min(crop.size) < 8:
        return "horizontal"
    ink = np.array(crop) < 128
    col_gap = float((ink.sum(axis=0) == 0).mean())
    row_gap = float((ink.sum(axis=1) == 0).mean())
    return "vertical" if col_gap - row_gap > 0.05 else "horizontal"


def main(argv: list[str]) -> int:
    series_dir = Path(argv[1])
    stem = argv[2]
    fix_path = Path(argv[3]) if len(argv) > 3 and argv[3] != "-" else None

    from comic_gen.core.config import Config
    from comic_gen.core.models import Region
    from comic_gen.pipeline.inpaint import Inpainter
    from comic_gen.pipeline.proofread import apply_confusions
    from comic_gen.pipeline.render import render_page

    src_path = series_dir / f"{stem} [300dpi 简].cbz"
    text_path = series_dir / f"{stem}.text.jsonl"
    if fix_path is None:
        # 规则-only 模式（传 "-"）：如果这一卷旁边存着 <卷名>.fix.json，就一并套用。
        # 否则会用 .text.jsonl 里的原始 OCR 文本重排「被规则改动到的页」——那会把
        # 这些页上的人工修正悄悄覆盖掉（实测踩过：2026-11-06 死神第10卷 p146）。
        sibling = series_dir / f"{stem}.fix.json"
        fix_path = sibling if sibling.exists() else None
    fix = json.loads(fix_path.read_text(encoding="utf-8")) if fix_path else {}
    if not src_path.exists() or not text_path.exists():
        print(f"缺少输入：{src_path} / {text_path}")
        return 1

    # 原始输入（用来重排受影响页）：与批量脚本同一套目录规则
    series_name = series_dir.name
    raw_cbz = Path(r"C:\Users\yoyos\Downloads\电子书\漫画") / series_name / f"{stem}.cbz"
    if not raw_cbz.exists():
        print(f"找不到原始卷：{raw_cbz}")
        return 1

    texts = {}
    for line in text_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        texts[row["page"]] = row["regions"]

    inpainter = Inpainter("cuda")
    cfg = Config(translator="mock", source_lang="zh", target_lang="简体中文",
                 font_path=FONT, min_font_size=20, max_font_size=96,
                 direction="auto", device="cuda")

    with zipfile.ZipFile(raw_cbz) as zraw, zipfile.ZipFile(src_path) as zdone:
        names = zdone.namelist()
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=".cbz", dir=str(series_dir))
        tmp_path = Path(tmp_name)
        import os

        os.close(tmp_fd)
        changed_pages = 0
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                page_fix = fix.get(name) or fix.get(Path(name).name) or {}
                regions_meta = texts.get(name) or texts.get(Path(name).name) or []
                if not regions_meta and not page_fix:
                    zout.writestr(name, zdone.read(name))
                    continue
                new_texts: list[str] = []
                touched = False
                for i, meta in enumerate(regions_meta):
                    base = page_fix.get(str(i), meta["text"])
                    t = apply_confusions(zhconv.convert(str(base or ""), "zh-cn").strip())
                    new_texts.append(t)
                    if t != meta["text"] or str(i) in page_fix:
                        touched = True
                if not touched:
                    zout.writestr(name, zdone.read(name))
                    continue
                with zraw.open(name) as f:
                    page = Image.open(f).convert("RGB")
                orig_page = page  # 判定横竖排必须用带字的原图；下面 page 会被抹字覆盖
                w0, h0 = page.size
                scale = TARGET_LONG_EDGE / max(w0, h0)
                kept, region_objs = [], []
                for meta, text in zip(regions_meta, new_texts):
                    if not text:
                        continue
                    r = Region(bbox=list(meta["bbox"]), direction="vertical",
                               translated_text=text)
                    kept.append(r)
                    region_objs.append((r, meta["bbox"]))
                if kept:
                    page = inpainter.inpaint_page(page, kept)
                big = page.resize((round(w0 * scale), round(h0 * scale)), Image.LANCZOS)
                scaled = [Region(bbox=[round(b[0] * scale), round(b[1] * scale),
                                       round(b[2] * scale), round(b[3] * scale)],
                                 direction=guess_direction(orig_page, b),
                                 translated_text=r.translated_text,
                                 color=list(r.color))
                          for r, b in region_objs]
                out = render_page(big, scaled, cfg)
                buf = tempfile.SpooledTemporaryFile()
                out.convert("RGB").save(buf, format="JPEG", quality=90, subsampling=0)
                buf.seek(0)
                zout.writestr(name, buf.read())
                changed_pages += 1
                print(f"  重排 {name}")
    # 目标文件必须**先关闭**再替换：Windows 上对正在被 zipfile 打开的文件做
    # os.replace 会直接 WinError 5（实测踩过），所以这段在 with 之外。
    for attempt in range(6):
        try:
            tmp_path.replace(src_path)
            break
        except PermissionError:
            time.sleep(1.5)
    else:
        alt = src_path.with_name(src_path.stem + ".retext.cbz")
        try:
            if alt.exists():
                alt.unlink()
            tmp_path.replace(alt)
            print(f"目标被占用，结果写在 {alt}（请手动替换）")
        except OSError as e:
            print(f"替换失败，保留临时文件 {tmp_path}：{e}")
            return 1
    print(f"完成：{changed_pages} 页已回填 -> {src_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
