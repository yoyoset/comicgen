#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把视觉模型的逐格转录结果与现有文字比对，产出审计报告 + fix json。

这是 docs/AUDIT_WORKFLOW.md 第 2 节 C 步（比对）的落地实现：A 步用
make_region_sheet.py 制表，B 步视觉模型回答 {#序号: 文字}，C/D 步用本脚本。

用法：
  python scripts/audit_diff.py "<系列目录>" "<卷名>" --trans-dir <转录目录>

转录目录里每个 JSON 文件形如：
  {"0137.jpg": {"0": "让我跟你一护来替妳分担。", "1": "从以前就是这样。"}, ...}
（被审计的页可以缺省；缺省的页不参与比对，仍记为「未审计」）

输出：
  <系列目录>/<卷名>.audit.md      每页一行：页 / 区域 / 现有 / 应为 / 类别
  <系列目录>/<卷名>.fix.json      只含「实质不同」的区域（格式同 retext_volume.py）

分类规则（宁可漏判，不可误改）：
  一致        现有 == 应为（忽略标点/繁简差异后相同）
  繁简/标点   只差繁体字、异体字、标点、空白       → 不写入 fix
  实质不同    错字/漏字/多字，且不是中文误识的常见变体 → 写入 fix
  存疑        视觉转录里含 '?'                     → 写入 audit 的「待人工核对」，不进 fix

为什么繁简差异不算错：渲染前统一走 zhconv 转简体，繁体存进去也会被转掉；
把它算成差异会让 fix json 变成整卷重排，掩盖真正的识别错误。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    import zhconv
except Exception:  # pragma: no cover - 缺依赖时退化为不做繁简归一
    zhconv = None

_SPACE = r"[\s\u3000]"

# 台湾版原书用的口语，大陆简体里没有这种说法，但**印在纸上就是这么写的**，
# 所以视觉转录会照抄、audit_diff 会判「一致」，用户抽查时一眼看出不通。
# 这类词必须在比对前就归一成目标用词，否则永远进不了 fix.json。
# 只收已确认的（来自审查报告），不要凭感觉往里加。
TWISMS: dict[str, str] = {
    "好康的": "好的",
    "好康": "好处",
}


def apply_twisms(text: str) -> str:
    for src in sorted(TWISMS, key=len, reverse=True):
        text = text.replace(src, TWISMS[src])
    return text


def normalize(text: str) -> str:
    """繁转简 + 去空白，只用于判断「是否实质不同」。

    注意**不能去标点**：校对时补上的「。」「」也是要写进成品的内容，
    去掉标点会把这类修正误判成「无差异」而丢掉。
    繁体差异可以忽略：retext_volume.py 在渲染前对每段文字都跑 zhconv。
    """
    if zhconv is not None:
        text = zhconv.convert(text, "zh-cn")
    return re.sub(_SPACE, "", text)


def load_texts(text_path: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for line in text_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        out[row["page"]] = row["regions"]
    return out


def load_transcriptions(trans_dir: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for path in sorted(trans_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for page, regions in data.items():
            if not isinstance(regions, dict):
                continue
            cur = out.setdefault(page, {})
            for k, v in regions.items():
                key = str(k).lstrip("#")
                cur[key] = str(v).strip()
    return out


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="audit_diff.py")
    ap.add_argument("series_dir")
    ap.add_argument("stem")
    ap.add_argument("--trans-dir", required=True, help="视觉转录 JSON 所在目录")
    ap.add_argument("--max-report", type=int, default=500,
                    help="audit.md 里最多列多少条差异（默认 500）")
    ap.add_argument("--skip-pages", nargs="*", default=[],
                    help="非主体页（封面/目录/扉页/版权/广告/下集预告），"
                         "这些页不参与比对、不进 fix，保持原图。页码写成 0001 或 0001.jpg")
    args = ap.parse_args(argv[1:])

    skip = {p if p.endswith(".jpg") else f"{p}.jpg" for p in args.skip_pages}

    series_dir = Path(args.series_dir)
    stem = args.stem
    text_path = series_dir / f"{stem}.text.jsonl"
    if not text_path.exists():
        print(f"缺少 {text_path}")
        return 1

    texts = load_texts(text_path)
    trans = load_transcriptions(Path(args.trans_dir))
    if not trans:
        print("转录目录里没有 JSON")
        return 1

    buckets = {"一致": 0, "繁简/标点": 0, "实质不同": 0, "存疑": 0, "缺转录": 0, "保留原图": 0}
    overflow: list[str] = []
    lines: list[str] = []
    fix: dict[str, dict[str, str]] = {}
    suspect: list[str] = []

    # 先扫一遍格号越界（串页），这类错误不会表现为"差异"，会静默丢失，
    # 所以必须在正式比对之前单独报出来。
    for page in sorted(texts):
        regions = texts[page]
        got = trans.get(page)
        if not regions or got is None:
            continue
        for key in got:
            if not key.isdigit() or int(key) >= len(regions):
                overflow.append(f"{page} #{key}（该页只有 {len(regions)} 格）")

    for page in sorted(texts):
        regions = texts[page]
        if not regions:
            continue          # 无字页（纯画面/纯图），本来就没有可转录的内容
        if page in skip:
            continue          # 封面/目录/扉页/版权/广告/预告：保持原图，不改
        got = trans.get(page)
        if got is None:
            buckets["缺转录"] += 1
            continue
        for idx, meta in enumerate(regions):
            want = got.get(str(idx))
            have = str(meta.get("text") or "").strip()
            if want is None:
                # 该格视觉模型没回答：跳过，不当作差异
                continue
            if want in ("", "-"):
                # 空字符串是**显式指令**：这一格保持原图（拟声词/美术字不重排）。
                # 必须写进 fix.json，否则 retext 会继续沿用 text.jsonl 里的旧文字
                # 把那格重排成黑体（实测 0044 的四个彩色拟声词就是这样被覆盖的）。
                if have:
                    fix.setdefault(page, {})[str(idx)] = ""
                    buckets["保留原图"] += 1
                    if len(lines) < args.max_report:
                        lines.append(f"| {page} | {idx} | {have} | （保留原图） | 保留原图 |")
                continue
            if "?" in want:
                buckets["存疑"] += 1
                suspect.append(f"{page} #{idx} 现有「{have}」 转录「{want}」")
                continue
            want = apply_twisms(want)
            if want == have:
                buckets["一致"] += 1
                continue
            if normalize(want) == normalize(have):
                buckets["繁简/标点"] += 1
                continue
            buckets["实质不同"] += 1
            fix.setdefault(page, {})[str(idx)] = want
            if len(lines) < args.max_report:
                lines.append(f"| {page} | {idx} | {have} | {want} | 实质不同 |")

    total = sum(buckets[k] for k in ("一致", "繁简/标点", "实质不同", "存疑", "保留原图"))
    rate = (buckets["实质不同"] / total * 100) if total else 0.0

    md = [f"# {stem} 视觉审计报告", ""]
    md.append(f"- 参与比对区域数：{total}")
    md.append(f"- 一致：{buckets['一致']}")
    md.append(f"- 繁简/标点差异（不修）：{buckets['繁简/标点']}")
    md.append(f"- 实质不同（进 fix）：{buckets['实质不同']}（{rate:.1f}%）")
    md.append(f"- 存疑，待人工核对：{buckets['存疑']}")
    md.append(f"- 保留原图（拟声词/美术字，不重排）：{buckets['保留原图']}")
    md.append(f"- 本卷未转录页数：{buckets['缺转录']}")
    md.append("")
    if rate > 2.0:
        md.append("> ⚠ 实质不同超过 2%：按 `docs/AUDIT_WORKFLOW.md` 第 3 节，"
                  "本卷判定为整体不可信，应**整卷重转录**，不要局部修补。")
        md.append("")
    md.append("## 差异明细")
    md.append("")
    md.append("| 页 | 区域 | 现有 | 应为 | 类别 |")
    md.append("|---|---|---|---|---|")
    md.extend(lines)
    md.append("")
    if suspect:
        md.append("## 待人工核对（视觉转录标了 ?）")
        md.append("")
        md.extend(f"- {s}" for s in suspect)
        md.append("")
    if overflow:
        md.append("## ⚠ 转录串页/格号越界（必须重转录这些页）")
        md.append("")
        md.extend(f"- {s}" for s in overflow)
        md.append("")

    (series_dir / f"{stem}.audit.md").write_text("\n".join(md), encoding="utf-8")
    (series_dir / f"{stem}.fix.json").write_text(
        json.dumps(fix, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"审计完成：实质不同 {buckets['实质不同']}/{total}（{rate:.1f}%），"
          f"存疑 {buckets['存疑']}，未转录页 {buckets['缺转录']}")
    print(f"-> {(series_dir / f'{stem}.audit.md')}")
    print(f"-> {(series_dir / f'{stem}.fix.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
