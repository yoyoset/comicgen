"""OcrEngine.__init__ 通过 registry.OCR_REGISTRY 分发的测试。

用假工厂替换 registry 里的 factory，验证 OcrEngine 挑对了后端、不需要真的加载
manga_ocr/easyocr。
"""
import pytest

from comic_gen.core.config import Config
from comic_gen.core.errors import ModelError
from comic_gen.pipeline.ocr import OcrEngine
import comic_gen.pipeline.registry as registry_mod


@pytest.fixture
def fake_backends(monkeypatch):
    created = {}

    def fake_ja_factory(cfg):
        created["backend"] = "ja"
        return object()

    def fake_other_factory(cfg):
        created["backend"] = "other"
        return object()

    fake_registry = {
        "ocr_ja": registry_mod.OcrBackendEntry("ocr_ja", frozenset({"ja"}), fake_ja_factory),
        "ocr_other": registry_mod.OcrBackendEntry(
            "ocr_other", frozenset({"en", "zh", "other"}), fake_other_factory,
        ),
    }
    monkeypatch.setattr(registry_mod, "OCR_REGISTRY", fake_registry)
    return created


def test_ocr_engine_picks_ja_backend_for_japanese(fake_backends):
    engine = OcrEngine(Config(source_lang="ja"))
    assert fake_backends["backend"] == "ja"
    assert engine._mocr is not None
    assert engine._easy is None


def test_ocr_engine_picks_other_backend_for_english(fake_backends):
    engine = OcrEngine(Config(source_lang="en"))
    assert fake_backends["backend"] == "other"
    assert engine._easy is not None
    assert engine._mocr is None


def test_ocr_engine_unsupported_language_raises_input_error(monkeypatch):
    from comic_gen.core.errors import InputError

    monkeypatch.setattr(registry_mod, "OCR_REGISTRY", {})
    with pytest.raises(InputError, match="不支持的源语言"):
        OcrEngine(Config(source_lang="klingon"))


def test_ocr_engine_wraps_import_error_as_model_error(monkeypatch):
    """非日文分支的 ImportError 要被包成 ModelError，带装好 easyocr 的指引。"""

    def boom_factory(cfg):
        raise ImportError("no module named easyocr")

    fake_registry = {
        "ocr_other": registry_mod.OcrBackendEntry("ocr_other", frozenset({"en"}), boom_factory),
    }
    monkeypatch.setattr(registry_mod, "OCR_REGISTRY", fake_registry)
    with pytest.raises(ModelError, match="easyocr"):
        OcrEngine(Config(source_lang="en"))
