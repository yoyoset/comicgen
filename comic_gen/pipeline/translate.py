"""翻译引擎：本地 Sakura(GGUF，日译中专用) / 本地通用 LLM(GGUF，多语言) / mock（流水线自测）。

全部本地推理，无需联网 API、无需 API Key。
"""
from __future__ import annotations

import re

from ..core.config import Config
from ..core.errors import InputError, ModelError
from ..core.manifest import MODEL_MANIFEST
from ..core.model_locator import STATUS_PATH_BROKEN, STATUS_SIZE_MISMATCH, resolve_status
from ..infra.logging import logger

_LANG_NAMES = {"ja": "日文", "en": "英文", "zh": "中文", "other": "原文语言"}

_SAKURA_SYS = (
    "你是一个轻小说翻译模型，可以流畅通顺地以日本轻小说的风格将日文翻译成简体中文，"
    "并联系上下文正确使用人称代词，不要擅自脱离原文情节。"
)

_LLM_SYS_TMPL = (
    "你是专业的漫画翻译。把给定的{src}漫画台词翻译成{dst}。"
    "要求：口语自然、符合漫画语境；保留原文语气和情绪；"
    "拟声词可意译；不要输出任何解释。"
)


class BaseTranslator:
    name = "base"

    def translate(self, texts: list[str], cfg: Config) -> list[str]:
        raise NotImplementedError

    def _translate_batch_llm(self, texts: list[str], cfg: Config, call_llm) -> list[str]:
        """整页编号批量翻译，保持对齐；解析失败则逐条重试（带上下文）。"""
        numbered = "\n".join(f"{i + 1}. {t.replace(chr(10), ' ')}" for i, t in enumerate(texts))
        prompt = (
            f"把下列编号文本逐条翻译成{cfg.target_lang}，保持编号格式输出，"
            f"每条一行，不要合并不要遗漏：\n{numbered}"
        )
        result = self._parse_numbered(call_llm(prompt), len(texts))
        if result is None:
            logger.warning("整页编号解析失败（编号缺失/重复/多出），回退逐条翻译（%s）", self.name)
            result = [call_llm(self._single_prompt(texts, i, cfg)) for i in range(len(texts))]
        return result

    @staticmethod
    def _single_prompt(texts: list[str], i: int, cfg: Config) -> str:
        """逐条兜底翻译的提示词：带前后一句原文作上下文，而不是完全孤立地翻译一句。"""
        before = texts[i - 1] if i > 0 else ""
        after = texts[i + 1] if i < len(texts) - 1 else ""
        ctx = ""
        if before or after:
            ctx = (
                f"（上下文，仅供理解语境，不要翻译）前一句：{before or '（无）'}；"
                f"后一句：{after or '（无）'}\n"
            )
        return f"{ctx}把下面这句话翻译成{cfg.target_lang}，只输出译文：\n{texts[i]}"

    @staticmethod
    def _parse_numbered(text: str, n: int) -> list[str] | None:
        """解析"编号. 内容"格式的整页批量翻译输出。

        编号集合必须恰好是 {1..n}，重复编号直接判定失败——数量对不代表编号对
        （实测：模型漏掉第 4 条却多输出一条第 7 条，len(items)==n 会误判为"解析成功"，
        第 4 条变空、第 7 条被静默丢弃）。不匹配编号格式的行追加到上一条（多行译文的续行），
        不再静默丢弃。
        """
        items: dict[int, str] = {}
        last_key: int | None = None
        for line in text.strip().splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = re.match(r"^\s*(\d+)\s*[.、．:：]\s*(.+)$", stripped)
            if m:
                idx = int(m.group(1))
                if idx in items:
                    return None  # 重复编号：无法判断哪个才是对的，判定解析失败
                items[idx] = m.group(2).strip()
                last_key = idx
            elif last_key is not None:
                items[last_key] = (items[last_key] + " " + stripped).strip()
        if set(items.keys()) != set(range(1, n + 1)):
            return None
        return [items[i + 1] for i in range(n)]


