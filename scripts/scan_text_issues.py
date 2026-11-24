#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""成品文本体检：找出交付页面里"%看起来不对"的格子，输出 CSV 供人工过目。

只读，不改任何文件。检查项（每项都留了余量，宁可漏报也不误报）：
  1. `ocr噪声`      日期/条款成串、整格无汉字、成段假名 —— OCR 幻觉，重排时应保留原图
  2. `假名残留`     中文句子里夹着 1–3 个日文假名
  3. `可能超框`     按最小字号 10pt 估算仍放不下（会在气泡外或压线）
  4. `重复退化`     同一 1–2 字单元重复 ≥6 次，且不是拟声词常见形（哈哈哈/啊啊啊）

用法：
  .\\.venv\\Scripts\\python.exe scripts\\scan_text_issues.py                 # 默认读环境变量 COMICGEN_DATA_ROOT
  .\\.venv\\Scripts\\python.exe scripts\\scan_text_issues.py --root <目录> --out <csv目录>

2026-11-17 实测（117 卷 12 万格）：ocr噪声 314、假名残留 868、可能超框 201。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comic_gen.pipeline.proofread import looks_like_ocr_noise  # noqa: E402

KANA = re.compile(r"[\u3040-\u30ff]")
HAN = re.compile(r"[\u4e00-\u9fff]")
MIN_SIZE = 10
COL_GAP = 1.35
V_ADV = 1.05


def over_capacity(text: str, bbox) -> bool:
    x, y, w, h = bbox
    if w <= 0 or h <= 0:
        return False
    # 竖排：每列字数 × 列数；横排：每行字数 × 行数。取两种里更宽松的，只报明显放不下的。
    vertical = max(1, int(h // (MIN_SIZE * V_ADV))) * max(1, int((w - 6) // (MIN_SIZE * COL_GAP)))
    horizontal = max(1, int((w - 6) // MIN_SIZE)) * max(1, int((h - 6) // (MIN_SIZE * 1.2)))
    return len(text) > max(vertical, horizontal)


def repeated_degeneration(text: str) -> bool:
    t = text.strip()
    if len(t) < 8:
        return False
    for unit_len in (1, 2):
        for start in range(0, min(unit_len * 2, max(1, len(t) - unit_len * 6))):
            unit = t[start:start + unit_len]
            if not unit:
                continue
            if unit in "哈呵啊喔呜呀嘿哒喳嘻哦噢喂…—":
                continue
            if t.count(unit) >= 6:
                return True
    return False


def main(argv: list[str] | None = None) -> int:
    default_root = Path(os.environ.get("COMICGEN_DATA_ROOT", "."))
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(default_root))
    ap.add_argument("--out", default=str(default_root / "_文档" / "_remaining_issues"))
    args = ap.parse_args(argv)

    root = Path(args.root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    buckets: dict[str, list[list[str]]] = {k: [] for k in ("ocr噪声", "假名残留", "可能超框", "重复退化")}

    for series_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        for f in sorted(series_dir.glob("*.text.jsonl")):
            stem = f.name[: -len(".text.jsonl")]
            fixp = series_dir / f"{stem}.fix.json"
            fix = json.loads(fixp.read_text(encoding="utf-8")) if fixp.exists() else {}
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                page = row.get("page")
                page_fix = fix.get(page, {})
                for i, r in enumerate(row.get("regions") or []):
                    t = str(page_fix.get(str(i), r.get("text")) or "").strip()
                    if not t:
                        continue
                    row_out = [series_dir.name, stem, str(page), str(i), t[:60]]
                    if looks_like_ocr_noise(t):
                        buckets["ocr噪声"].append(row_out)
                        continue
                    han, kana = len(HAN.findall(t)), len(KANA.findall(t))
                    if han >= 3 and 0 < kana <= 3:
                        buckets["假名残留"].append(row_out)
                    if over_capacity(t, r.get("bbox") or [0, 0, 1, 1]):
                        buckets["可能超框"].append(row_out)
                    if repeated_degeneration(t):
                        buckets["重复退化"].append(row_out)

    for name, rows in buckets.items():
        p = out / f"{name}.csv"
        with p.open("w", encoding="utf-8-sig", newline="") as fh:
            wr = csv.writer(fh)
            wr.writerow(["系列", "卷", "页", "区域", "文本"])
            wr.writerows(rows)
        print(f"{name}: {len(rows)} 格 -> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
