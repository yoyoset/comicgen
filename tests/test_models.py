"""Tests for core.models (offline, no models/network)."""
from comic_gen.core.models import STATUS_PENDING, STATUS_RENDERED, Page, Region


def test_region_creation():
    """Test Region creation and attributes."""
    region = Region(
        bbox=(10, 20, 100, 50),
        direction="horizontal",
        source_text="日本語",
        translated_text="日语",
        color=(0, 0, 0),
        kind="text_free",
    )
    assert region.bbox == (10, 20, 100, 50)
    assert region.source_text == "日本語"
    assert region.translated_text == "日语"


def test_region_to_dict_roundtrip():
    """Test Region serialization and deserialization."""
    region = Region(
        bbox=(10, 20, 100, 50),
        direction="horizontal",
        source_text="原文",
        translated_text="译文",
        color=(255, 128, 0),
        kind="text_free",
    )

    region_dict = region.to_dict()
    region_restored = Region.from_dict(region_dict)

    assert region_restored.bbox == region.bbox
    assert region_restored.source_text == region.source_text
    assert region_restored.translated_text == region.translated_text
    assert region_restored.color == region.color
    assert region_restored.kind == region.kind


def test_page_creation():
    """Test Page creation with status."""
    page = Page(image="page_001.png", width=800, height=600, status=STATUS_PENDING)
    assert page.image == "page_001.png"
    assert page.status == STATUS_PENDING
    assert page.regions == []
    assert page.error == ""  # 空串表示"无错误"，序列化进 pages.json 时比 None 干净


def test_page_with_regions():
    """Test Page with multiple regions."""
    regions = [
        Region(bbox=(10, 10, 50, 30), direction="h", kind="text"),
        Region(bbox=(10, 50, 50, 30), direction="h", kind="text"),
    ]
    page = Page(image="p1.png", width=800, height=600, status=STATUS_PENDING, regions=regions)
    assert len(page.regions) == 2
    assert page.regions[0].bbox == (10, 10, 50, 30)


def test_page_to_dict_roundtrip():
    """Test Page serialization with regions."""
    region = Region(bbox=(5, 5, 100, 100), direction="h", source_text="テスト")
    page = Page(
        image="test.png",
        width=800,
        height=600,
        status=STATUS_RENDERED,
        regions=[region],
        error="",
    )

    page_dict = page.to_dict()
    page_restored = Page.from_dict(page_dict)

    assert page_restored.image == page.image
    assert page_restored.width == page.width
    assert page_restored.status == page.status
    assert len(page_restored.regions) == 1
    assert page_restored.regions[0].source_text == "テスト"


def test_page_error_stage_default_and_roundtrip():
    page = Page(image="a.png", error="检测失败: x", error_stage="detect")
    restored = Page.from_dict(page.to_dict())
    assert restored.error_stage == "detect"


def test_page_from_dict_without_error_stage_defaults_to_empty():
    """读旧版 pages.json（没有 error_stage 字段）不应报错，回落默认空串。"""
    old_dict = {"image": "a.png", "status": "pending", "error": "检测失败: x", "regions": []}
    page = Page.from_dict(old_dict)
    assert page.error_stage == ""


def test_region_score_default_and_roundtrip():
    """score 默认 0.0；序列化往返保留精确值。"""
    r = Region(bbox=(0, 0, 10, 10))
    assert r.score == 0.0

    r2 = Region(bbox=(0, 0, 10, 10), score=0.873)
    restored = Region.from_dict(r2.to_dict())
    assert restored.score == 0.873


def test_region_from_dict_without_score_defaults_to_zero():
    """读旧版（无 score 字段）的 pages.json 不应报错，score 回落默认值。"""
    old_dict = {
        "bbox": (1, 2, 3, 4),
        "direction": "horizontal",
        "source_text": "a",
        "translated_text": "b",
        "color": (0, 0, 0),
        "kind": "text_free",
    }
    r = Region.from_dict(old_dict)
    assert r.score == 0.0
    assert r.source_text == "a"


def test_status_constants():
    """Test that status constants are defined."""
    from comic_gen.core.models import STATUS_DETECTED, STATUS_OCRED, STATUS_TRANSLATED
    assert STATUS_PENDING == "pending"
    assert STATUS_DETECTED == "detected"
    assert STATUS_OCRED == "ocred"
    assert STATUS_TRANSLATED == "translated"
    assert STATUS_RENDERED == "rendered"
