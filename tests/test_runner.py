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


# ---------------------------------------------------------------- 引擎缓存失效


def test_needed_model_keys_delegates_to_registry():
    """2.4 回归测试：Runner._needed_model_keys 应该和 registry.needed_model_keys
    产出一致——包括之前的真 bug（非日文任务要包含 ocr_other）。
    """
    from comic_gen.pipeline.registry import needed_model_keys

    for translator, lang in [("sakura", "ja"), ("llm", "en"), ("mock", "zh")]:
        cfg = Config(translator=translator, source_lang=lang)
        runner = Runner(cfg)
        assert set(runner._needed_model_keys()) == set(needed_model_keys(cfg))
    # 明确锁住这个历史 bug 不再回归
    assert "ocr_other" in Runner(Config(translator="llm", source_lang="en"))._needed_model_keys()


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


def test_get_ocr_rebuilds_when_device_changes(monkeypatch):
    """4.3 回归测试：OCR 后端现在认 cfg.device 了（registry.py 的 _wants_gpu），
    缓存签名不带上这一项会出现同一类"改了设置不生效"的陈旧缓存 bug。
    """
    import comic_gen.pipeline.ocr as ocr_mod

    created_with = []

    class FakeOcrEngine:
        def __init__(self, cfg):
            created_with.append(cfg.device)

    monkeypatch.setattr(ocr_mod, "OcrEngine", FakeOcrEngine)

    runner = Runner(Config(source_lang="ja", device="cuda"))
    ocr_gpu = runner._get_ocr()
    assert created_with == ["cuda"]

    runner.cfg = Config(source_lang="ja", device="cpu")
    ocr_cpu = runner._get_ocr()
    assert created_with == ["cuda", "cpu"]
    assert ocr_cpu is not ocr_gpu


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


def test_get_translator_rebuilds_when_target_lang_changes(monkeypatch):
    """2.5 回归测试：实测复现的真 bug——旧的缓存签名只有 cfg.translator 一项。
    GeneralLLMTranslator 在构造时把 source_lang/target_lang 烤进 system_prompt，
    只换 target_lang（引擎名字不变）时旧签名判断不出该重建，会一直用旧语言对的
    system_prompt 推理。
    """
    import comic_gen.pipeline.registry as registry_mod

    created_with = []

    class FakeTranslator:
        name = "llm"

        def __init__(self, cfg):
            created_with.append((cfg.source_lang, cfg.target_lang))

    monkeypatch.setattr(
        registry_mod,
        "TRANSLATOR_REGISTRY",
        {"llm": registry_mod.TranslatorEntry("llm", FakeTranslator)},
    )

    runner = Runner(Config(translator="llm", source_lang="en", target_lang="简体中文"))
    t1 = runner._get_translator()
    assert created_with == [("en", "简体中文")]

    runner.cfg = Config(translator="llm", source_lang="en", target_lang="繁體中文")
    t2 = runner._get_translator()
    assert created_with == [("en", "简体中文"), ("en", "繁體中文")]
    assert t2 is not t1


def test_get_translator_reuses_when_nothing_relevant_changed(monkeypatch):
    import comic_gen.pipeline.registry as registry_mod

    created_with = []

    class FakeTranslator:
        name = "llm"

        def __init__(self, cfg):
            created_with.append(1)

    monkeypatch.setattr(
        registry_mod,
        "TRANSLATOR_REGISTRY",
        {"llm": registry_mod.TranslatorEntry("llm", FakeTranslator)},
    )

    runner = Runner(Config(translator="llm", source_lang="en", target_lang="简体中文"))
    t1 = runner._get_translator()
    t2 = runner._get_translator()
    assert t1 is t2
    assert len(created_with) == 1


