"""Tests for pipeline.runner safety checks (offline)."""
import json
import pytest
from pathlib import Path
import tempfile
from PIL import Image
from comic_gen.core.errors import OutputError
from comic_gen.core.models import Region
from comic_gen.pipeline.runner import Runner, select_drawable_regions
from comic_gen.core.config import Config


def test_check_output_safe_same_path():
    """Test that output == input is rejected."""
    cfg = Config()
    runner = Runner(cfg)

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / "input.zip"
        output_path = Path(tmpdir) / "input.zip"

        with pytest.raises(OutputError, match="覆盖原图"):
            runner._check_output_safe(input_path, output_path)


def test_check_output_safe_output_in_input():
    """Test that output inside input directory is rejected."""
    cfg = Config()
    runner = Runner(cfg)

    with tempfile.TemporaryDirectory() as tmpdir:
        input_dir = Path(tmpdir) / "input"
        input_dir.mkdir()
        output_path = input_dir / "output.zip"

        with pytest.raises(OutputError, match="输出目录位于输入目录内"):
            runner._check_output_safe(input_dir, output_path)


def test_check_output_safe_valid_paths():
    """Test that valid paths pass the check."""
    cfg = Config()
    runner = Runner(cfg)

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / "input.zip"
        output_path = Path(tmpdir) / "output.zip"

        # Should not raise
        runner._check_output_safe(input_path, output_path)


def test_check_output_safe_different_dirs():
    """Test that output in different directory is safe."""
    cfg = Config()
    runner = Runner(cfg)

    with tempfile.TemporaryDirectory() as tmpdir:
        input_dir = Path(tmpdir) / "input_folder"
        output_dir = Path(tmpdir) / "output_folder"
        input_dir.mkdir()
        output_dir.mkdir()

        # Should not raise
        runner._check_output_safe(input_dir, output_dir)


def test_check_output_safe_fails_closed_on_resolve_error():
    """0.5 回归测试：路径解析失败必须拒绝，不能"测不出来就当安全放行"。

    旧实现是 `except Exception: return`——异常路径反而是这道防线最该生效、
    却恰好失效的地方。用一个 resolve() 必炸的假路径对象验证现在会拒绝。
    """

    class BoomPath:
        def resolve(self):
            raise OSError("模拟路径解析失败")

    with pytest.raises(OutputError, match="无法校验输出路径是否安全"):
        Runner._check_output_safe(BoomPath(), Path("/tmp/whatever"))


def test_run_rejects_conflicting_output_before_any_extraction(tmp_path, monkeypatch):
    """0.5 回归测试：输出路径与输入冲突时，必须在碰任何文件之前就拒绝。

    旧实现是在 load_input()（此时已经清空/重新解包了 _extract 目录）之后才检查——
    用一个"只要被调用就报错"的假 load_input 验证它现在从未被触发到。
    """
    import comic_gen.pipeline.runner as runner_mod

    def must_not_be_called(*a, **k):
        raise AssertionError("load_input 不应该被调用：安全检查应该在此之前就失败")

    monkeypatch.setattr(runner_mod, "load_input", must_not_be_called)

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "a.png").touch()

    runner = Runner(Config())
    with pytest.raises(OutputError, match="覆盖原图"):
        runner.run(str(input_dir), str(input_dir))  # 输出 == 输入


def test_select_drawable_regions_keeps_only_translated():
    """有原文有译文 -> 抹字+重绘；有原文无译文 -> 原样保留，不进 drawable。"""
    regions = [
        Region(bbox=[0, 0, 10, 10], source_text="原文A", translated_text="译文A"),
        Region(bbox=[10, 10, 10, 10], source_text="原文B", translated_text=""),  # 漏译
        Region(bbox=[20, 20, 10, 10], source_text="", translated_text=""),  # 无文字区域
    ]
    drawable = select_drawable_regions(regions)
    assert len(drawable) == 1
    assert drawable[0].source_text == "原文A"


