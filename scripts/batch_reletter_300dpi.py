#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全量批处理：繁体中文漫画 → 300 dpi · 简体 · 重排。

每卷一个产物，可断点续跑（成品已存在的卷跳过；卷内已处理的页也跳过）。
流水线（全部复用项目已有模块）：
  RT-DETRv2 检测文字框 → manga-ocr 逐框识别 → zhconv 转简体 →
  LaMa 抹掉原文 → 美术层放大到 300 dpi（B5 长边 3030px）→ 按目标分辨率重排文字。

用法：
  python scripts/batch_reletter_300dpi.py            # 全量
  python scripts/batch_reletter_300dpi.py --limit 2   # 只跑前 2 卷（调试/验收）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import traceback
import zipfile
from pathlib import Path

import zhconv
from PIL import Image

TARGET_LONG_EDGE = 3030
IMG_SUFFIX = (".jpg", ".jpeg", ".png", ".webp")
FONT = r"C:\Windows\Fonts\msyh.ttc"
SERIES_ROOT = Path(os.environ.get("COMICGEN_DATA_ROOT", "."))
OUT_ROOT = SERIES_ROOT / "_300dpi_简"
WORK_ROOT = OUT_ROOT / "_work"
LOG_PATH = OUT_ROOT / "batch.log"
JPEG_QUALITY = 90

_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def looks_like_misread(text: str) -> bool:
    stripped = re.sub(r"\s", "", text)
    if not stripped:
        return True
    return len(_KANA_RE.findall(stripped)) / len(stripped) > 0.3


def guess_direction(page: Image.Image, bbox) -> str:
    x, y, w, h = bbox
    crop = page.crop((x, y, x + w, y + h)).convert("L")
    if min(crop.size) < 8:
        return "horizontal"
    import numpy as np

    ink = np.array(crop) < 128
    col_gap = float((ink.sum(axis=0) == 0).mean())
    row_gap = float((ink.sum(axis=1) == 0).mean())
    return "vertical" if col_gap - row_gap > 0.05 else "horizontal"


def process_page(page, det, mocr, inpainter, cfg, llm=None):
    from comic_gen.core.models import Region
    from comic_gen.pipeline.proofread import apply_confusions, proofread_lines
    from comic_gen.pipeline.render import render_page

    w0, h0 = page.size
    scale = TARGET_LONG_EDGE / max(w0, h0)
    regions = det.detect(page)

    kept, drafts = [], []
    kana_dropped = 0
    for r in regions:
        x, y, bw, bh = r.bbox
        pad = 6
        crop = page.crop((max(0, x - pad), max(0, y - pad),
                          min(w0, x + bw + pad), min(h0, y + bh + pad)))
        raw = mocr(crop)
        if looks_like_misread(raw):
            kana_dropped += 1
            continue
        simp = apply_confusions(zhconv.convert(raw, "zh-cn").strip())
        # 没有一点汉字 = 英文页脚/球衣字/logo 被当文字认了。这类内容不能重排：
        # manga-ocr 对拉丁字母基本是瞎猜（实测把 DRAGON BALL FULL COLOR 排成
        # "ORAGⓃBALTU|CLCR"），保留原图像素反而正确。
        if not simp or not _CJK_RE.search(simp):
            continue
        # 只重排"对话"：检测器把气泡里的台词标成 text_bubble，画面上的拟声词/标注标成
        # text_free。拟声词是美术字（换字体就毁了），旁白/N 字框大多也走 text_free，
        # 所以 text_free 只收"成句"的（≥4 字），短促的拟声词一律留原图。
        if r.kind != "text_bubble" and len(simp) < 4:
            continue
        kept.append(r)
        drafts.append(simp)

    # 整页兜底：注释页/资料页（几十处小字、大量日文假名）重排只会更糟——
    # 实测七龍珠第17卷的"太空船图鉴"页，30 个区域里大半是铏释小字，重排后整页变乱码。
    # 这类页保持原图，只做分辨率放大。
    if len(regions) > 25 or (regions and kana_dropped / len(regions) > 0.5):
        keep_big = page.resize((round(w0 * scale), round(h0 * scale)), Image.LANCZOS)
        return keep_big, 0, 0, 0, []

    changed = 0
    if kept and llm is not None:
        drafts, changed = proofread_lines(drafts, llm)
        # 校对模型偶尔会把简体改回繁体（实测），再过一次转换保证输出是简体。
        drafts = [zhconv.convert(t, "zh-cn") for t in drafts]

    clean = inpainter.inpaint_page(page, kept) if kept else page
    big = clean.resize((round(w0 * scale), round(h0 * scale)), Image.LANCZOS)
    scaled = [Region(bbox=[round(r.bbox[0] * scale), round(r.bbox[1] * scale),
                           round(r.bbox[2] * scale), round(r.bbox[3] * scale)],
                     direction=guess_direction(page, r.bbox),
                     translated_text=text, color=list(r.color), kind=r.kind)
              for r, text in zip(kept, drafts)]
    # 把这一页的「框 + 最终文本」一并带出去：校对要读的是文本，不是 2500 张图。
    dump = [{"bbox": list(r.bbox), "text": t} for r, t in zip(kept, drafts)]
    return render_page(big, scaled, cfg), len(kept), changed, sum(len(t) for t in drafts), dump


