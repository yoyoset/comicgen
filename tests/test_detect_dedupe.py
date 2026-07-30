"""suppress_overlaps 纯函数测试（不加载模型/不需要网络）。

场景来自实测复现：RT-DETRv2 的 post_process_object_detection 拍平 queries×classes 取
top-k，同一个 query 在多个类别都超阈值时会输出完全相同的框（不同 label）。
"""
from comic_gen.core.models import Region
from comic_gen.pipeline.detect import _containment, _iou, suppress_overlaps


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
