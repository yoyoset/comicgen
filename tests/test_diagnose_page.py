from scripts.region_quality import (
    format_batch_report,
    percentile,
    region_records,
    score_bucket,
    simulate_filter,
)


def test_percentile_interpolates_and_rejects_empty_input():
    assert percentile([1, 2, 4, 8], 50) == 3


def test_region_records_calculates_area_and_aspect_ratio():
    records = region_records(
        [{"image": "p.jpg", "regions": [{"bbox": [1, 2, 10, 20], "score": 0.6}]}]
    )
    assert records[0]["area"] == 200
    assert records[0]["aspect_ratio"] == 2


def test_score_bucket_uses_observed_boundaries():
    assert score_bucket(0.399) == "[0.0, 0.4)"
    assert score_bucket(0.7) == "[0.7, 1.0]"


def test_simulate_filter_reports_only_dropped_regions():
    records = [
        {"score": 0.3, "area": 20, "aspect_ratio": 2, "source_text": "a"},
        {"score": 0.8, "area": 30, "aspect_ratio": 2, "source_text": ""},
    ]
    result = simulate_filter(records, score_min=0.5)
    assert result["dropped"] == 1
    assert result["empty_source"] == 0
    assert len(records) == 2


def test_batch_report_exposes_human_review_samples_and_missing_timing():
    report = format_batch_report(
        [{"regions": [{"bbox": [0, 0, 10, 10], "score": 0.3, "source_text": "花"}]}]
    )
    assert "请人眼判断" in report
    assert "不可计算（缺少阶段耗时）" in report
