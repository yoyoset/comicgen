#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校对回填：把人工校对过的文本重新排到已完成的成品卷里（只重做受影响页）。

批量脚本产出：
  <系列>/<卷名> [300dpi 简].cbz     成品
  <系列>/<卷名>.text.jsonl          每页每个框的最终文本（校对依据）

校对产物：
  <系列>/<卷名>.fix.json            {"<页面文件名>": {"0": "修正后的文本", ...}, ...}
  索引对应 .text.jsonl 里该页 regions 的顺序；空字符串 = 该框不排字（保留原图）。

用法：
  python scripts/retext_volume.py "<页面 jsonl 所在目录>" "<卷名>" "<.fix.json>"
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from PIL import Image

try:
    import zhconv
except ImportError:  # pragma: no cover - 可选依赖，仅中文繁转简脚本使用
    zhconv = None

TARGET_LONG_EDGE = 3030
FONT = r"C:\Windows\Fonts\msyh.ttc"

# 页眉页脚/版式字：**不重排**（用户明确要求，且实测重排后会把整排红色小字
# 「弗力札篇—2★36」变成黑字、数字还被拆开）。这些是书籍版式，不是台词。
# 判据取得很窄：短、且明显是版式格式，避免误伤正文。
_MASTHEAD_PATTERNS = (
    r"★\s*[0-9０-９]+\s*$",
    r"DRAGON\s*BALL",
    r"ドラゴンボール",
    r"^(弗力札篇|人造人間篇|サイヤ人篇|魔人ブウ篇)",
    r"^死神\s*[0-9０-９]+",
    r"^BLEACH\b",
)


def _is_masthead(text: str) -> bool:
    import re

    t = (text or "").strip()
    if not t or len(t) > 40:
        return False
    return any(re.search(p, t, re.IGNORECASE) for p in _MASTHEAD_PATTERNS)


def _load_extra(series_dir: Path, stem: str) -> dict[str, list[dict]]:
    """漏检气泡的手工补框：<卷名>.extra.json
    {"0044.jpg": [{"bbox": [x, y, w, h], "text": "我……"}, ...]}
    坐标与 .text.jsonl 里的 bbox 同一坐标系（原始页像素）。"""
    path = series_dir / f"{stem}.extra.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def guess_direction(page: Image.Image, bbox) -> str:
    import numpy as np

    x, y, w, h = bbox
    # 形状先验：宽而扁的框不可能是竖排（竖排要堆字，高度不够就只能一字一列，
    # 反而把整行字按列从右往左倒着排，字序全反）。实测 0041#1 [253×34] 一行
    # 12 字被像素间隔启发式判成竖排，成品读作「师却灭的强最为成你让会我」。
    if w >= 2 * h:
        return "horizontal"
    crop = page.crop((x, y, x + w, y + h)).convert("L")
    if min(crop.size) < 8:
        return "horizontal"
    ink = np.array(crop) < 128
    col_gap = float((ink.sum(axis=0) == 0).mean())
    row_gap = float((ink.sum(axis=1) == 0).mean())
    return "vertical" if col_gap - row_gap > 0.05 else "horizontal"


