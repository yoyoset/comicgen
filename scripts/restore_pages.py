#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把成品里的若干页还原成原图（不重排、不抹字）。

场景：封面、目录、扉页、版权页、广告、下集预告这些**非主体页**，
按交付标准不应该动。但历史流程里它们也进了 fix.json 被重排过
（实测：七龙珠 17 卷把封面上的「鸟山明」重写成了整行标题）。

做法：打开原始卷取那一页，按成品页的尺寸缩放（保持整卷分辨率一致），
写回成品 cbz，其它页不动。

用法：
  python scripts/restore_pages.py "<系列目录>" "<卷名>" 0001 0002 0003 0004 0247
  --raw-root 原始卷根目录（默认读环境变量 COMICGEN_RAW_ROOT，未设则为当前目录）
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

from PIL import Image

DEFAULT_RAW_ROOT = os.environ.get("COMICGEN_RAW_ROOT", ".")


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="restore_pages.py")
    ap.add_argument("series_dir")
    ap.add_argument("stem")
    ap.add_argument("pages", nargs="+", help="页码，如 0001；可带或不带 .jpg")
    ap.add_argument("--raw-root", default=DEFAULT_RAW_ROOT)
    ap.add_argument("--quality", type=int, default=90)
    args = ap.parse_args(argv[1:])

    series_dir = Path(args.series_dir)
    raw = Path(args.raw_root) / series_dir.name / f"{args.stem}.cbz"
    out = series_dir / f"{args.stem} [300dpi 简].cbz"
    if not raw.exists() or not out.exists():
        print(f"缺少输入：{raw} / {out}")
        return 1

    names = {p if p.endswith(".jpg") else f"{p}.jpg" for p in args.pages}
    replaced = []

    # mkstemp 返回的是**打开的文件描述符**；Windows 上不 close 就删/写这个文件会
    # WinError 32（实测踩过）。拿到路径后立刻 close，zipfile 自己会覆盖写。
    fd, tmp_name = tempfile.mkstemp(suffix=".cbz", dir=str(series_dir))
    os.close(fd)

    with zipfile.ZipFile(raw) as zraw, zipfile.ZipFile(out) as zout, \
            zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zw:
        for item in zout.infolist():
            data = zout.read(item.filename)
            if item.filename in names and item.filename in zraw.namelist():
                with zraw.open(item.filename) as f:
                    img = Image.open(f).convert("RGB")
                with Image.open(zout.open(item.filename)) as cur:
                    size = cur.size          # 保持成品页尺寸一致
                img = img.resize(size, Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=args.quality, subsampling=0)
                data = buf.getvalue()
                replaced.append(item.filename)
            zw.writestr(item, data)

    if not replaced:
        Path(tmp_name).unlink(missing_ok=True)
        print("没有可还原的页（检查页码/原始卷文件名）")
        return 1

    for attempt in range(6):
        try:
            Path(tmp_name).replace(out)
            break
        except PermissionError:
            if attempt == 5:
                raise
    print(f"还原 {len(replaced)} 页：{', '.join(replaced)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
