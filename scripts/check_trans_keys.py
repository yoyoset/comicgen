#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""转录键数体检：每页「转录键数」必须等于 text.jsonl 里该页的区域数。

为什么需要这一步（docs/AUDIT_WORKFLOW.md C-3）：转录是分批交给视觉模型的，
偶发「整对调」——A 页的答案贴到 B、B 贴到 A，两页区域数相近时格号不越界，
audit_diff 完全看不出来（实测七龙珠 18 卷 0028/0029 等四对）。唯一可靠的
体检办法就是数键：多一个键、少一个键、键号不连续都说明这一页的答案不对。

用法：
  .\\.venv\\Scripts\\python.exe scripts\\check_trans_keys.py `
    "<系列目录>" "<卷名>" "<转录目录>" [--skip-pages 0001 0002 ...]

退出码 1 = 有异常页（必须重转录这些页）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="check_trans_keys.py")
    ap.add_argument("series_dir")
    ap.add_argument("stem")
    ap.add_argument("trans_dir")
    ap.add_argument("--skip-pages", nargs="*", default=[],
                    help="非主体页（不参与比对，也就不要求转录键数）")
    args = ap.parse_args(argv[1:])

    skip = {p if p.endswith(".jpg") else f"{p}.jpg" for p in args.skip_pages}
    series_dir = Path(args.series_dir)
    stem = args.stem
    text_path = series_dir / f"{stem}.text.jsonl"
    if not text_path.exists():
        print(f"缺少 {text_path}")
        return 1

    texts: dict[str, int] = {}
    pages_order: list[str] = []
    for line in text_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        texts[row["page"]] = len(row["regions"])
        pages_order.append(row["page"])

    trans: dict[str, dict] = {}
    for path in sorted(Path(args.trans_dir).glob("*.json")):
        if path.name.startswith("_"):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for page, regions in data.items():
            if not isinstance(regions, dict):
                continue
            trans.setdefault(page, {}).update(
                {str(k).lstrip("#"): v for k, v in regions.items()})

    bad = []
    for page in pages_order:
        want = texts[page]
        if want == 0 or page in skip:
            continue
        if page not in trans:
            continue                      # 未转录页由 audit_diff 计为「缺转录」
        got = trans[page]
        keys = sorted(got, key=lambda k: int(k) if k.isdigit() else 10 ** 9)
        problems = []
        if len(got) != want:
            problems.append(f"键数 {len(got)} ≠ 区域数 {want}")
        if any(not k.isdigit() for k in got):
            problems.append("存在非数字键")
        if [k for k in keys if k.isdigit()] != [str(i) for i in range(len(keys))]:
            problems.append("键号不连续")
        if problems:
            bad.append(f"{page}: {'；'.join(problems)}（现有键 {keys}）")
        # 串页的另一种表现：答案内容疑似属于别的页（页面区域数差 1-2 时最难发现），
        # 只能靠逐页复核，这里只做篇幅层面的粗筛。
    total = sum(1 for p in pages_order if texts[p] and p not in skip)
    missing = [p for p in pages_order if texts[p] and p not in skip and p not in trans]

    print(f"应转录页 {total}，已转录 {total - len(missing)}，未转录 {len(missing)}")
    if missing:
        print(f"未转录页：{' '.join(p[:4] for p in missing)}")
    if bad:
        print("## 键数异常页（必须重转录）")
        for line in bad:
            print(f"- {line}")
        return 1
    print("键数体检通过：所有已转录页键数 = 区域数")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