def test_select_drawable_regions_empty_translated_never_erased():
    """回归测试：这是 R2 的红绿用例——之前的实现会把这种区域送去抹字。"""
    regions = [Region(bbox=[0, 0, 10, 10], source_text="残留原文", translated_text="")]
    assert select_drawable_regions(regions) == []


def test_select_drawable_regions_all_translated():
    regions = [
        Region(bbox=[0, 0, 10, 10], source_text="A", translated_text="a"),
        Region(bbox=[10, 10, 10, 10], source_text="B", translated_text="b"),
    ]
    assert len(select_drawable_regions(regions)) == 2


def test_select_drawable_regions_whitespace_only_treated_as_empty():
    regions = [Region(bbox=[0, 0, 10, 10], source_text="原文", translated_text="   ")]
    assert select_drawable_regions(regions) == []


# ---------------------------------------------------------------- _get_or_rebuild


def test_get_or_rebuild_reuses_when_signature_unchanged():
    runner = Runner(Config())
    calls = []

    def factory():
        calls.append(1)
        return object()

    a = runner._get_or_rebuild("_detector", "_detector_sig", "sig-a", factory)
    b = runner._get_or_rebuild("_detector", "_detector_sig", "sig-a", factory)
    assert a is b
    assert len(calls) == 1


def test_get_or_rebuild_rebuilds_when_signature_changes():
    """这是 0.1 的核心机制：签名变了必须重建，不能只判 is None。"""
    runner = Runner(Config())
    calls = []

    def factory():
        calls.append(1)
        return object()

    a = runner._get_or_rebuild("_detector", "_detector_sig", "sig-a", factory)
    b = runner._get_or_rebuild("_detector", "_detector_sig", "sig-b", factory)
    assert a is not b
    assert len(calls) == 2


def test_get_ocr_rebuilds_when_source_lang_changes(monkeypatch):
    """实测复现的真 bug：源语言从日文切到英文再跑一次，旧实现返回的还是按日文构造
    的 manga-ocr 实例（只判 is None，从不检查 cfg 是否变了）。用假 OcrEngine 验证
    _get_ocr 现在会按 source_lang 重建，不需要真的加载 manga-ocr/easyocr。
    """
    import comic_gen.pipeline.ocr as ocr_mod

    created_with = []

    class FakeOcrEngine:
        def __init__(self, cfg):
            created_with.append(cfg.source_lang)

    monkeypatch.setattr(ocr_mod, "OcrEngine", FakeOcrEngine)

    runner = Runner(Config(source_lang="ja"))
    ocr_ja = runner._get_ocr()
    assert created_with == ["ja"]

    runner.cfg = Config(source_lang="en")
    ocr_en = runner._get_ocr()
    assert created_with == ["ja", "en"]
    assert ocr_en is not ocr_ja

    # 语言不变时不重建
    runner._get_ocr()
    assert created_with == ["ja", "en"]


def test_get_detector_rebuilds_when_threshold_changes(monkeypatch):
    import comic_gen.pipeline.detect as detect_mod

    created_with = []

    class FakeDetector:
        def __init__(self, repo, device, threshold):
            created_with.append(threshold)

    monkeypatch.setattr(detect_mod, "ComicDetector", FakeDetector)

    runner = Runner(Config(det_threshold=0.3))
    d1 = runner._get_detector()
    assert created_with == [0.3]

    runner.cfg = Config(det_threshold=0.5)
    d2 = runner._get_detector()
    assert created_with == [0.3, 0.5]
    assert d2 is not d1


def test_get_inpainter_rebuilds_when_model_binding_changes(monkeypatch):
    import comic_gen.pipeline.inpaint as inpaint_mod

    created_with = []

    class FakeInpainter:
        def __init__(self, device, model_path=None):
            created_with.append(model_path)

    monkeypatch.setattr(inpaint_mod, "Inpainter", FakeInpainter)

    runner = Runner(Config(model_paths={}))
    i1 = runner._get_inpainter()
    assert created_with == [None]

    runner.cfg = Config(model_paths={"lama": "F:/models/big-lama.pt"})
    i2 = runner._get_inpainter()
    assert created_with == [None, "F:/models/big-lama.pt"]
    assert i2 is not i1


