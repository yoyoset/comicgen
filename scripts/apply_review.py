"""把逐页复核的修正合并进 trans + fix（不重排，重排另跑 retext）。

用法：
  .\\.venv\\Scripts\\python.exe scripts\\apply_review.py "<系列目录>" "<卷名>" "<review.json>" [--dry]

review.json 结构：{"0012.jpg": {"3": "改后的文字"}, ...}（只放有错的格）
做的事：
  1. 把改后文字写回 trans 目录里**包含该页**的那个批次 JSON（找不到就报错，不新建）；
  2. 把同一批改动写进 <卷名>.fix.json（retext 读的是它）；
  3. 打印统计；--dry 只报告不落盘。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path


def find_batch(trans_dirs: list[Path], page: str) -> Path | None:
    for d in trans_dirs:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if page in data:
                return f
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("series_dir")
    ap.add_argument("stem")
    ap.add_argument("review")
    ap.add_argument("--trans-dir", action="append", default=[], help="可多次给；默认猜 TEMP 下的 transNN / transNN_main")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args(argv)

    series = Path(args.series_dir)
    review = json.loads(Path(args.review).read_text(encoding="utf-8"))
    trans_dirs = [Path(p) for p in args.trans_dir]
    if not trans_dirs:
        num = args.stem.rsplit("第", 1)[-1].replace("卷", "")
        base = Path(tempfile.gettempdir()) / "opencode"
        trans_dirs = [base / f"trans{num}", base / f"trans{num}_main"]

    fix_path = series / f"{args.stem}.fix.json"
    fix = json.loads(fix_path.read_text(encoding="utf-8")) if fix_path.exists() else {}

    touched_batches: dict[Path, dict] = {}
    applied = fix_only = 0
    for page, cells in review.items():
        if page.startswith("_"):
            continue
        batch = find_batch(trans_dirs, page)
        if batch is None:
            # 转录目录里没有这一页（页被并到别的批次或批次已清理）：
            # 仍然写进 fix.json —— retext 读的是 fix，不回填就白改。
            fix.setdefault(page, {}).update({str(k): v for k, v in cells.items()})
            fix_only += len(cells)
            print(f"!! {page} 不在任何批次里，只写 fix.json（{len(cells)} 格）")
            continue
        data = touched_batches.setdefault(batch, json.loads(batch.read_text(encoding="utf-8")))
        page_cells = data.setdefault(page, {})
        for idx, text in cells.items():
            page_cells[str(idx)] = text
            fix.setdefault(page, {})[str(idx)] = text
            applied += 1

    print(f"待应用 {sum(len(v) for k, v in review.items() if not k.startswith('_'))} 格 / "
          f"{sum(1 for k in review if not k.startswith('_'))} 页：写入转录 {applied}，仅 fix {fix_only}")
    for batch, data in touched_batches.items():
        print(f"  {batch.name} 更新 {sum(len(v) for v in data.values())} 格")

    if args.dry:
        print("--dry：未落盘")
        return 0
    for batch, data in touched_batches.items():
        batch.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    fix_path.write_text(json.dumps(fix, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写入 {len(touched_batches)} 个批次与 {fix_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
