"""安全更新 PROGRESS.md 的一行（两个 worker 并行时不会互相覆盖）。

用法：
  .\\.venv\\Scripts\\python.exe scripts\\mark_progress.py "<卷名>" "<新状态>" ["<页数标记>"]

做法：文件锁（PROGRESS.md.lock，O_EXCL，最多等 30 秒）+ 只改目标那行 + 原子写。
两个 worker 同时收尾时，后到的会等前一个写完再读，不会把对方的行盖掉。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BASE = Path(os.environ.get("COMICGEN_DATA_ROOT", "."))
PROGRESS = BASE / "PROGRESS.md"
LOCK = BASE / "PROGRESS.md.lock"


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    vol = argv[1]
    status = argv[2]
    pages = argv[3] if len(argv) > 3 else ""

    deadline = time.time() + 30
    while True:
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() > deadline:
                print("!! 等锁超时（另一个 worker 正在写 PROGRESS），放弃")
                return 1
            time.sleep(1)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)

        lines = PROGRESS.read_text(encoding="utf-8").splitlines()
        hit = -1
        for i, line in enumerate(lines):
            if line.startswith(f"| {vol} |"):
                hit = i
                break
        if hit < 0:
            print(f"!! PROGRESS 里没有 `| {vol} |` 这一行")
            return 1

        cells = [c.strip() for c in lines[hit].strip().strip("|").split("|")]
        cells[0] = vol
        cells[1] = status
        if pages:
            if len(cells) < 3:
                cells.append(pages)
            else:
                cells[2] = pages
        lines[hit] = "| " + " | ".join(cells) + " |"

        atomic_write(PROGRESS, "\n".join(lines) + "\n")
        print(f"PROGRESS 已更新：{vol}")
        return 0
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