def main(argv: list[str]) -> int:
    series_dir = Path(argv[1])
    stem = argv[2]
    fix_path = Path(argv[3]) if len(argv) > 3 and argv[3] != "-" else None
    raw_root_arg = ""
    rest = argv[4:]
    for i, a in enumerate(rest):
        if a == "--raw-root" and i + 1 < len(rest):
            raw_root_arg = rest[i + 1]

    from comic_gen.core.config import Config
    from comic_gen.core.models import Region
    from comic_gen.pipeline.inpaint import Inpainter
    from comic_gen.pipeline.proofread import apply_confusions, render_text
    from comic_gen.pipeline.render import (
        _VERTICAL_ROTATE,
        DARK_BACKGROUND_LUM,
        background_luminance,
        render_page,
    )

    src_path = series_dir / f"{stem} [300dpi 简].cbz"
    text_path = series_dir / f"{stem}.text.jsonl"
    if fix_path is None:
        # 规则-only 模式（传 "-"）：如果这一卷旁边存着 <卷名>.fix.json，就一并套用。
        # 否则会用 .text.jsonl 里的原始 OCR 文本重排「被规则改动到的页」——那会把
        # 这些页上的人工修正悄悄覆盖掉（实测踩过：2026-11-06 死神第10卷 p146）。
        sibling = series_dir / f"{stem}.fix.json"
        fix_path = sibling if sibling.exists() else None
    fix = json.loads(fix_path.read_text(encoding="utf-8")) if fix_path else {}
    extra = _load_extra(series_dir, stem)
    if not src_path.exists() or not text_path.exists():
        print(f"缺少输入：{src_path} / {text_path}")
        return 1

    # 原始输入（用来重排受影响页）：与批量脚本同一套目录规则。
    # --raw-root 既可以是「卷 cbz 直接所在的目录」，也可以是「系列根目录」（下面再拼系列名）。
    series_name = series_dir.name
    raw_root = Path(raw_root_arg) if raw_root_arg else Path(os.environ.get("COMICGEN_RAW_ROOT", "."))
    if (raw_root / f"{stem}.cbz").exists():
        raw_cbz = raw_root / f"{stem}.cbz"
    else:
        raw_cbz = raw_root / series_name / f"{stem}.cbz"
    if not raw_cbz.exists():
        print(f"找不到原始卷：{raw_cbz}（原始卷已挪走时用 --raw-root 指定目录）")
        return 1

    texts = {}
    for line in text_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        texts[row["page"]] = row["regions"]

    inpainter = Inpainter("cuda")
    cfg = Config(translator="mock", source_lang="zh", target_lang="简体中文",
                 font_path=FONT, min_font_size=20, max_font_size=96,
                 direction="auto", device="cuda")

    with zipfile.ZipFile(raw_cbz) as zraw, zipfile.ZipFile(src_path) as zdone:
        names = zdone.namelist()
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=".cbz", dir=str(series_dir))
        tmp_path = Path(tmp_name)
        os.close(tmp_fd)
        changed_pages = 0
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                page_fix = fix.get(name) or fix.get(Path(name).name) or {}
                regions_meta = texts.get(name) or texts.get(Path(name).name) or []
                extras = extra.get(name) or extra.get(Path(name).name) or []
                if not regions_meta and not page_fix and not extras:
                    zout.writestr(name, zdone.read(name))
                    continue
                new_texts: list[str] = []
                touched = False
                for i, meta in enumerate(regions_meta):
                    base = page_fix.get(str(i), meta["text"])
                    t = render_text(base)
                    # 旧版重排出来的实际文字（apply_confusions 之后）：两者不同 =
                    # 这一页当时被那张单字表改坏了，必须重排一次才能修回来。
                    raw_legacy = str(base or "")
                    if zhconv is not None:
                        raw_legacy = zhconv.convert(raw_legacy, "zh-cn")
                    legacy = apply_confusions(raw_legacy.strip())
                    if _is_masthead(t) or _is_masthead(str(meta["text"] or "")):
                        # 页眉页脚/版式字：保留原图，既不判"改动"也不排字
                        new_texts.append("")
                        continue
                    new_texts.append(t)
                    # 破折号类字符改成了竖排旋转绘制（2026-11-16）：含这类字的页
                    # 必须重排一次，否则成品里还是旧的「一一」画法。
                    if (t != meta["text"] or legacy != t or str(i) in page_fix
                            or any(c in _VERTICAL_ROTATE for c in t)):
                        touched = True
                if extras:
                    # 漏检气泡页可能整页 0 个检测区域（实测 28 卷 0028：8 个气泡全漏检），
                    # 这时没有 regions 可改、touched 永远是 False，页面会被原样写回，
                    # 补框等于没做。有补框就必须重排这一页。
                    touched = True
                if not touched:
                    # 深色底上的字现在改用纯白（2026-11-17「黑底黑字看不清」）：
                    # 这类页面必须重排一次才会变白，否则成品保持不可读的旧画法。
                    with zraw.open(name) as fh:
                        probe = Image.open(fh).convert("L")
                    for meta, txt in zip(regions_meta, new_texts):
                        if txt and background_luminance(probe, meta["bbox"]) < DARK_BACKGROUND_LUM:
                            touched = True
                            break
                if not touched:
                    zout.writestr(name, zdone.read(name))
                    continue
                with zraw.open(name) as f:
                    page = Image.open(f).convert("RGB")
                orig_page = page  # 判定横竖排必须用带字的原图；下面 page 会被抹字覆盖
                w0, h0 = page.size
                scale = TARGET_LONG_EDGE / max(w0, h0)
                kept, region_objs = [], []
                for meta, text in zip(regions_meta, new_texts):
                    if not text:
                        continue
                    r = Region(bbox=list(meta["bbox"]), direction="vertical",
                               translated_text=text)
                    kept.append(r)
                    region_objs.append((r, meta["bbox"]))
                # 漏检气泡的手工补框：同样的抹字/排字流程，只是框不在 text.jsonl 里
                for item in extras:
                    text = render_text(item.get("text"))
                    if not text:
                        continue
                    bbox = list(item["bbox"])
                    r = Region(bbox=bbox, direction="vertical", translated_text=text)
                    kept.append(r)
                    region_objs.append((r, bbox))
                if kept:
                    page = inpainter.inpaint_page(page, kept)
                big = page.resize((round(w0 * scale), round(h0 * scale)), Image.LANCZOS)
                scaled = [Region(bbox=[round(b[0] * scale), round(b[1] * scale),
                                       round(b[2] * scale), round(b[3] * scale)],
                                 direction=guess_direction(orig_page, b),
                                 translated_text=r.translated_text,
                                 color=list(r.color))
                          for r, b in region_objs]
                out = render_page(big, scaled, cfg)
                buf = tempfile.SpooledTemporaryFile()
                out.convert("RGB").save(buf, format="JPEG", quality=90, subsampling=0)
                buf.seek(0)
                zout.writestr(name, buf.read())
                changed_pages += 1
                print(f"  重排 {name}")
    # 目标文件必须**先关闭**再替换：Windows 上对正在被 zipfile 打开的文件做
    # os.replace 会直接 WinError 5（实测踩过），所以这段在 with 之外。
    for attempt in range(6):
        try:
            tmp_path.replace(src_path)
            break
        except PermissionError:
            time.sleep(1.5)
    else:
        alt = src_path.with_name(src_path.stem + ".retext.cbz")
        try:
            if alt.exists():
                alt.unlink()
            tmp_path.replace(alt)
            print(f"目标被占用，结果写在 {alt}（请手动替换）")
        except OSError as e:
            print(f"替换失败，保留临时文件 {tmp_path}：{e}")
            return 1
    print(f"完成：{changed_pages} 页已回填 -> {src_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
