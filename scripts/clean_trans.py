#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""清洗视觉转录结果：把「抄图时带进来的排版噪声」去掉，再交给 audit_diff 比对。

要清的东西（都是实测遇到的）：
- 日文原版一行一句、行间留空隙，模型会把全角空格甚至普通空格一起抄进来，
  中文重排是按气泡宽度自动折行的，这些空格只会让排版出现莫名其妙断口；
- 冒号混进省略号（`…:` / `:…`），是模型对「三点省略号」的误判；
- 半角标点（`!` `?` `,`）：成品统一全角。

用法：
  python scripts/clean_trans.py <转录目录>            # 处理目录下所有 *.json
  python scripts/clean_trans.py a.json b.json         # 只处理指定文件
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def clean(text: str) -> str:
    s = text.replace("…:", "…").replace(":…", "…")
    s = s.replace("…：", "…").replace("：…", "…")
    s = re.sub(r"…{2,}", "…", s)
    s = s.replace("!!!", "！！").replace("!!", "！").replace("??", "？")
    s = s.replace("!?", "！？").replace("?!", "？！")
    s = s.replace("!", "！").replace("?", "？").replace(",", "，").replace(";", "；")
    # 模型常把冒号当中文逗号用（「都会变强:可恶…！」），残留在正文里很刺眼。
    # 只在两侧都是汉字时替换，避免动到「10:30」这类。
    s = re.sub(r"(?<=[\u4e00-\u9fff])[:：](?=[\u4e00-\u9fff])", "，", s)
    s = s.replace("：", "：").replace("\u3000", "")
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"^[：:]+", "", s)
    s = re.sub(r"[：:]+$", "", s)      # 句尾残留冒号（模型把冒号当句号用）
    return s.strip()


def clean_file(path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    # 目录里可能有 `_plan.json` 这类批次清单（值是列表，不是区域字典），
    # 一并清洗会 AttributeError 崩掉。只处理「页 → {序号: 文本}」结构的文件。
    out = {page: {k: clean(str(v)) for k, v in regions.items()}
           for page, regions in data.items() if isinstance(regions, dict)}
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(out)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    targets: list[Path] = []
    for arg in argv[1:]:
        p = Path(arg)
        targets.extend(sorted(p.glob("*.json")) if p.is_dir() else [p])
    if not targets:
        print("没有可处理的 JSON")
        return 1
    total = 0
    for path in targets:
        pages = clean_file(path)
        total += pages
        print(f"{path.name}: {pages} 页")
    print(f"共清洗 {len(targets)} 个文件 / {total} 页")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
