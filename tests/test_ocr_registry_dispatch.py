"""OcrEngine.__init__ 通过 registry.OCR_REGISTRY 分发的测试。

用假工厂替换 registry 里的 factory，验证 OcrEngine 挑对了后端、不需要真的加载
manga_ocr/easyocr。
"""
import pytest

import comic_gen.pipeline.registry as registry_mod
from comic_gen.core.config import Config
from comic_gen.core.errors import ModelError
from comic_gen.pipeline.ocr import OcrEngine


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
    assert engine._recognizer is not None


def test_ocr_engine_picks_other_backend_for_english(fake_backends):
    engine = OcrEngine(Config(source_lang="en"))
    assert fake_backends["backend"] == "other"
    assert engine._recognizer is not None


def test_ocr_engine_unsupported_language_raises_input_error(monkeypatch):
    from comic_gen.core.errors import InputError

    monkeypatch.setattr(registry_mod, "OCR_REGISTRY", {})
    with pytest.raises(InputError, match="不支持的源语言"):
        OcrEngine(Config(source_lang="klingon"))


def test_ocr_engine_wraps_import_error_as_model_error(monkeypatch):
    """兜底层：第三方后端自己的工厂没把依赖缺失转成 ModelError 时，OcrEngine 统一转一次。
    自带的 easyocr 后端已在 registry._other_factory 里自行翻译成人话，不会走到这里。
    """

    def boom_factory(cfg):
        raise ImportError("no module named xyz")

    fake_registry = {
        "ocr_other": registry_mod.OcrBackendEntry("ocr_other", frozenset({"en"}), boom_factory),
    }
    monkeypatch.setattr(registry_mod, "OCR_REGISTRY", fake_registry)
    with pytest.raises(ModelError, match="OCR 后端加载失败"):
        OcrEngine(Config(source_lang="en"))


def test_other_factory_raises_model_error_with_install_guidance(monkeypatch):
    """easyocr 缺失时，registry._other_factory 自己给出安装指引（人话），
    而不是让裸 ImportError 一路炸到 UI。用假的 __import__ 模拟 easyocr 不可导入。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "easyocr":
            raise ImportError("No module named 'easyocr'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    entry = registry_mod._build_ocr_registry()["ocr_other"]
    with pytest.raises(ModelError, match="easyocr"):
        entry.factory(Config(source_lang="en"))
