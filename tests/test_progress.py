"""ui.workers.overall_progress 的离线测试：纯函数，不碰 Qt/Runner。

之前 UI 直接把"当前阶段内的 i/total"当整体进度喂给进度条，detect/ocr/translate/
render 四个阶段各自把进度条从 0 填到 100%——用户体感是"一轮任务填满四次"。
"""
from comic_gen.ui.workers import _format_duration, overall_progress


def test_each_stage_occupies_its_own_quarter_of_the_bar():
    # detect 阶段跑到一半，应该落在 [0, 0.25) 区间，而不是 0.5
    fraction, _ = overall_progress("detect", 5, 10, elapsed_s=0)
    assert 0 < fraction < 0.25

    # render 是最后一个阶段，跑到一半应该落在 [0.75, 1.0)
    fraction, _ = overall_progress("render", 5, 10, elapsed_s=0)
    assert 0.75 <= fraction < 1.0


def test_progress_is_monotonic_across_all_four_stages():
    elapsed = 0
    fractions = [
        overall_progress(stage, i, 10, elapsed)[0]
        for stage in ("detect", "ocr", "translate", "render")
        for i in range(1, 11)
    ]
    assert fractions == sorted(fractions)
    assert fractions[-1] <= 1.0


def test_unknown_stage_name_does_not_crash():
    fraction, desc = overall_progress("load", 1, 1, elapsed_s=0)
    assert 0 <= fraction <= 1
    assert "load" in desc


def test_eta_omitted_when_progress_too_small_to_extrapolate():
    _, desc = overall_progress("detect", 1, 1000, elapsed_s=5)
    assert "预计剩余" not in desc


def test_eta_shown_once_progress_and_elapsed_time_are_meaningful():
    _, desc = overall_progress("render", 5, 10, elapsed_s=100)
    assert "预计剩余" in desc


def test_format_duration_buckets():
    assert _format_duration(30) == "30秒"
    assert _format_duration(90) == "1分钟"
    assert _format_duration(3700) == "1小时1分钟"
    assert _format_duration(-5) == "0秒"
