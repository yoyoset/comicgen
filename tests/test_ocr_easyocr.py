"""assemble_easyocr_text 纯函数测试（不加载 easyocr/torch）。

EasyOCR readtext() 的返回形状: list[(四角点坐标, 文本, 置信度)]，
四角点形如 [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]（左上/右上/右下/左下）。
"""
from comic_gen.pipeline.ocr import assemble_easyocr_text


def _box(x, y, w=20, h=10):
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def test_assembles_in_reading_order_not_return_order():
    """EasyOCR 不保证按阅读顺序返回；原实现直接顺序拼接会打乱句子。"""
    results = [
        (_box(0, 50), "第二行", 0.9),
        (_box(0, 0), "第一行", 0.9),
    ]
    assert assemble_easyocr_text(results) == "第一行 第二行"


def test_left_to_right_within_same_row():
    results = [
        (_box(100, 0), "右", 0.9),
        (_box(0, 0), "左", 0.9),
    ]
    assert assemble_easyocr_text(results) == "左 右"


def test_low_confidence_fragment_dropped():
    """装饰性花纹被幻觉识别成的乱码片段（低置信度）不该混进正文。"""
    results = [
        (_box(0, 0), "正文", 0.95),
        (_box(0, 20), "乱码", 0.05),
    ]
    assert assemble_easyocr_text(results) == "正文"


def test_all_below_threshold_returns_empty():
    results = [(_box(0, 0), "噪声", 0.1)]
    assert assemble_easyocr_text(results) == ""


def test_empty_results():
    assert assemble_easyocr_text([]) == ""


def test_custom_threshold():
    results = [(_box(0, 0), "边界值", 0.5)]
    assert assemble_easyocr_text(results, min_conf=0.6) == ""
    assert assemble_easyocr_text(results, min_conf=0.5) == "边界值"
