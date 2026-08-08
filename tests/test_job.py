"""core.job 的离线测试：JobReport 的结果分类，之前这个模块完全没有测试覆盖，
却是"用户被告知运行结果是什么"的唯一决策点。
"""
from comic_gen.core.job import JobReport, PageFailure, _legacy_stage_from_message
from comic_gen.core.models import Page, STATUS_RENDERED


def _rendered_page(image: str) -> Page:
    return Page(image=image, status=STATUS_RENDERED, error="")


def _failed_page(image: str, message: str, stage: str = "") -> Page:
    return Page(image=image, status="pending", error=message, error_stage=stage)


def test_from_pages_all_success_is_ok():
    pages = [_rendered_page("a.png"), _rendered_page("b.png")]
    report = JobReport.from_pages(pages, output_path=None)
    assert report.status == "ok"
    assert report.total_pages == 2
    assert report.failed_pages == []
    assert "2" in report.summary


def test_from_pages_partial_uses_error_stage_directly():
    pages = [
        _rendered_page("a.png"),
        _failed_page("b.png", "OCR 失败: 模型未加载", stage="ocr"),
    ]
    report = JobReport.from_pages(pages, output_path=None)
    assert report.status == "partial"
    assert len(report.failed_pages) == 1
    assert report.failed_pages[0].stage == "ocr"
    assert report.failed_pages[0].image == "b.png"
    assert report.failed_pages[0].message == "OCR 失败: 模型未加载"


def test_from_pages_all_failed_is_failed():
    pages = [
        _failed_page("a.png", "检测失败: x", stage="detect"),
        _failed_page("b.png", "检测失败: y", stage="detect"),
    ]
    report = JobReport.from_pages(pages, output_path=None)
    assert report.status == "failed"
    assert len(report.failed_pages) == 2


def test_from_pages_falls_back_to_legacy_stage_parsing_without_error_stage():
    """回归测试：读旧版 pages.json（没有 error_stage 字段，Page 默认给空串）时，
    仍要能从消息里切出一个可读的阶段标签，不是空字符串。
    """
    p = Page(image="a.png", status="pending", error="翻译失败: 超时")
    assert p.error_stage == ""  # 默认值，模拟旧数据
    report = JobReport.from_pages([p], output_path=None)
    assert report.failed_pages[0].stage == "翻译失败"


def test_legacy_stage_from_message_with_colon():
    assert _legacy_stage_from_message("检测失败: 模型未加载") == "检测失败"


def test_legacy_stage_from_message_without_colon():
    assert _legacy_stage_from_message("未知错误") == "未知阶段"


def test_from_pages_empty_list():
    report = JobReport.from_pages([], output_path=None)
    assert report.status == "ok"
    assert report.total_pages == 0


def test_cancelled_report_shape():
    report = JobReport.cancelled([_rendered_page("a.png"), _rendered_page("b.png")])
    assert report.status == "cancelled"
    assert report.total_pages == 2
    assert report.failed_pages == []
    assert "已停止" in report.summary


def test_cancelled_report_counts_only_rendered_pages_as_completed():
    pages = [
        _rendered_page("a.png"),
        Page(image="b.png", status="translated", error=""),
        Page(image="c.png", status="pending", error=""),
    ]
    report = JobReport.cancelled(pages)
    assert "1/3" in report.summary


def test_cancelled_report_carries_output_path_for_ui_prefill():
    from pathlib import Path

    report = JobReport.cancelled([_rendered_page("a.png")], Path("/tmp/out"))
    assert report.output_path == Path("/tmp/out")


def test_page_failure_stage_matches_core_errors_stage_values():
    """PageFailure.stage 的值域应该和 core.errors.Stage 一致。"""
    from comic_gen.core.errors import Stage

    pf = PageFailure(image="a.png", stage=Stage.RENDER.value, message="x")
    assert pf.stage == "render"


def test_format_failure_report_lists_every_failure_with_stage():
    """失败报告必须逐条列出所有失败页（含阶段），不再截断到前 20 条。"""
    from comic_gen.core.job import format_failure_report

    report = JobReport.from_pages(
        [
            _failed_page("p01.png", "OCR 失败: 超时", stage="ocr"),
            _failed_page("p02.png", "翻译失败: 模型未加载", stage="translate"),
        ],
        output_path=None,
    )
    text = format_failure_report(report)
    assert "2 页失败" in text
    assert "[ocr] p01.png：OCR 失败: 超时" in text
    assert "[translate] p02.png：翻译失败: 模型未加载" in text


def test_format_failure_report_ends_with_resume_hint_when_failures_exist():
    """部分失败时报告末尾要带恢复提示——用户需要知道"下一步该做什么"（重新运行即
    可从断点续跑，不重做已完成页）。"""
    from comic_gen.core.job import format_failure_report

    report = JobReport.from_pages([_failed_page("p01.png", "渲染失败: x", stage="render")], output_path=None)
    text = format_failure_report(report)
    assert "重新运行即可从断点继续" in text


def test_format_failure_report_without_failures():
    from comic_gen.core.job import format_failure_report

    report = JobReport.from_pages([_rendered_page("a.png")], output_path=None)
    text = format_failure_report(report)
    assert "0 页失败" in text
    assert "重新运行即可从断点继续" not in text  # 没有失败就不该出现恢复提示
