"""pages.json 区域质量统计的纯函数；不加载模型，也不写输入文件。"""
from __future__ import annotations

import math


def percentile(values: list[float], percent: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    return {
        "min": min(values),
        "p01": percentile(values, 1),
        "p05": percentile(values, 5),
        "p25": percentile(values, 25),
        "p50": percentile(values, 50),
        "p75": percentile(values, 75),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": max(values),
        "mean": sum(values) / len(values),
    }


def region_records(pages: list[dict]) -> list[dict]:
    records = []
    for page_number, page in enumerate(pages, 1):
        for region_number, region in enumerate(page.get("regions", []), 1):
            bbox = region.get("bbox", [0, 0, 0, 0])
            width = max(0, float(bbox[2])) if len(bbox) > 2 else 0
            height = max(0, float(bbox[3])) if len(bbox) > 3 else 0
            short_side = min(width, height)
            records.append(
                {
                    "page": page_number,
                    "region": region_number,
                    "area": width * height,
                    "aspect_ratio": max(width, height) / short_side if short_side else math.inf,
                    "score": float(region.get("score", 0.0)),
                    "source_text": str(region.get("source_text", "")),
                    "translated_text": str(region.get("translated_text", "")),
                    "image": page.get("image", ""),
                }
            )
    return records


def score_bucket(score: float, boundaries: tuple[float, ...] = (0.4, 0.5, 0.6, 0.7)) -> str:
    previous = 0.0
    for boundary in boundaries:
        if score < boundary:
            return f"[{previous:.1f}, {boundary:.1f})"
        previous = boundary
    return f"[{previous:.1f}, 1.0]"


def simulate_filter(records: list[dict], *, score_min=None, area_min=None, aspect_max=None) -> dict:
    dropped = [
        record
        for record in records
        if (
            (score_min is not None and record["score"] < score_min)
            or (area_min is not None and record["area"] < area_min)
            or (aspect_max is not None and record["aspect_ratio"] > aspect_max)
        )
    ]
    return {
        "dropped": len(dropped),
        "empty_source": sum(not r["source_text"].strip() for r in dropped),
        "sample": dropped[:5],
    }


def _format_summary(name: str, values: list[float]) -> str:
    stats = summarize(values)
    if not stats:
        return f"{name}: 无数据"
    return f"{name} (n={len(values)}): " + " ".join(
        f"{key}={value:.3f}" for key, value in stats.items()
    )


def format_batch_report(
    pages: list[dict], *, stage_seconds: dict[str, float] | None = None,
    stage_note: str = "", sample_size: int = 5
) -> str:
    records = region_records(pages)
    if not records:
        return "=== comic-gen 区域质量决策报告（只读） ===\n页面数: %d\n区域数: 0" % len(pages)
    scores = [r["score"] for r in records]
    areas = [r["area"] for r in records]
    aspects = [r["aspect_ratio"] for r in records if math.isfinite(r["aspect_ratio"])]
    lines = [
        "=== comic-gen 区域质量决策报告（只读） ===",
        f"页面数: {len(pages)}",
        f"区域数: {len(records)}",
        f"OCR 空字符串: {sum(not r['source_text'].strip() for r in records)}",
        "",
        _format_summary("面积(px²)", areas),
        _format_summary("长宽比(max/min)", aspects),
        _format_summary("检测分数", scores),
        "",
        f"阶段耗时说明: {stage_note or 'job.json 未提供阶段耗时'}",
        "=== 按检测分数分桶抽样（请人眼判断是否为垃圾区域） ===",
    ]
    buckets = {}
    for record in records:
        buckets.setdefault(score_bucket(record["score"]), []).append(record)
    for bucket in ("[0.0, 0.4)", "[0.4, 0.5)", "[0.5, 0.6)", "[0.6, 0.7)", "[0.7, 1.0]"):
        bucket_records = buckets.get(bucket, [])
        lines.append(f"{bucket}: {len(bucket_records)} 个")
        for record in bucket_records[:sample_size]:
            lines.append(
                f"  p{record['page']}r{record['region']} score={record['score']:.3f} "
                f"area={record['area']:.0f} ratio={record['aspect_ratio']:.2f} "
                f"原文={record['source_text']!r} 译文={record['translated_text']!r}"
            )
    lines.extend([
        "",
        "=== 候选规则独立模拟（不是产品默认值，须主脑决策） ===",
        "面积/长宽比候选由本数据分位数生成，仅为未校准的观察起点。",
    ])
    candidates = [(f"score < {threshold:.1f}", simulate_filter(records, score_min=threshold))
                  for threshold in (0.4, 0.5, 0.6, 0.7)]
    candidates += [
        (f"area < p{percent} ({threshold:.2f})", simulate_filter(records, area_min=threshold))
        for percent in (1, 5)
        for threshold in [round(percentile(areas, percent), 2)]
    ]
    candidates += [
        (f"aspect > p{percent} ({threshold:.2f})", simulate_filter(records, aspect_max=threshold))
        for percent in (95, 99)
        for threshold in [round(percentile(aspects, percent), 2)]
    ]
    for label, result in candidates:
        savings = "不可计算（缺少阶段耗时）"
        if stage_seconds:
            savings = ", ".join(
                f"{stage}≈{seconds * result['dropped'] / len(records):.2f}s"
                for stage, seconds in stage_seconds.items()
            )
        lines.append(
            f"{label}: 砍掉 {result['dropped']} ({result['dropped'] / len(records):.1%}), "
            f"其中空 OCR {result['empty_source']}，节省 {savings}"
        )
    lines.extend([
        "",
        "注：脚本只能确认空 OCR；‘看起来确实是垃圾’必须依据上方抽样由人眼标记，",
        "因此没有把 OCR 非空区域擅自判为垃圾，也没有改写 pages.json 或图片。",
    ])
    return "\n".join(lines)
