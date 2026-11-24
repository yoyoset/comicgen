"""核对覆盖率体检：确认「这一卷每一页有字的页都真的进过逐页核对」。

用法：
  .\\.venv\\Scripts\\python.exe scripts\\check_coverage.py "<系列>" "<卷名>" \
      --coverage "<系列>\\<卷名>.coverage.json" \
      --sheets "%TEMP%\\opencode\\sheets20"

coverage json 结构（worker 汇总每批回来的 `_checked` 得到）：
  {"checked": ["0005", "0006", ...], "batches": [{"range": ["0005", "0019"], "pages": 15}]}

判定：`text.jsonl` 里区域数 > 0 的页 = 必须核对；sheets 目录里没有的页视为无字页，可缺。
缺任何一页 → 退出码 1（worker 不许 `--finish`）。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="逐页核对覆盖率体检")
    ap.add_argument("series_dir")
    ap.add_argument("stem")
    ap.add_argument("--coverage", default="", help="默认 <系列>/<卷名>.coverage.json")
    ap.add_argument("--sheets", default="", help="默认按卷号猜 TEMP 下的 sheets 目录")
    ap.add_argument("--skip", nargs="*", default=[], help="非主体页（封面/目录/附录/版权），不参与覆盖率")
    args = ap.parse_args(argv)

    series = Path(args.series_dir)
    stem = args.stem
    cov_path = Path(args.coverage) if args.coverage else series / f"{stem}.coverage.json"
    if not args.sheets:
        num = stem.rsplit("第", 1)[-1].replace("卷", "")
        args.sheets = str(Path(tempfile.gettempdir()) / "opencode" / f"sheets{num}")

    text_path = series / f"{stem}.text.jsonl"
    if not text_path.exists():
        print(f"!! 找不到 {text_path.name}")
        return 2
    with_regions = set()
    for line in text_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("regions"):
            with_regions.add(Path(row["page"]).stem)

    sheets_dir = Path(args.sheets)
    sheets = {p.name.split("_")[0] for p in sheets_dir.glob("*_sheet.png")} if sheets_dir.is_dir() else set()

    covered = set()
    batches = []
    if cov_path.exists():
        data = json.loads(cov_path.read_text(encoding="utf-8"))
        covered = {str(x).split(".")[0] for x in data.get("checked", [])}
        batches = data.get("batches", [])

    target = with_regions & sheets if sheets else with_regions
    skip = {str(s).split(".")[0] for s in args.skip}
    target -= skip
    missing = sorted(target - covered)
    extra = sorted(covered - with_regions)

    print(f"卷：{stem}")
    print(f"有字页 {len(with_regions)}，联系表 {len(sheets)}，应核对 {len(target)}，已核对 {len(covered & target)}")
    if batches:
        print(f"批次记录 {len(batches)} 条")
    if extra:
        print(f"（核对记录里有 {len(extra)} 页不在有字页清单，可忽略：{extra[:5]}）")
    if missing:
        print(f"!! 缺核对 {len(missing)} 页：{missing}")
        return 1
    print("覆盖率 100%：每个有字页都进过逐页核对")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
