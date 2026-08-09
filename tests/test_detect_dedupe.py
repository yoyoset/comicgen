"""suppress_overlaps / reading_order 纯函数测试（不加载模型/不需要网络）。

场景来自实测复现：RT-DETRv2 的 post_process_object_detection 拍平 queries×classes 取
top-k，同一个 query 在多个类别都超阈值时会输出完全相同的框（不同 label）。
"""
from comic_gen.core.models import Region
from comic_gen.pipeline.detect import (
    _containment,
    _iou,
    filter_region_quality,
    reading_order,
    suppress_overlaps,
)


def test_identical_box_different_label_keeps_higher_score():
    """实测复现的场景：同一个框以 label 1(score 0.77) 和 label 2(score 0.60) 各输出一次。"""
    a = Region(bbox=[100, 100, 100, 100], kind="text_bubble", score=0.769)
    b = Region(bbox=[100, 100, 100, 100], kind="text_free", score=0.599)
    kept = suppress_overlaps([a, b])
    assert len(kept) == 1
    assert kept[0].score == 0.769
    assert kept[0].kind == "text_bubble"


def test_non_overlapping_boxes_both_kept():
    a = Region(bbox=[0, 0, 50, 50], score=0.9)
    b = Region(bbox=[500, 500, 50, 50], score=0.5)
    kept = suppress_overlaps([a, b])
    assert len(kept) == 2


def test_partial_overlap_below_threshold_both_kept():
    """IoU 恰好在阈值以下的两框都要保留：不是所有邻近框都是重复检测。"""
    a = Region(bbox=[0, 0, 100, 100], score=0.9)
    b = Region(bbox=[80, 0, 100, 100], score=0.5)  # IoU = 20*100 / (100*100+100*100-20*100) = 0.111
    assert _iou([0, 0, 100, 100], [80, 0, 180, 100]) < 0.5
    kept = suppress_overlaps([a, b])
    assert len(kept) == 2


def test_small_box_contained_in_large_box_dropped():
    """大框套小框：纯 IoU 判据会漏判，必须用包含比例兜底。"""
    big = Region(bbox=[0, 0, 200, 200], score=0.9)
    small = Region(bbox=[10, 10, 30, 30], score=0.95)  # 完全在 big 内部，但 IoU 很小
    assert _iou([0, 0, 200, 200], [10, 10, 40, 40]) < 0.5
    assert _containment([10, 10, 40, 40], [0, 0, 200, 200]) > 0.9
    kept = suppress_overlaps([big, small])
    assert len(kept) == 1
    assert kept[0].score == 0.95  # 保留 score 更高的那个，不管是大框还是小框


def test_three_way_duplicate_keeps_only_highest():
    a = Region(bbox=[10, 10, 50, 50], score=0.5)
    b = Region(bbox=[10, 10, 50, 50], score=0.9)
    c = Region(bbox=[10, 10, 50, 50], score=0.3)
    kept = suppress_overlaps([a, b, c])
    assert len(kept) == 1
    assert kept[0].score == 0.9


def test_empty_input():
    assert suppress_overlaps([]) == []


def test_quality_filter_drops_only_scores_below_conservative_starting_point():
    low = Region(bbox=[0, 0, 10, 10], score=0.399)
    boundary = Region(bbox=[20, 0, 10, 10], score=0.4)
    high = Region(bbox=[40, 0, 10, 10], score=0.9)
    kept = filter_region_quality([low, boundary, high])
    assert kept == [boundary, high]


def test_quality_filter_does_not_mutate_input():
    regions = [Region(bbox=[0, 0, 10, 10], score=0.2)]
    filter_region_quality(regions)
    assert len(regions) == 1


def test_single_region_passthrough():
    r = Region(bbox=[1, 2, 3, 4], score=0.5)
    kept = suppress_overlaps([r])
    assert kept == [r]


def test_iou_helper_symmetry():
    a, b = [0, 0, 100, 100], [50, 50, 150, 150]
    assert _iou(a, b) == _iou(b, a)


def test_containment_helper_not_symmetric():
    small, big = [10, 10, 40, 40], [0, 0, 200, 200]
    assert _containment(small, big) > 0.9
    assert _containment(big, small) < 0.1


