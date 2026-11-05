#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把一卷的 .text.jsonl 转成便于逐页校对的紧凑文本。

输出每行一页：`<页> | 0:文本 | 1:文本 …`，下标就是 .fix.json 里的键。
用法：python scripts/make_review.py <系列目录> <卷名> [输出文件]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    series_dir = Path(argv[1])
    stem = argv[2]
    src = series_dir / f"{stem}.text.jsonl"
    if not src.exists():
        print(f"缺少 {src}")
        return 1
    rows = []
    for line in src.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        parts = [f"{i}:{r['text']}" for i, r in enumerate(row["regions"])]
        if parts:
            rows.append(f"{Path(row['page']).stem} | " + " | ".join(parts))
    out = Path(argv[3]) if len(argv) > 3 else src.with_suffix(".review.txt")
    out.write_text("\n".join(rows), encoding="utf-8")
    print(f"{len(rows)} 页 -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