# ---------------------------------------------------------------- run() 编排（用假引擎，不加载真模型）


def _make_fake_pipeline(monkeypatch):
    """把 detect/ocr/inpaint/render 四个阶段换成不碰真模型的假实现，只留 run() 的编排逻辑。"""
    import comic_gen.pipeline.detect as detect_mod
    import comic_gen.pipeline.inpaint as inpaint_mod
    import comic_gen.pipeline.ocr as ocr_mod
    import comic_gen.pipeline.render as render_mod

    class FakeDetector:
        def __init__(self, *a, **k):
            pass

        def detect(self, image):
            return [Region(bbox=[0, 0, 10, 10], direction="horizontal")]

    class FakeOcr:
        def __init__(self, *a, **k):
            pass

        def recognize_regions(self, image, regions):
            for r in regions:
                r.source_text = "テスト"

    class FakeInpainter:
        def __init__(self, *a, **k):
            pass

        def inpaint_page(self, image, regions):
            return image.copy()

    def fake_render_page(image, regions, cfg):
        return image.copy()

    monkeypatch.setattr(detect_mod, "ComicDetector", FakeDetector)
    monkeypatch.setattr(ocr_mod, "OcrEngine", FakeOcr)
    monkeypatch.setattr(inpaint_mod, "Inpainter", FakeInpainter)
    monkeypatch.setattr(render_mod, "render_page", fake_render_page)


def _make_input_folder(tmp_path, n=3):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for i in range(n):
        Image.new("RGB", (20, 20), "white").save(input_dir / f"page_{i}.png")
    return input_dir


def test_run_saves_translate_progress_incrementally(tmp_path, monkeypatch):
    """0.2 回归测试：翻译阶段中途被"外部强杀"打断，已翻完的页必须已经落盘。

    旧实现的 _save_pages 在翻译循环外，只在整批翻完后存一次——循环中途异常退出会
    丢失已经跑完的那些页的译文。用 KeyboardInterrupt（不会被阶段内的
    `except Exception` 捕获，模拟强制中断/崩溃）让 run() 在处理第二页时直接中止，
    再检查落盘的 pages.json 是否已经记录了第一页的翻译结果。
    """
    _make_fake_pipeline(monkeypatch)
    input_dir = _make_input_folder(tmp_path, n=3)
    output_dir = tmp_path / "output"

    class CrashOnSecondPageTranslator:
        name = "fake"

        def __init__(self):
            self.calls = 0

        def translate(self, texts, cfg):
            self.calls += 1
            if self.calls == 2:
                raise KeyboardInterrupt("模拟强制中断")
            return [f"译:{t}" for t in texts]

    cfg = Config(translator="mock", source_lang="ja")
    runner = Runner(cfg)
    runner._get_translator = lambda: CrashOnSecondPageTranslator()

    with pytest.raises(KeyboardInterrupt):
        runner.run(str(input_dir), str(output_dir))

    pages_data = json.loads((output_dir / "pages.json").read_text(encoding="utf-8"))
    statuses = [p["status"] for p in pages_data["pages"]]
    # 第一页已经翻译成功并落盘，尽管第二页把整个 run() 打断了
    assert "translated" in statuses


