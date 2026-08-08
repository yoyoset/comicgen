"""app.review_session 的离线测试：直接构造 pages.json + 原图，不跑完整流水线——
校对会话本身不关心页面是怎么走到这个状态的，只关心怎么读、怎么改、怎么单页重渲染。
render_page 是真实函数（不需要 GPU，字体多级兜底保证离线可跑）；Inpainter 换成假实现，
和 test_runner.py 的 _make_fake_pipeline 同一个理由——LaMa 是真实 GPU 模型，测试不该碰它。
"""
import json

import pytest
from PIL import Image

from comic_gen.app.review_session import ReviewSession, load_config_for_review
from comic_gen.core.config import Config
from comic_gen.core.errors import InputError
from comic_gen.core.models import STATUS_RENDERED, STATUS_TRANSLATED, Page, Region


def _make_work_dir(tmp_path, pages: list[Page]):
    """造一个"已经跑完一次翻译"的工作目录：input/ 放原图，pages.json 记录状态。
    文件夹输入时 work_dir 就是输出目录本身（和 Runner._work_dir 的规则一致）。
    """
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    work_dir = tmp_path / "output"
    work_dir.mkdir()
    for p in pages:
        Image.new("RGB", (100, 150), "white").save(input_dir / p.image)

    data = {"pages": [p.to_dict() for p in pages], "input": str(input_dir.resolve())}
    (work_dir / "pages.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return work_dir, input_dir


def _sample_pages() -> list[Page]:
    return [
        Page(
            image="page_0.png", width=100, height=150, status=STATUS_RENDERED,
            regions=[
                Region(bbox=[0, 0, 10, 10], source_text="こんにちは", translated_text="你好", score=0.9),
                Region(bbox=[20, 20, 10, 10], source_text="", translated_text="", score=0.8),  # OCR 空
                Region(bbox=[40, 40, 10, 10], source_text="さようなら", translated_text="", score=0.2),  # 低分+缺译文（低分优先命中）
            ],
        ),
        Page(image="page_1.png", width=100, height=150, status=STATUS_TRANSLATED, error="", regions=[]),
    ]


def test_missing_pages_json_raises_input_error(tmp_path):
    with pytest.raises(InputError, match="pages.json"):
        ReviewSession(tmp_path, Config())


def test_missing_original_input_record_raises_input_error(tmp_path):
    (tmp_path / "pages.json").write_text('{"pages": []}', encoding="utf-8")
    with pytest.raises(InputError, match="原始输入"):
        ReviewSession(tmp_path, Config())


def test_page_images_and_get_page(tmp_path):
    work_dir, _ = _make_work_dir(tmp_path, _sample_pages())
    session = ReviewSession(work_dir, Config())
    assert session.page_images() == ["page_0.png", "page_1.png"]
    assert session.get_page("page_0.png").regions[0].translated_text == "你好"


def test_original_input_property_exposes_input_fingerprint(tmp_path):
    """「导出全部」需要原始输入路径来定位原图/推导输出路径——会话要把它暴露出来。"""
    work_dir, input_dir = _make_work_dir(tmp_path, _sample_pages())
    session = ReviewSession(work_dir, Config())
    assert session.original_input == str(input_dir.resolve())


def test_attention_items_flags_low_score_empty_ocr_and_missing_translation(tmp_path):
    work_dir, _ = _make_work_dir(tmp_path, _sample_pages())
    session = ReviewSession(work_dir, Config())
    items = session.attention_items()
    reasons = {(i.page_image, i.region_index): i.reason for i in items}

    assert (reasons[("page_0.png", 1)]).startswith("OCR")
    assert "置信度低" in reasons[("page_0.png", 2)]  # 低分和缺译文同时命中，低分判定在前
    assert ("page_0.png", 0) not in reasons  # 正常区域不出现


def test_preview_path_falls_back_to_original_when_not_yet_rendered(tmp_path):
    work_dir, input_dir = _make_work_dir(tmp_path, _sample_pages())
    session = ReviewSession(work_dir, Config())
    assert session.preview_path("page_1.png") == input_dir / "page_1.png"


def test_preview_path_prefers_rendered_output(tmp_path):
    work_dir, _ = _make_work_dir(tmp_path, _sample_pages())
    trans_dir = work_dir / "translated"
    trans_dir.mkdir()
    Image.new("RGB", (10, 10)).save(trans_dir / "page_0.png")
    session = ReviewSession(work_dir, Config())
    assert session.preview_path("page_0.png") == trans_dir / "page_0.png"


def test_update_region_text_resets_rendered_page_back_to_translated(tmp_path):
    """改完译文后这一页要在下次导出时被重新渲染，不能停在 rendered 被跳过。"""
    work_dir, _ = _make_work_dir(tmp_path, _sample_pages())
    session = ReviewSession(work_dir, Config())
    assert session.get_page("page_0.png").status == STATUS_RENDERED

    session.update_region_text("page_0.png", 2, "再见")

    page = session.get_page("page_0.png")
    assert page.regions[2].translated_text == "再见"
    assert page.status == STATUS_TRANSLATED


def test_save_persists_edits_to_pages_json(tmp_path):
    work_dir, _ = _make_work_dir(tmp_path, _sample_pages())
    session = ReviewSession(work_dir, Config())
    session.update_region_text("page_0.png", 0, "改过的译文")
    session.save()

    reloaded = ReviewSession(work_dir, Config())
    assert reloaded.get_page("page_0.png").regions[0].translated_text == "改过的译文"


def test_rerender_page_writes_output_and_marks_rendered(tmp_path, monkeypatch):
    import comic_gen.pipeline.inpaint as inpaint_mod

    class FakeInpainter:
        def __init__(self, *a, **k):
            pass

        def inpaint_page(self, image, regions):
            return image.copy()

    monkeypatch.setattr(inpaint_mod, "Inpainter", FakeInpainter)

    pages = _sample_pages()
    pages[1].status = STATUS_TRANSLATED  # 没有区域但状态是 translated，允许重渲染跑通
    work_dir, _ = _make_work_dir(tmp_path, pages)
    session = ReviewSession(work_dir, Config())

    out_path = session.rerender_page("page_1.png")

    assert out_path == work_dir / "translated" / "page_1.png"
    assert out_path.exists()
    assert session.get_page("page_1.png").status == STATUS_RENDERED
    # rerender_page 内部调用了 save()，重新读一遍磁盘上的 pages.json 应该也看到新状态
    reloaded = ReviewSession(work_dir, Config())
    assert reloaded.get_page("page_1.png").status == STATUS_RENDERED


def test_load_config_for_review_prefers_job_json_snapshot(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "job.json").write_text(
        json.dumps({"config": {"translator": "llm", "det_threshold": 0.77}}), encoding="utf-8"
    )
    cfg = load_config_for_review(work_dir, fallback=Config(translator="mock"))
    assert cfg.translator == "llm"
    assert cfg.det_threshold == 0.77


def test_load_config_for_review_falls_back_without_job_json(tmp_path):
    fallback = Config(translator="mock")
    cfg = load_config_for_review(tmp_path / "no_such_dir", fallback=fallback)
    assert cfg is fallback


def test_load_config_for_review_falls_back_on_corrupt_job_json(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "job.json").write_text("not json", encoding="utf-8")
    fallback = Config(translator="mock")
    cfg = load_config_for_review(work_dir, fallback=fallback)
    assert cfg is fallback


def test_rerender_page_reuses_cached_inpainter_across_calls(tmp_path, monkeypatch):
    """引擎缓存：同一个 session 里多次重渲染不该每次都重建 Inpainter（GPU 模型加载
    是真实成本），和 Runner._get_inpainter 用的是同一套 EngineCache 机制。
    """
    import comic_gen.pipeline.inpaint as inpaint_mod

    build_count = 0

    class FakeInpainter:
        def __init__(self, *a, **k):
            nonlocal build_count
            build_count += 1

        def inpaint_page(self, image, regions):
            return image.copy()

    monkeypatch.setattr(inpaint_mod, "Inpainter", FakeInpainter)

    pages = [Page(image="page_0.png", width=10, height=10, status=STATUS_TRANSLATED, regions=[])]
    work_dir, _ = _make_work_dir(tmp_path, pages)
    session = ReviewSession(work_dir, Config())

    session.rerender_page("page_0.png")
    session.get_page("page_0.png").status = STATUS_TRANSLATED  # 模拟又改了一次
    session.rerender_page("page_0.png")

    assert build_count == 1
