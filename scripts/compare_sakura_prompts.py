"""只读对比：Sakura「当前生产 prompt 格式」vs「官方 v1.0 推荐格式」。

目的：验证 AUDIT.md P2-6 的猜想——当前 translate.py 用「编号批量」格式调 Sakura，
官方 README 明确警告「使用非本项目提供的 prompt 格式不保证质量」。控制变量：只改
prompt 格式，采样参数统一用官方推荐（temperature=0.1, top_p=0.3, repeat_penalty=1.0）。
不写任何中间产物，只读 pages.json + 只读 GGUF，输出一份对比报告到 UTF-8 文件。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from comic_gen.pipeline._cuda_runtime import preload_cuda_runtime
from comic_gen.pipeline.translate import _SAKURA_SYS, BaseTranslator, _is_failed_translation

PAGES = r"E:\某漫画杂志\某漫画杂志_translated\pages.json"
MODEL = r"F:\hf_cache\sakura-7b-qwen2.5-v1.0-iq4xs.gguf"
OUT = Path(__file__).parent / "_sakura_format_compare_report.txt"

# 官方 README（v1.0）的 system prompt 原文，与 _SAKURA_SYS 只有一句不同
_OFFICIAL_SYS = (
    "你是一个轻小说翻译模型，可以流畅通顺地以日本轻小说的风格将日文翻译成简体中文，"
    "并联系上下文正确使用人称代词，不擅自添加原文中没有的代词。"
)

# 从 _preview_pages_out.txt 里挑的对话连贯页（避开混入幻觉/广告垃圾的页）
PAGES_TO_TEST = [
    "027.JPEG", "028.JPEG", "029.JPEG", "031.JPEG", "032.JPEG", "035.JPEG",
    "040.JPEG", "064.JPEG", "065.JPEG", "066.JPEG", "103.JPEG", "104.JPEG",
]

TEMP = 0.1
TOP_P = 0.3
REPEAT_PENALTY = 1.0


def current_batch_prompt(texts: list[str]) -> str:
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    return (
        f"把下列编号文本逐条翻译成简体中文，保持编号格式输出，"
        f"每条一行，不要合并不要遗漏：\n{numbered}"
    )


def official_prompt(texts: list[str]) -> str:
    return "将下面的日文文本翻译成中文：" + "\n".join(texts)


def parse_plain_lines(text: str, n: int) -> list[str | None]:
    """官方格式按行对齐解析：输出行数 == 输入行数才算对齐，否则全置 None。"""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) != n:
        return [None] * n
    return lines


def main() -> None:
    d = json.load(open(PAGES, encoding="utf-8"))
    by_name = {}
    for p in d["pages"]:
        by_name[p["image"]] = [r["source_text"].strip() for r in p["regions"] if r["source_text"].strip()]

    preload_cuda_runtime()
    from llama_cpp import Llama

    llm = Llama(model_path=MODEL, n_ctx=4096, n_gpu_layers=-1, verbose=False)

    def call(messages, max_tokens: int) -> str:
        resp = llm.create_chat_completion(
            messages=messages, temperature=TEMP, top_p=TOP_P,
            max_tokens=max_tokens, repeat_penalty=REPEAT_PENALTY,
        )
        return (resp["choices"][0]["message"]["content"] or "").strip()

    report = []
    a_fail = b_fail = 0
    a_align = b_align = 0
    total = 0
    t0 = time.time()

    for name in PAGES_TO_TEST:
        texts = by_name[name]
        n = len(texts)
        total += n
        max_tokens = min(2048, 96 + int(n * 80))

        raw_a = call(
            [{"role": "system", "content": _SAKURA_SYS},
             {"role": "user", "content": current_batch_prompt(texts)}],
            max_tokens,
        )
        raw_b = call(
            [{"role": "system", "content": _OFFICIAL_SYS},
             {"role": "user", "content": official_prompt(texts)}],
            max_tokens,
        )

        parsed_a = BaseTranslator._parse_numbered(raw_a, n)
        parsed_b = parse_plain_lines(raw_b, n)
        a_aligned = sum(1 for i, v in enumerate(parsed_a)
                        if v is not None and not _is_failed_translation(texts[i], v))
        b_aligned = sum(1 for i, v in enumerate(parsed_b)
                        if v is not None and not _is_failed_translation(texts[i], v))

        a_fail += n - a_aligned
        b_fail += n - b_aligned
        if a_aligned == n:
            a_align += 1
        if b_aligned == n:
            b_align += 1

        report.append(f"\n{'='*72}\n=== [{name}] ({n} regions) | A对齐 {a_aligned}/{n} B对齐 {b_aligned}/{n}\n{'='*72}")
        for i in range(n):
            report.append(f"\n[SRC] {texts[i]}")
            report.append(f"[A]   {parsed_a[i] if parsed_a[i] is not None else '<未对齐/失败>'}")
            report.append(f"[B]   {parsed_b[i] if parsed_b[i] is not None else '<未对齐/失败>'}")
        print(f"[{name}] done, elapsed {time.time()-t0:.0f}s", flush=True)

    report.append(
        f"\n{'='*72}\nSUMMARY: pages {len(PAGES_TO_TEST)} regions {total}\n"
        f"A(current): {a_align}/{len(PAGES_TO_TEST)} pages fully aligned, {a_fail}/{total} regions failed\n"
        f"B(official): {b_align}/{len(PAGES_TO_TEST)} pages fully aligned, {b_fail}/{total} regions failed\n"
        f"total elapsed {time.time()-t0:.0f}s"
    )
    OUT.write_text("\n".join(report), encoding="utf-8")
    print(f"\nreport written: {OUT}")


if __name__ == "__main__":
    sys.exit(main())
