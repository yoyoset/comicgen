"""扩展点注册表：翻译引擎、OCR 后端。

取代原来分散在 translate.py（`_TRANSLATORS` dict + 一个特例）和 ocr.py（纯
if/else，没有注册表）里的硬编码分发。加一个新引擎/后端，理想情况下只要在这里
加一条 Entry，不用再满项目找哪几个地方要同步改。

本轮范围：翻译引擎、OCR 后端两个注册表——这是审计里问题最集中的两处（OCR 完全
没有注册表；翻译的"注册表"里已经有一个绕不过去的特例）。runner.py 的
`_needed_model_keys` 和 ocr.py 的构造分支已经改成从这里读，直接修了一个真 bug：
旧代码里 `_needed_model_keys` 只在 `source_lang == "ja"` 时加 `ocr_ja`，从来没有
任何分支会加 `ocr_other`，非日文任务的离线就绪检查永远漏掉这个模型。

明确不做（范围控制，不是遗漏）：`ui/workers.py` 的模型下载面板、
`ui/views/translate_view.py` 的引擎下拉框仍然是各自的硬编码列表，没有改成从这里
读。那两处改动面在 UI 层，没有测试基础设施，改错了不容易发现；等真的要加第三个
翻译引擎/OCR 后端时再顺带一起做，届时才有真实需求校验改法对不对，而不是提前为
假想的第三个引擎做设计。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from ..core.config import Config

if TYPE_CHECKING:
    from .translate import BaseTranslator


@dataclass(frozen=True)
class TranslatorEntry:
    name: str
    factory: Callable[[Config], "BaseTranslator"]
    lang_lock: str | None = None  # 非 None：只支持这一种 source_lang
    recommended_for: frozenset[str] = frozenset()  # 这些 source_lang 推荐用它
    needs_model: bool = True  # mock 引擎不需要下载/绑定任何模型文件


@dataclass(frozen=True)
class OcrBackendEntry:
    key: str  # core.manifest 里的键
    languages: frozenset[str]  # 支持的 source_lang 集合
    factory: Callable[[Config], object]  # 返回满足 core.contracts.OcrBackend 的对象


def _build_translator_registry() -> dict[str, TranslatorEntry]:
    from .translate import GeneralLLMTranslator, MockTranslator, SakuraTranslator

    return {
        "sakura": TranslatorEntry(
            "sakura", SakuraTranslator, lang_lock="ja", recommended_for=frozenset({"ja"}),
        ),
        "llm": TranslatorEntry(
            "llm", GeneralLLMTranslator, recommended_for=frozenset({"en", "zh", "other"}),
        ),
        "mock": TranslatorEntry("mock", lambda cfg: MockTranslator(), needs_model=False),
    }


TRANSLATOR_REGISTRY: dict[str, TranslatorEntry] = _build_translator_registry()


def get_translator(cfg: Config) -> "BaseTranslator":
    from ..core.errors import InputError

    entry = TRANSLATOR_REGISTRY.get(cfg.translator)
    if entry is None:
        raise InputError(f"未知翻译引擎: {cfg.translator}")
    return entry.factory(cfg)


def engine_lang_conflict(engine: str, source_lang: str) -> str | None:
    """若引擎与源语言不兼容，返回人话错误信息；兼容（或引擎未知）则返回 None。"""
    entry = TRANSLATOR_REGISTRY.get(engine)
    if entry is None or entry.lang_lock is None:
        return None
    if entry.lang_lock != source_lang:
        return f"{engine} 引擎仅支持源语言「{entry.lang_lock}」，当前选择的是「{source_lang}」。"
    return None


def recommended_engine(source_lang: str, default: str = "llm") -> str:
    for name, entry in TRANSLATOR_REGISTRY.items():
        if source_lang in entry.recommended_for:
            return name
    return default


def _wants_gpu(cfg: Config) -> bool:
    """cfg.device 请求 GPU 且这台机器真的有 CUDA 才用 GPU——和 detect.ComicDetector
    已经在用的同一条回退规则（请求 cuda 但没有可用设备时不崩，退回 CPU），不是
    "cfg.device=='cuda' 就无脑传 True"。
    """
    if cfg.device != "cuda":
        return False
    import torch

    return torch.cuda.is_available()


def _build_ocr_registry() -> dict[str, OcrBackendEntry]:
    def _ja_factory(cfg: Config):
        from manga_ocr import MangaOcr

        # 之前不传这个参数，MangaOcr 自己探测到有 GPU 就用——cfg.device="cpu" 时
        # 设置页写着"推理设备：CPU"，实际这个模型仍然悄悄用了 GPU。四个模型里
        # 只认 cfg.device 的原来只有 detect/inpaint 两个，manga-ocr/EasyOCR/llama.cpp
        # 这三个各自忽略或硬编码，是审计里明确点出的问题。
        want_gpu = _wants_gpu(cfg)
        model_dir = cfg.model_paths.get("ocr_ja")
        kwargs = {"force_cpu": not want_gpu}
        if model_dir:
            kwargs["pretrained_model_name_or_path"] = model_dir
        return MangaOcr(**kwargs)

    def _other_factory(cfg: Config):
        import easyocr

        code = {"en": ["en"], "zh": ["ch_sim", "en"]}.get(cfg.source_lang, ["en"])
        storage_dir = cfg.model_paths.get("ocr_other")
        return easyocr.Reader(code, gpu=_wants_gpu(cfg), model_storage_directory=storage_dir)

    return {
        "ocr_ja": OcrBackendEntry("ocr_ja", frozenset({"ja"}), _ja_factory),
        "ocr_other": OcrBackendEntry("ocr_other", frozenset({"en", "zh", "other"}), _other_factory),
    }


OCR_REGISTRY: dict[str, OcrBackendEntry] = _build_ocr_registry()


def ocr_backend_for(source_lang: str) -> OcrBackendEntry | None:
    for entry in OCR_REGISTRY.values():
        if source_lang in entry.languages:
            return entry
    return None


def needed_model_keys(cfg: Config) -> list[str]:
    """本次任务要用到的 manifest 键，从翻译/OCR 两个注册表推导。

    取代 runner.py 原来的硬编码判断：旧代码 `if translator in ("sakura","llm")`
    是个陷阱——加一个新的 GGUF 引擎、忘了同步这个元组，离线就绪检查会静默漏掉它；
    `ocr_other` 在旧代码里从来没有任何分支会把它加进去，非日文任务的检查永远不完整。
    """
    keys = ["detect", "lama"]
    ocr_entry = ocr_backend_for(cfg.source_lang)
    if ocr_entry is not None:
        keys.append(ocr_entry.key)
    entry = TRANSLATOR_REGISTRY.get(cfg.translator)
    if entry is not None and entry.needs_model:
        keys.append(entry.name)
    return keys