class _LlamaGGUFTranslator(BaseTranslator):
    """本地 GGUF（llama.cpp）翻译引擎基类：加载 + 调用逻辑复用。

    子类的 name 必须与 core.manifest.MODEL_MANIFEST 的键一致——_resolve_model_path
    用它去查 manifest 拿 repo/filename，也用它去 cfg.model_paths 里查显式绑定。
    子类另需设置 system_prompt、采样参数。
    """

    system_prompt: str = ""
    temperature: float = 0.3
    top_p: float = 1.0

    def __init__(self, cfg: Config):
        # 让 llama_cpp 的 CUDA 后端能找到 torch 自带的 CUDA 运行时 DLL（torch/lib）
        try:
            from ._cuda_runtime import preload_cuda_runtime
            preload_cuda_runtime()
        except Exception:
            pass
        try:
            from llama_cpp import Llama
        except (ImportError, RuntimeError) as e:
            raise ModelError(
                f"未安装 GPU 版 llama-cpp-python 或 CUDA 运行时缺失，无法使用本地 {self.name} 引擎。"
                "安装见 README。",
                technical=str(e),
            ) from e

        path = self._resolve_model_path(cfg)
        self.llm = Llama(model_path=path, n_ctx=4096, n_gpu_layers=-1, verbose=False)
        self.cfg = cfg

    def _resolve_model_path(self, cfg: Config) -> str:
        """走 core.model_locator 的四态状态机：显式绑定 → 本地搜索 → 联网下载兜底。

        原先无条件走 list_repo_files() 联网列目录挑量化，网络一抖就整个任务失败
        （实测 SSL UNEXPECTED_EOF，权重当时其实就躺在 F:\\hf_cache 里）。
        绑定失效（文件被移走）或大小不对（下载不完整）时必须直接报错，不能悄悄
        回落去联网下载一份新的——那会让用户设置里的绑定形同虚设。
        """
        spec = MODEL_MANIFEST[self.name]
        st = resolve_status(self.name, bindings=cfg.model_paths)
        if st.is_ready:
            logger.info("翻译模型使用：%s", st.path)
            return str(st.path)
        if st.state == STATUS_PATH_BROKEN:
            raise ModelError(
                f"设置里绑定的{spec.label}文件已找不到：{st.path}\n"
                f"请到「模型管理」页重新定位，或清除这条绑定。"
            )
        if st.state == STATUS_SIZE_MISMATCH:
            raise ModelError(
                f"{spec.label}文件大小异常（期望 {st.expected_size} 字节，实际 {st.size} 字节），"
                f"可能下载不完整。请到「模型管理」页重新下载。"
            )

        # 未绑定、本地也没搜到：兜底联网下载
        from huggingface_hub import hf_hub_download, list_repo_files

        name = spec.filename
        try:
            if not name:
                files = [f for f in list_repo_files(spec.repo) if f.lower().endswith(".gguf")]
                for pref in ("q4_k_m", "iq4_xs", "iq4xs", "q5_k_m", "q6_k", "q6k", "q8_0", "q8"):
                    cands = [f for f in files if pref in f.lower()]
                    if cands:
                        name = cands[0]
                        break
                else:
                    name = files[0]
            logger.info("翻译模型本地未找到 %s，从 %s 下载…", name, spec.repo)
            return hf_hub_download(spec.repo, name)
        except Exception as e:
            raise ModelError(
                f"本地未找到{spec.label}（{name or spec.repo}），联网下载也失败：{e}\n"
                f"可在「模型管理」页把「模型目录」指向已有权重所在目录，或直接指定文件路径。",
                technical=str(e),
            ) from e

    def _call(self, prompt: str) -> str:
        resp = self.llm.create_chat_completion(
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=self.temperature,
            top_p=self.top_p,
            max_tokens=1024,
        )
        return resp["choices"][0]["message"]["content"].strip()

    def translate(self, texts: list[str], cfg: Config) -> list[str]:
        return self._translate_batch_llm(texts, cfg, self._call)


class SakuraTranslator(_LlamaGGUFTranslator):
    """本地 Sakura：日译中专用轻小说/漫画翻译模型。"""

    name = "sakura"
    system_prompt = _SAKURA_SYS
    temperature = 0.1
    top_p = 0.3

    def __init__(self, cfg: Config):
        if cfg.source_lang != "ja":
            raise InputError("Sakura 为日文专用模型，源语言非日文请改用通用本地引擎（llm）")
        super().__init__(cfg)


class GeneralLLMTranslator(_LlamaGGUFTranslator):
    """本地通用 LLM：多语言（日/英/中/其它）漫画翻译，非日文场景的默认引擎。"""

    name = "llm"
    temperature = 0.3

    def __init__(self, cfg: Config):
        src = _LANG_NAMES.get(cfg.source_lang, "原文语言")
        self.system_prompt = _LLM_SYS_TMPL.format(src=src, dst=cfg.target_lang)
        super().__init__(cfg)


class MockTranslator(BaseTranslator):
    """流水线自测用：不真正翻译，只打标记。"""

    name = "mock"

    def translate(self, texts: list[str], cfg: Config) -> list[str]:
        return [f"测试译文{i + 1}号" for i in range(len(texts))]


# ---------------------------------------------------------------- 注册表（单一来源）
# 引擎分发/语言锁/推荐映射的实际数据和逻辑搬进了 pipeline/registry.py（新增的 OCR
# 后端注册表放在同一个文件，两者是同一类问题）。这里用模块级 __getattr__（PEP 562）
# 而不是模块顶层 `from .registry import ...` 委托——后者会在真实导入时炸：
# registry.py 的 _build_translator_registry() 需要从本模块拿 SakuraTranslator 等类
# （放在函数体内、惰性 import，本身没问题），但如果 registry.py 先于 translate.py
# 被导入，它会在自己模块体执行到一半时反过来 import 本模块；本模块此时如果在顶层
# 又立刻 `from .registry import TRANSLATOR_REGISTRY`，registry.py 还没把这个名字
# 赋值完，就会报 "cannot import name 'TRANSLATOR_REGISTRY' from partially
# initialized module"——这是实测复现过的真崩溃，不是假设性担忧
# （`from comic_gen.pipeline.registry import get_translator` 单独跑必现）。
# __getattr__ 只在属性访问时才触发，此时两个模块各自的顶层类/函数定义早已跑完，
# 不会撞上"对方还在初始化中"的窗口。
_LAZY_REGISTRY_NAMES = {"get_translator", "engine_lang_conflict", "ENGINE_LANG_LOCK", "ENGINE_RECOMMENDATION"}


def __getattr__(name):
    if name not in _LAZY_REGISTRY_NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import registry

    if name == "ENGINE_LANG_LOCK":
        return {n: e.lang_lock for n, e in registry.TRANSLATOR_REGISTRY.items() if e.lang_lock}
    if name == "ENGINE_RECOMMENDATION":
        return {lang: registry.recommended_engine(lang) for lang in ("ja", "en", "zh", "other")}
    return getattr(registry, name)
