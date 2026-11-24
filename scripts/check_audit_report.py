#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""拿用户/人工写好的审查报告，回来核对「报告里点名的每一条，成品里到底改了没有」。

为什么需要这个脚本：`audit_diff.py` 比的是「视觉转录 vs 现有文字」。
当**印在纸上的原文本身就是台湾用语/错别字**（例如「好康」是台湾口语、
用户要求写成「好」），两边会一致，diff 判为「无差异」，这条就漏过去了。
报告是这类问题的权威来源，所以必须有一个反向核对：报告点名的错串，
在最终文本里**不许再出现**。

用法：
  python scripts/check_audit_report.py "<审查报告.md>" --vol 17 \
      --text "C:\\...\\七龍珠 全彩版 第17卷.text.jsonl" \
      --fix  "C:\\...\\七龍珠 全彩版 第17卷.fix.json"

  --vol 17        只看报告里「第17卷」那一节（报告按卷分节）
  --text/--fix    最终文本 = text.jsonl 叠加 fix.json

输出：每条未落实的修正打印成 `页 | 错串 | 应有 | 现有`，退出码 1。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BULLET = re.compile(r"^\s*-\s*\*\*(\d{4}[^:*]*)\*\*\s*[:：]", re.M)
PAIR = re.compile(r"「([^」]{1,40})」\s*[→→\-]{1,2}\s*「([^」]{1,40})」")


def split_volume_sections(md: str) -> dict[str, str]:
    """按「## 第N卷」切节；同一卷可能有多节（逐页记录 / 统计），合并起来。"""
    parts = re.split(r"^##\s*第(\d+)卷", md, flags=re.M)
    out: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        vol = parts[i]
        out[vol] = out.get(vol, "") + "\n" + parts[i + 1]
    return out


def parse_items(section: str) -> list[tuple[str, str, str]]:
    """返回 [(页, 错串, 应有), ...]，页可能是 `0001–0003` 这种范围。"""
    items = []
    for line in section.splitlines():
        m = BULLET.match(line)
        if not m:
            continue
        page = m.group(1).split("–")[0].split("-")[0].strip()
        for wrong, right in PAIR.findall(line):
            if wrong == right:
                continue
            items.append((page, wrong, right))
    return items


def load_final(text_path: Path, fix_path: Path | None) -> dict[str, list[str]]:
    fix = json.loads(fix_path.read_text(encoding="utf-8")) if fix_path and fix_path.exists() else {}
    out: dict[str, list[str]] = {}
    for line in text_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        page = row["page"]
        page_fix = fix.get(page) or {}
        texts = []
        for i, meta in enumerate(row["regions"]):
            v = page_fix.get(str(i), meta.get("text") or "")
            texts.append(str(v))
        out[page] = texts
    return out


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="check_audit_report.py")
    ap.add_argument("report")
    ap.add_argument("--vol", required=True)
    ap.add_argument("--text", required=True)
    ap.add_argument("--fix", default=None)
    args = ap.parse_args(argv[1:])

    md = Path(args.report).read_text(encoding="utf-8")
    sections = split_volume_sections(md)
    section = sections.get(args.vol)
    if section is None:
        print(f"报告里没有第 {args.vol} 卷分节（有：{sorted(sections)}）")
        return 2

    items = parse_items(section)
    final = load_final(Path(args.text), Path(args.fix) if args.fix else None)

    total = 0
    for page, wrong, right in items:
        texts = final.get(f"{page}.jpg") or final.get(page)
        if texts is None:
            print(f"{page} | {wrong} -> {right} | 该页不在 text.jsonl")
            total += 1
            continue
        for t in texts:
            if wrong and wrong in t:
                print(f"{page} | 应改「{wrong}」->「{right}」 | 现有：{t}")
                total += 1
    print(f"报告条目 {len(items)} 条，未落实 {total} 条")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