def process_volume(series: str, cbz: Path, out_path: Path, det, mocr, inpainter, cfg, llm,
                   max_pages: int = 0) -> dict:
    from comic_gen.core.utils import atomic_save_image

    work = WORK_ROOT / series / cbz.stem
    work.mkdir(parents=True, exist_ok=True)
    qa_rows: list[dict] = []
    text_rows: list[dict] = []
    with zipfile.ZipFile(cbz) as z:
        names = [n for n in z.namelist() if n.lower().endswith(IMG_SUFFIX)]
        total = len(names)
        t0 = time.time()
        for i, name in enumerate(names, 1):
            if max_pages and i > max_pages:
                break
            dst = work / name
            if dst.exists() and dst.stat().st_size > 0:
                continue
            try:
                with z.open(name) as f:
                    page = Image.open(f).convert("RGB")
                out, regions, changed, chars, dump = process_page(page, det, mocr, inpainter,
                                                                   cfg, llm)
                atomic_save_image(out.convert("RGB"), dst, jpeg_quality=JPEG_QUALITY)
                ratio = changed / max(1, chars)
                qa_rows.append({"page": name, "regions": regions, "chars": chars,
                                "changed": changed, "ratio": round(ratio, 3),
                                "flag": ratio > 0.4})
                text_rows.append({"page": name, "regions": dump})
            except Exception as e:  # 单页失败不能让整卷停
                log(f"  !! {series} {cbz.name} p{i}/{total} {Path(name).name}: {e}")
                traceback.print_exc()
                continue
            if i % 20 == 0 or i >= total:
                rate = (time.time() - t0) / i
                log(f"  {series} {cbz.name} {i}/{total}  {rate:.1f}s/页  剩 {rate * (total - i) / 60:.0f} 分")

    qa_path = OUT_ROOT / series / f"{cbz.stem}.qa.jsonl"
    qa_path.parent.mkdir(parents=True, exist_ok=True)
    with qa_path.open("w", encoding="utf-8") as f:
        for row in qa_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    text_path = OUT_ROOT / series / f"{cbz.stem}.text.jsonl"
    with text_path.open("w", encoding="utf-8") as f:
        for row in text_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".cbz.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            p = work / name
            if p.exists():
                zf.write(p, name)
    tmp.replace(out_path)
    if not max_pages:
        shutil.rmtree(work, ignore_errors=True)
    flags = sum(1 for r in qa_rows if r["flag"])
    return {"pages": len(qa_rows), "flagged": flags,
            "changed_chars": sum(r["changed"] for r in qa_rows),
            "chars": sum(r["chars"] for r in qa_rows)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 卷（0 = 全量）")
    ap.add_argument("--max-pages", type=int, default=0, help="每卷只处理前 N 页（冒烟用）")
    args = ap.parse_args()

    from manga_ocr import MangaOcr

    from comic_gen.core.config import Config
    from comic_gen.core.model_locator import find_local_gguf

    # llama.cpp 的 CUDA 后端要能找到 torch 自带的 CUDA 运行时 DLL（torch/lib），
    # 否则加载 llama.dll 时会报 "找不到模块或其依赖项"（项目里 _LlamaGGUFTranslator
    # 构造时也做同一件事）。必须在 import llama_cpp **之前**调用——import 时就会
    # 加载 DLL，之后再加路径已经晚了。
    from comic_gen.pipeline._cuda_runtime import preload_cuda_runtime
    from comic_gen.pipeline.detect import ComicDetector
    from comic_gen.pipeline.inpaint import Inpainter

    preload_cuda_runtime()
    from llama_cpp import Llama

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    log("=== 批处理开始 ===")
    det = ComicDetector("ogkalu/comic-text-and-bubble-detector", device="cuda", threshold=0.3)
    mocr = MangaOcr()
    inpainter = Inpainter("cuda")

    # 校对用的小模型：Qwen3-4B（非思考版），显存和速度都比 14B 合适，能与另外三个
    # 模型同时驻留（4B Q4 约 2.5 GB）。
    proof_path = find_local_gguf("Qwen3-4B-Instruct-2507-Q4_K_M.gguf")
    llm = None
    if proof_path is not None:
        engine = Llama(model_path=str(proof_path), n_ctx=2048, n_gpu_layers=-1, verbose=False)

        def call_llm(prompt: str) -> str:
            resp = engine.create_chat_completion(
                messages=[{"role": "system", "content": "你是中文漫画校对员，只订正 OCR 错字。"},
                          {"role": "user", "content": prompt}],
                temperature=0.0, top_p=1.0, repeat_penalty=1.05, max_tokens=1024,
            )
            return resp["choices"][0]["message"]["content"] or ""

        llm = call_llm
        log(f"校对模型：{proof_path.name}")
    else:
        log("!! 没找到 Qwen3-4B 校对模型，本次不做 LLM 校对（只做字形表）")

    cfg = Config(translator="mock", source_lang="zh", target_lang="简体中文",
                 font_path=FONT, min_font_size=20, max_font_size=96,
                 direction="auto", device="cuda")
    log("模型就绪（检测 / manga-ocr / LaMa / 校对）")

    volumes: list[tuple[str, Path]] = []
    for series in sorted(p for p in SERIES_ROOT.iterdir() if p.is_dir() and not p.name.startswith("_")):
        volumes += [(series.name, c) for c in sorted(series.glob("*.cbz"))]
    if args.limit:
        volumes = volumes[: args.limit]
    log(f"待处理 {len(volumes)} 卷")

    for idx, (series, cbz) in enumerate(volumes, 1):
        out_path = OUT_ROOT / series / f"{cbz.stem} [300dpi 简].cbz"
        if out_path.exists() and out_path.stat().st_size > 0 and not args.max_pages:
            log(f"[{idx}/{len(volumes)}] 跳过（已存在）{series} {cbz.name}")
            continue
        log(f"[{idx}/{len(volumes)}] 开始 {series} {cbz.name}")
        t = time.time()
        try:
            stats = process_volume(series, cbz, out_path, det, mocr, inpainter, cfg, llm,
                                   max_pages=args.max_pages)
            mb = out_path.stat().st_size / 1024 / 1024
            log(f"[{idx}/{len(volumes)}] 完成 {series} {cbz.name} -> {mb:.0f} MB "
                f"耗时 {(time.time() - t) / 60:.1f} 分  页 {stats['pages']}  "
                f"校对改动 {stats['changed_chars']}/{stats['chars']} 字  "
                f"可疑页 {stats['flagged']}")
        except Exception as e:
            log(f"[{idx}/{len(volumes)}] 失败 {series} {cbz.name}: {e}")
            traceback.print_exc()

    log("=== 批处理结束 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
