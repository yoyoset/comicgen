#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""导出某段页码的「最终文本」= `<卷名>.text.jsonl` 叠加 `<卷名>.fix.json`。

逐页复核时要拿它和编号联系表对照：左边是入库文字，右边是图上原字。
只看 text.jsonl 会看到旧 OCR 的原文，看不见 fix 之后的结果，容易误判。

用法：
  python scripts/dump_final.py "<系列目录>" "<卷名>" 0037 0046
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 5:
        print(__doc__)
        return 2
    series, stem, lo, hi = Path(argv[1]), argv[2], argv[3], argv[4]
    out_path = Path(argv[6]) if len(argv) > 6 and argv[5] == "--out" else None
    fix_path = series / f"{stem}.fix.json"
    fix = json.loads(fix_path.read_text(encoding="utf-8")) if fix_path.exists() else {}
    text_path = series / f"{stem}.text.jsonl"
    lines: list[str] = []
    for line in text_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        page = row["page"]
        if not (lo <= page[:4] <= hi):
            continue
        pf = fix.get(page) or {}
        parts = []
        for i, meta in enumerate(row["regions"]):
            t = pf.get(str(i), meta.get("text") or "")
            parts.append(f"{i}:{t}")
        lines.append(f"{page[:4]} | " + " | ".join(parts))
    text = "\n".join(lines)
    if out_path:
        out_path.write_text(text + "\n", encoding="utf-8")
        print(f"{len(lines)} 页 -> {out_path}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
