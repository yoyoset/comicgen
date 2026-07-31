"""pipeline.registry 的测试：翻译引擎 + OCR 后端两个注册表。

只测数据/分发逻辑，不触发真正的模型加载（工厂函数只在真的构造引擎时才 import
manga_ocr/easyocr/llama_cpp，注册表本身是轻量的）。
"""
import pytest

from comic_gen.core.config import Config
from comic_gen.core.errors import InputError
from comic_gen.pipeline.registry import (
    OCR_REGISTRY,
    TRANSLATOR_REGISTRY,
    engine_lang_conflict,
    get_translator,
    needed_model_keys,
    ocr_backend_for,
    recommended_engine,
)
from comic_gen.pipeline.translate import MockTranslator


def test_translator_registry_has_three_engines():
    assert set(TRANSLATOR_REGISTRY) == {"sakura", "llm", "mock"}


def test_get_translator_mock_needs_no_cfg_model_path():
    tr = get_translator(Config(translator="mock"))
    assert isinstance(tr, MockTranslator)


def test_get_translator_unknown_engine_raises_input_error():
    with pytest.raises(InputError, match="nope"):
        get_translator(Config(translator="nope"))


def test_engine_lang_conflict_sakura_locked_to_japanese():
    assert engine_lang_conflict("sakura", "ja") is None
    msg = engine_lang_conflict("sakura", "en")
    assert msg and "ja" in msg


def test_engine_lang_conflict_llm_has_no_lock():
    assert engine_lang_conflict("llm", "en") is None
    assert engine_lang_conflict("llm", "ja") is None


def test_engine_lang_conflict_unknown_engine_returns_none():
    """未知引擎不该在这里报错——那是 get_translator 的职责，这里只判断语言锁。"""
    assert engine_lang_conflict("nope", "ja") is None


def test_recommended_engine():
    assert recommended_engine("ja") == "sakura"
    assert recommended_engine("en") == "llm"
    assert recommended_engine("zh") == "llm"
    assert recommended_engine("other") == "llm"


def test_recommended_engine_unknown_lang_falls_back_to_default():
    assert recommended_engine("klingon") == "llm"
    assert recommended_engine("klingon", default="mock") == "mock"


def test_ocr_registry_has_two_backends():
    assert set(OCR_REGISTRY) == {"ocr_ja", "ocr_other"}


def test_ocr_backend_for_japanese():
    entry = ocr_backend_for("ja")
    assert entry is not None
    assert entry.key == "ocr_ja"


def test_ocr_backend_for_non_japanese():
    for lang in ("en", "zh", "other"):
        entry = ocr_backend_for(lang)
        assert entry is not None
        assert entry.key == "ocr_other"


def test_ocr_backend_for_unknown_lang_returns_none():
    assert ocr_backend_for("klingon") is None


# ---------------------------------------------------------------- needed_model_keys


def test_needed_model_keys_japanese_sakura():
    cfg = Config(translator="sakura", source_lang="ja")
    keys = needed_model_keys(cfg)
    assert set(keys) == {"detect", "lama", "ocr_ja", "sakura"}


def test_needed_model_keys_english_llm_includes_ocr_other():
    """回归测试：旧的硬编码实现从来没有任何分支会加 ocr_other，非日文任务的离线
    就绪检查永远不完整。
    """
    cfg = Config(translator="llm", source_lang="en")
    keys = needed_model_keys(cfg)
    assert "ocr_other" in keys
    assert set(keys) == {"detect", "lama", "ocr_other", "llm"}


def test_needed_model_keys_mock_translator_needs_no_translator_model():
    """mock 引擎不需要任何模型文件——needs_model=False，不该出现在需要的 key 里。"""
    cfg = Config(translator="mock", source_lang="ja")
    keys = needed_model_keys(cfg)
    assert "mock" not in keys
    assert set(keys) == {"detect", "lama", "ocr_ja"}


def test_needed_model_keys_unknown_translator_omitted_gracefully():
    cfg = Config(translator="nope", source_lang="ja")
    keys = needed_model_keys(cfg)
    assert set(keys) == {"detect", "lama", "ocr_ja"}


# ---------------------------------------------------------------- 循环导入回归测试


def test_importing_registry_before_translate_does_not_crash():
    """实测复现过的真崩溃：registry.py 的 _build_translator_registry() 需要从
    translate.py 拿类（惰性 import，在函数体内没问题），但如果 translate.py 顶层
    也立刻 `from .registry import TRANSLATOR_REGISTRY`，谁先被导入就会在对方模块体
    执行到一半时炸——"cannot import name ... from partially initialized module"。

    这个场景在同一个 pytest 进程里测不出来：collection 阶段早就把两个模块都导入
    过了，sys.modules 里已经是完整状态。必须开一个全新的子进程，只导入 registry，
    验证它不需要 translate.py 先被导入过。
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "from comic_gen.pipeline.registry import get_translator; print('OK')"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_importing_translate_before_registry_still_exposes_lazy_attrs():
    """反过来的顺序也要验证：translate.py 先被导入，随后访问
    get_translator/engine_lang_conflict/ENGINE_LANG_LOCK/ENGINE_RECOMMENDATION
    这几个惰性属性（模块级 __getattr__ 处理）要能正常工作，不是只有"不崩"这一个
    要求。
    """
    import subprocess
    import sys

    script = (
        "from comic_gen.pipeline.translate import ("
        "get_translator, engine_lang_conflict, ENGINE_LANG_LOCK, ENGINE_RECOMMENDATION); "
        "assert ENGINE_LANG_LOCK == {'sakura': 'ja'}; "
        "assert ENGINE_RECOMMENDATION['ja'] == 'sakura'; "
        "assert engine_lang_conflict('sakura', 'en') is not None; "
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
