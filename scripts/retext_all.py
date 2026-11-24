#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""整批重排：对一系列卷跑 retext_volume.py（只重排被改到的页）。

为什么需要：校对/规则修正只改 `<卷名>.fix.json`，成品 cbz 要重排才生效；
单卷手工跑一次 3–7 分钟，整库 113 卷就得批处理。2026-11-17 用它跑完了
死神 74 + 灌篮 24 + 七龍珠 15 卷（全部 rc=0）。

用法：
  .\\.venv\\Scripts\\python.exe scripts\\retext_all.py 死神 灌篮 七龍珠 --workers 3
  .\\.venv\\Scripts\\python.exe scripts\\retext_all.py --dry        # 只列清单

约定：
- 成品在 `<成品根>\\<系列>\\`（默认环境变量 `COMICGEN_DATA_ROOT`），原始卷在 `COMICGEN_RAW_ROOT`；
- 成功记录写进 `%TEMP%\\opencode\\clean\\rerender.jsonl`，重跑时**自动跳过已成功的卷**
  （关机/中断后续跑就靠它，不会把做完的卷重做一遍）；
- 每卷耗时记在日志里，失败会把末 600 字错误贴出来。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PY = sys.executable
SCRIPTS = Path(__file__).resolve().parent
PRODUCT_ROOT = Path(os.environ.get("COMICGEN_DATA_ROOT", "."))
RAW_ROOT = Path(os.environ.get("COMICGEN_RAW_ROOT", "."))
TEMP = Path(tempfile.gettempdir()) / "opencode" / "clean"
LOG = TEMP / "rerender.log"
SUMMARY = TEMP / "rerender.jsonl"

SERIES = {
    "死神": ("死神", "死神"),
    "灌篮": ("灌篮高手", "灌篮高手"),
    "七龍珠": ("七龍珠 全彩版", "七龍珠 全彩版"),
    "海贼": ("海贼王", "海贼王"),
}


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def done_stems() -> set[str]:
    done: set[str] = set()
    if SUMMARY.exists():
        for line in SUMMARY.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("rc") == 0:
                done.add(rec["stem"])
    return done


def jobs(tags: list[str]) -> list[tuple[str, Path, Path]]:
    out = []
    skip = done_stems()
    for tag in tags:
        prod_name, raw_name = SERIES[tag]
        d = PRODUCT_ROOT / prod_name
        raw_root = RAW_ROOT / raw_name
        for fix in sorted(d.glob("*.fix.json")):
            stem = fix.name[: -len(".fix.json")]
            if stem in skip or not (d / f"{stem} [300dpi 简].cbz").exists():
                continue
            cells = sum(len(v) for k, v in json.loads(fix.read_text(encoding="utf-8")).items()
                        if not k.startswith("_"))
            if cells:
                out.append((stem, d, raw_root))
    return out


def run_one(item) -> dict:
    stem, d, raw_root = item
    t0 = time.time()
    p = subprocess.run(
        [PY, str(SCRIPTS / "retext_volume.py"), str(d), stem, str(d / f"{stem}.fix.json"),
         "--raw-root", str(raw_root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
    out = (p.stdout or "") + (p.stderr or "")
    done = next((l.strip() for l in out.splitlines() if l.startswith("完成：")), "")
    if not done:
        done = next((l.strip() for l in reversed(out.splitlines()) if l.strip()), "(无输出)")
    rec = {"stem": stem, "rc": p.returncode, "result": done, "sec": round(time.time() - t0)}
    log(f"{stem}: rc={p.returncode} {done} ({rec['sec']}s)")
    if p.returncode != 0:
        log(f"   !!! 失败（末 600 字）: {out[-600:]}")
    with SUMMARY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def main(argv: list[str]) -> int:
    global SUMMARY, LOG
    tags = [a for a in argv if a in SERIES]
    workers = 2
    if "--workers" in argv:
        workers = int(argv[argv.index("--workers") + 1])
    if "--summary" in argv:
        SUMMARY = Path(argv[argv.index("--summary") + 1])
        LOG = SUMMARY.with_suffix(".log")
    todo = jobs(tags or list(SERIES))
    log(f"总计 {len(todo)} 卷，并发 {workers}")
    if "--dry" in argv:
        for s, _d, _r in todo:
            print(" ", s)
        return 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(run_one, todo))
    log("全部结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