def _brute_force_suppress(regions, iou_thr=0.5, contain_thr=0.9):
    """对照基准：逐个与全部已保留框比较的 O(n²) 实现（网格优化的正确性参照）。"""
    ordered = sorted(regions, key=lambda r: r.score, reverse=True)
    kept: list[Region] = []
    kept_boxes: list[list[int]] = []
    for r in ordered:
        rbox = [r.bbox[0], r.bbox[1], r.bbox[0] + r.bbox[2], r.bbox[1] + r.bbox[3]]
        is_dup = any(
            _iou(rbox, kb) > iou_thr
            or _containment(rbox, kb) > contain_thr
            or _containment(kb, rbox) > contain_thr
            for kb in kept_boxes
        )
        if not is_dup:
            kept.append(r)
            kept_boxes.append(rbox)
    return kept


def _regions_equal(a, b):
    return [(r.bbox, r.score, r.kind) for r in a] == [(r.bbox, r.score, r.kind) for r in b]


def test_grid_matches_brute_force_on_random_layouts():
    """网格空间索引与逐对比较的 O(n²) 基准必须产出完全相同的结果。

    这个差分测试保证把 O(n²) 改成期望 O(n) 空间索引后，去重判定没有行为漂移——
    网格去重的正确性依赖"重叠/包含的框必共享至少一个 cell"，随机布局覆盖了
    大框套小框、跨 cell 重叠、分数乱序等边界。
    """
    import random

    rng = random.Random(20260809)
    for n in (0, 1, 2, 5, 20, 80):
        for _ in range(20):
            regions = []
            for i in range(n):
                w = rng.randint(20, 200)
                h = rng.randint(20, 200)
                x = rng.randint(0, 800 - w)
                y = rng.randint(0, 1200 - h)
                regions.append(
                    Region(
                        bbox=[x, y, w, h],
                        score=rng.uniform(0.3, 0.99),
                        kind=f"k{i}",
                    )
                )
            assert _regions_equal(suppress_overlaps(regions), _brute_force_suppress(regions)), n


def test_grid_handles_many_identical_duplicate_boxes():
    """同一坐标的重复框（实测场景：一个 query 在多个类别超阈值）只留 score 最高的那个。"""
    dupes = [Region(bbox=[100, 100, 50, 50], score=0.9 - i * 0.01, kind=f"d{i}") for i in range(30)]
    kept = suppress_overlaps(dupes)
    assert len(kept) == 1
    assert kept[0].score == 0.9


# ---------------------------------------------------------------- reading_order


def test_reading_order_rtl_same_row_right_before_left():
    """实测场景：日漫同一行两个气泡，右边的先说话，rtl=True 时必须排在前面。"""
    right = Region(bbox=[700, 100, 150, 120], kind="R")
    left = Region(bbox=[150, 100, 150, 120], kind="L")
    ordered = reading_order([left, right], rtl=True)
    assert [r.kind for r in ordered] == ["R", "L"]


def test_reading_order_ltr_same_row_left_before_right():
    right = Region(bbox=[700, 100, 150, 120], kind="R")
    left = Region(bbox=[150, 100, 150, 120], kind="L")
    ordered = reading_order([left, right], rtl=False)
    assert [r.kind for r in ordered] == ["L", "R"]


def test_reading_order_rtl_tolerates_y_jitter():
    """实测场景：气泡不会像素对齐，y 抖动 ±4px 不该打乱行内顺序。"""
    right = Region(bbox=[700, 104, 150, 120], kind="R")
    left = Region(bbox=[150, 100, 150, 120], kind="L")
    ordered = reading_order([left, right], rtl=True)
    assert [r.kind for r in ordered] == ["R", "L"]


def test_reading_order_different_rows_top_before_bottom_regardless_of_rtl():
    top = Region(bbox=[100, 50, 100, 80], kind="TOP")
    bottom = Region(bbox=[100, 800, 100, 80], kind="BOTTOM")
    for rtl in (True, False):
        ordered = reading_order([bottom, top], rtl=rtl)
        assert [r.kind for r in ordered] == ["TOP", "BOTTOM"]


def test_reading_order_empty_input():
    assert reading_order([], rtl=True) == []


def test_reading_order_explicit_band_overrides_median():
    a = Region(bbox=[0, 0, 50, 30], kind="A")
    b = Region(bbox=[0, 40, 50, 30], kind="B")  # 40px 之差
    # band=100 时两者被视为同一行，按 x 排（都是 x=0，稳定排序保持输入顺序）
    ordered = reading_order([a, b], rtl=False, band=100)
    assert [r.kind for r in ordered] == ["A", "B"]
    # band=10 时 y//10 不同，分属不同行
    ordered2 = reading_order([b, a], rtl=False, band=10)
    assert [r.kind for r in ordered2] == ["A", "B"]