def test_get_translator_rebuilds_when_device_changes(monkeypatch):
    """4.3 回归测试：GGUF 引擎的 n_gpu_layers 现在认 cfg.device 了，缓存签名不带上
    这一项，切 CPU/GPU 不会重建、白改设置——和上面 target_lang 那条是同一类 bug。
    """
    import comic_gen.pipeline.registry as registry_mod

    created_with = []

    class FakeTranslator:
        name = "llm"

        def __init__(self, cfg):
            created_with.append(cfg.device)

    monkeypatch.setattr(
        registry_mod,
        "TRANSLATOR_REGISTRY",
        {"llm": registry_mod.TranslatorEntry("llm", FakeTranslator)},
    )

    runner = Runner(Config(translator="llm", source_lang="en", device="cuda"))
    t1 = runner._get_translator()
    assert created_with == ["cuda"]

    runner.cfg = Config(translator="llm", source_lang="en", device="cpu")
    t2 = runner._get_translator()
    assert created_with == ["cuda", "cpu"]
    assert t2 is not t1


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


# ---------------------------------------------------------------- job.json 快照


def test_run_writes_job_snapshot_with_stage_timings(tmp_path, monkeypatch):
    """2.3：job.json 要记录完整的 config 快照、app 版本号，以及每个实际跑过的阶段
    的耗时——之前 pages.json 只记 input 一个字段，出问题后不知道当时用的是什么配置。
    """
    _make_fake_pipeline(monkeypatch)
    input_dir = _make_input_folder(tmp_path, n=2)
    output_dir = tmp_path / "output"

    cfg = Config(translator="mock", source_lang="ja", det_threshold=0.42)
    Runner(cfg).run(str(input_dir), str(output_dir))

    job_data = json.loads((output_dir / "job.json").read_text(encoding="utf-8"))
    assert job_data["config"]["det_threshold"] == 0.42
    assert job_data["config"]["translator"] == "mock"
    assert job_data["app_version"]
    assert set(job_data["stages"].keys()) == {"detect", "ocr", "translate", "render"}
    for stage_info in job_data["stages"].values():
        assert stage_info["page_count"] == 2
        assert stage_info["duration_s"] >= 0
    assert isinstance(job_data["resolved_model_paths"], dict)


def test_run_writes_job_snapshot_even_when_every_page_fails(tmp_path, monkeypatch):
    """每一页在检测阶段都失败（per-page try/except 兜底，不会让 run() 整个抛出），
    job.json 仍然要落盘，且能看到 detect 阶段确实跑完了（page_count 反映的是
    "跑过几页"，不是"成功几页"——成功与否是 pages.json/JobReport 的职责）。
    """
    import comic_gen.pipeline.detect as detect_mod

    class BoomDetector:
        def __init__(self, *a, **k):
            pass

        def detect(self, image):
            raise RuntimeError("模拟检测崩溃")

    monkeypatch.setattr(detect_mod, "ComicDetector", BoomDetector)

    input_dir = _make_input_folder(tmp_path, n=1)
    output_dir = tmp_path / "output"
    cfg = Config(translator="mock", source_lang="ja")
    Runner(cfg).run(str(input_dir), str(output_dir))  # 检测失败但不会抛出（per-page 兜底）

    job_data = json.loads((output_dir / "job.json").read_text(encoding="utf-8"))
    assert job_data["config"]["translator"] == "mock"
    assert job_data["stages"]["detect"]["page_count"] == 1
    # 后续阶段（OCR/翻译/渲染）因为没有页面进入 STATUS_DETECTED，todo 为空，不记录耗时
    assert "ocr" not in job_data["stages"]


def test_run_cancelled_report_carries_resolved_output_path(tmp_path, monkeypatch):
    """3.2 回归测试：取消发生在检测阶段第一页之前，此时 output_path 早已解析好
    （在任何阶段循环之前），JobReport.cancelled 必须把它带出来——UI 用它回填输出框，
    防止用户下次留空/改填导致 work_dir 解析到别处、整卷从零重来。
    之前 4 处取消返回点都是裸 `JobReport.cancelled(pages)`，output_path 永远是 None。
    """
    _make_fake_pipeline(monkeypatch)
    input_dir = _make_input_folder(tmp_path, n=3)
    output_dir = tmp_path / "output"

    cfg = Config(translator="mock", source_lang="ja")
    report = Runner(cfg).run(str(input_dir), str(output_dir), should_cancel=lambda: True)

    assert report.status == "cancelled"
    assert report.output_path == output_dir
    assert "0/3" in report.summary