def test_run_clears_page_error_on_later_success(tmp_path, monkeypatch):
    """0.3 回归测试：某一页第一次跑检测失败，第二次重跑成功后，error 字段必须清空，
    否则 JobReport 会把这个已经修好的页永远算作失败（旧 bug：p.error 从不重置）。
    """
    _make_fake_pipeline(monkeypatch)
    input_dir = _make_input_folder(tmp_path, n=1)
    output_dir = tmp_path / "output"

    import comic_gen.pipeline.detect as detect_mod

    call_count = {"n": 0}

    class FlakyDetector:
        def __init__(self, *a, **k):
            pass

        def detect(self, image):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("第一次检测失败")
            return [Region(bbox=[0, 0, 10, 10], direction="horizontal")]

    monkeypatch.setattr(detect_mod, "ComicDetector", FlakyDetector)

    cfg = Config(translator="mock", source_lang="ja")
    runner = Runner(cfg)

    # 第一次跑：检测失败，pages.json 记录 error
    runner.run(str(input_dir), str(output_dir))
    pages_data = json.loads((output_dir / "pages.json").read_text(encoding="utf-8"))
    assert pages_data["pages"][0]["error"] != ""

    # 第二次跑（同一个 work_dir，检测这次成功）：error 必须被清空
    runner.run(str(input_dir), str(output_dir))
    pages_data = json.loads((output_dir / "pages.json").read_text(encoding="utf-8"))
    assert pages_data["pages"][0]["error"] == ""
    assert pages_data["pages"][0]["status"] in ("translated", "rendered")


# ---------------------------------------------------------------- infra.logging 桥接


def test_run_bridges_leaf_module_logging_to_self_log_and_run_log_file(tmp_path, monkeypatch):
    """2.2 回归测试：叶子模块（ocr/translate/render）用 logging.getLogger(__name__)
    记的消息，之前各自 print(file=sys.stderr)，到不了 UI 日志面板也没有落盘。
    这里模拟一个"叶子模块"在 OCR 阶段调用 comic_gen 这棵 logger，验证 run() 期间
    这条记录会被转发到 self.log（UI 面板走这个）、也会写进 <work_dir>/run.log。
    """
    import comic_gen.pipeline.detect as detect_mod
    import comic_gen.pipeline.inpaint as inpaint_mod
    import comic_gen.pipeline.ocr as ocr_mod
    import comic_gen.pipeline.render as render_mod
    from comic_gen.infra.logging import logger as pipeline_logger

    class FakeDetector:
        def __init__(self, *a, **k):
            pass

        def detect(self, image):
            return [Region(bbox=[0, 0, 10, 10], direction="horizontal")]

    class FakeOcrWithLogging:
        def __init__(self, *a, **k):
            pass

        def recognize_regions(self, image, regions):
            pipeline_logger.warning("模拟叶子模块警告：装饰性花纹被识别成文字")
            for r in regions:
                r.source_text = "テスト"

    class FakeInpainter:
        def __init__(self, *a, **k):
            pass

        def inpaint_page(self, image, regions):
            return image.copy()

    def fake_render_page(image, regions, cfg):
        return image.copy()

    monkeypatch.setattr(detect_mod, "ComicDetector", FakeDetector)
    monkeypatch.setattr(ocr_mod, "OcrEngine", FakeOcrWithLogging)
    monkeypatch.setattr(inpaint_mod, "Inpainter", FakeInpainter)
    monkeypatch.setattr(render_mod, "render_page", fake_render_page)

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    Image.new("RGB", (20, 20), "white").save(input_dir / "page_0.png")
    output_dir = tmp_path / "output"

    collected_logs = []
    cfg = Config(translator="mock", source_lang="ja")
    runner = Runner(cfg, log=collected_logs.append)
    runner.run(str(input_dir), str(output_dir))

    assert any("装饰性花纹" in m for m in collected_logs)

    run_log_content = (output_dir / "run.log").read_text(encoding="utf-8")
    assert "装饰性花纹" in run_log_content
    assert "[WARNING]" in run_log_content


def test_run_detaches_logging_handlers_after_completion(tmp_path, monkeypatch):
    """跑完之后不应该在 comic_gen 这棵 logger 上留下 handler——不然构造/跑很多个
    Runner（测试套件里就是这样）会不断堆积，同一条日志被转发好几遍。
    """
    from comic_gen.infra.logging import logger as pipeline_logger

    _make_fake_pipeline(monkeypatch)
    input_dir = _make_input_folder(tmp_path, n=1)
    output_dir = tmp_path / "output"

    before = len(pipeline_logger.handlers)
    cfg = Config(translator="mock", source_lang="ja")
    Runner(cfg).run(str(input_dir), str(output_dir))
    assert len(pipeline_logger.handlers) == before
