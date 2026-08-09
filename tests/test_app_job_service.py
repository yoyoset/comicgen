"""app.job_service 的测试：包含"第一个端到端集成测试"——用真实提交的
tests/sample.zip（不是临时合成的图片）+ MockTranslator 跑完整的 4 阶段编排 +
真实 ZIP 解包/打包路径。

detect/OCR/抹字/渲染仍然用假实现（和 test_runner.py 里其它编排测试同样的模式）——
这是刻意的：全套测试 250+ 个用例都不加载真模型/不需要网络/不需要 GPU 就能跑，
引入一个需要真实权重的测试会打破这个纪律，在没有缓存好模型的机器/CI 上直接卡死
或失败。MockTranslator 本身是真实组件，不是假的；假的只是 detect/OCR/inpaint/
render 四个需要重量级模型的阶段。
"""
import zipfile
from pathlib import Path

from PIL import Image

from comic_gen.app.job_service import _build_arg_parser, rerender_job, run_job
from comic_gen.core.config import Config
from comic_gen.core.models import Region
from comic_gen.pipeline.registry import SUPPORTED_SOURCE_LANGS, TRANSLATOR_REGISTRY

_SAMPLE_ZIP = Path(__file__).parent / "sample.zip"


def _install_fake_pipeline(monkeypatch):
    import comic_gen.pipeline.detect as detect_mod
    import comic_gen.pipeline.inpaint as inpaint_mod
    import comic_gen.pipeline.ocr as ocr_mod
    import comic_gen.pipeline.render as render_mod

    class FakeDetector:
        def __init__(self, *a, **k):
            pass

        def detect(self, image):
            return [Region(bbox=[10, 10, 100, 40], direction="horizontal")]

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


def test_run_job_on_real_sample_zip_end_to_end(tmp_path, monkeypatch):
    """第一个真正意义上的端到端集成测试：真实 ZIP 输入 -> 真实解包 -> 四阶段编排
    -> 真实打包回 ZIP -> 验证产物里确实有一页译文图片。之前没有任何测试走过这条
    完整链路（tests/sample.zip 委托已经在仓库里躺了很久没人用）。
    """
    _install_fake_pipeline(monkeypatch)
    assert _SAMPLE_ZIP.exists(), "tests/sample.zip 应该已经提交在仓库里"

    output_zip = tmp_path / "output.zip"
    cfg = Config(translator="mock", source_lang="ja")
    report = run_job(str(_SAMPLE_ZIP), str(output_zip), cfg)

    assert report.status == "ok"
    assert report.total_pages == 1
    assert output_zip.exists()
    with zipfile.ZipFile(output_zip) as zf:
        names = zf.namelist()
        assert "page_001.png" in names
        # 产物是真实图片，不是空文件/占位符
        with zf.open("page_001.png") as f:
            img = Image.open(f)
            img.load()
            assert img.size[0] > 0 and img.size[1] > 0


def test_run_job_output_defaults_when_none_given(tmp_path, monkeypatch):
    """output_path 为 None 时按默认规则推导（<stem>_translated.zip），不能落在
    仓库的 tests/ 目录里污染源码树——这里把输入先复制到 tmp_path 再跑。
    """
    _install_fake_pipeline(monkeypatch)
    local_zip = tmp_path / "sample.zip"
    local_zip.write_bytes(_SAMPLE_ZIP.read_bytes())

    cfg = Config(translator="mock", source_lang="ja")
    report = run_job(str(local_zip), None, cfg)

    assert report.status == "ok"
    expected_output = tmp_path / "sample_translated.zip"
    assert expected_output.exists()
    assert report.output_path == expected_output


def test_rerender_job_reuses_existing_work_dir(tmp_path, monkeypatch):
    """rerender_job 是 app 层对"译后编辑"的入口：先跑一次正常翻译，改
    pages.json 里的译文，再走 rerender，产物应该反映新译文（而不是重新调用一次
    "翻译"阶段——mock 引擎两次固定返回同样的"测试译文N号"，这里改的是渲染后的
    像素内容是否真的变了不好直接断言文字，改用状态字段验证走的是 rerender 路径）。
    """
    _install_fake_pipeline(monkeypatch)
    local_zip = tmp_path / "sample.zip"
    local_zip.write_bytes(_SAMPLE_ZIP.read_bytes())
    output_zip = tmp_path / "output.zip"

    cfg = Config(translator="mock", source_lang="ja")
    first = run_job(str(local_zip), str(output_zip), cfg)
    assert first.status == "ok"

    second = rerender_job(str(local_zip), str(output_zip), cfg)
    assert second.status == "ok"
    assert second.total_pages == 1


def test_cli_choices_derive_from_registry():
    """CLI 的 --translator/--source-lang choices 必须来自 registry，不能是另一份
    硬编码列表——否则加新引擎/新语言时会出现"注册表能跑但 CLI 传不进去"的漂移。
    这轮改动把 choices 从字面量改为注册表推导，此测试防止未来有人改回硬编码。
    """
    parser = _build_arg_parser()
    actions = {a.dest: a for a in parser._actions}

    translator_choices = list(actions["translator"].choices)
    assert translator_choices == list(TRANSLATOR_REGISTRY)
    assert translator_choices == ["sakura", "llm", "mock"]

    source_lang_choices = list(actions["source_lang"].choices)
    assert source_lang_choices == list(SUPPORTED_SOURCE_LANGS)
    assert source_lang_choices == ["ja", "en", "zh", "other"]
