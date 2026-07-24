"""翻译引擎：本地 Sakura(GGUF，日译中专用) / 本地通用 LLM(GGUF，多语言) / mock（流水线自测）。

全部本地推理，无需联网 API、无需 API Key。
"""
from __future__ import annotations

import re
from pathlib import Path

from ..core.config import Config

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
        """整页编号批量翻译，保持对齐；解析失败则逐条重试。"""
        numbered = "\n".join(f"{i + 1}. {t.replace(chr(10), ' ')}" for i, t in enumerate(texts))
        prompt = (
            f"把下列编号文本逐条翻译成{cfg.target_lang}，保持编号格式输出，"
            f"每条一行，不要合并不要遗漏：\n{numbered}"
        )
        result = self._parse_numbered(call_llm(prompt), len(texts))
        if result is None:
            result = [
                call_llm(f"把下面的文本翻译成{cfg.target_lang}，只输出译文：\n{t}") for t in texts
            ]
        return result

    @staticmethod
    def _parse_numbered(text: str, n: int) -> list[str] | None:
        items = {}
        for line in text.strip().splitlines():
            m = re.match(r"^\s*(\d+)\s*[.、．:：]\s*(.+)$", line.strip())
            if m:
                items[int(m.group(1))] = m.group(2).strip()
        if len(items) < n:
            return None
        return [items.get(i + 1, "") for i in range(n)]


class _LlamaGGUFTranslator(BaseTranslator):
    """本地 GGUF（llama.cpp）翻译引擎基类：加载 + 调用逻辑复用。

    子类需设置 repo/filename 来源（见 _resolve_model）与 system_prompt、采样参数。
    """

    system_prompt: str = ""
    temperature: float = 0.3
    top_p: float = 1.0

    def __init__(self, cfg: Config):
        # 让 llama_cpp 的 CUDA 后端能找到 nvidia-* pip 包里的 CUDA 运行时 DLL
        try:
            from ._cuda_runtime import preload_cuda_runtime
            preload_cuda_runtime()
        except Exception:
            pass
        try:
            from llama_cpp import Llama
        except (ImportError, RuntimeError) as e:
            raise ImportError(
                f"未安装 GPU 版 llama-cpp-python 或 CUDA 运行时缺失，无法使用本地 {self.name} 引擎。"
                "安装见 README。"
            ) from e

        path = self._resolve_model_path(cfg)
        self.llm = Llama(model_path=path, n_ctx=4096, n_gpu_layers=-1, verbose=False)
        self.cfg = cfg

    def _resolve_model_path(self, cfg: Config) -> str:
        from huggingface_hub import hf_hub_download, list_repo_files

        repo, filename = self._model_source(cfg)
        if filename and Path(filename).exists():
            return filename  # 本地 .gguf 路径（用户可自行下载指定）
        if not filename:
            files = [f for f in list_repo_files(repo) if f.lower().endswith(".gguf")]
            for pref in ("q4_k_m", "iq4_xs", "iq4xs", "q5_k_m", "q6_k", "q6k", "q8_0", "q8"):
                cands = [f for f in files if pref in f.lower()]
                if cands:
                    filename = cands[0]
                    break
            else:
                filename = files[0]
        return hf_hub_download(repo, filename)

    def _model_source(self, cfg: Config) -> tuple[str, str]:
        raise NotImplementedError

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
            raise ValueError("Sakura 为日文专用模型，源语言非日文请改用通用本地引擎（llm）")
        super().__init__(cfg)

    def _model_source(self, cfg: Config) -> tuple[str, str]:
        return cfg.sakura_repo, cfg.sakura_filename


class GeneralLLMTranslator(_LlamaGGUFTranslator):
    """本地通用 LLM：多语言（日/英/中/其它）漫画翻译，非日文场景的默认引擎。"""

    name = "llm"
    temperature = 0.3

    def __init__(self, cfg: Config):
        src = _LANG_NAMES.get(cfg.source_lang, "原文语言")
        self.system_prompt = _LLM_SYS_TMPL.format(src=src, dst=cfg.target_lang)
        super().__init__(cfg)

    def _model_source(self, cfg: Config) -> tuple[str, str]:
        return cfg.llm_repo, cfg.llm_filename


class MockTranslator(BaseTranslator):
    """流水线自测用：不真正翻译，只打标记。"""

    name = "mock"

    def translate(self, texts: list[str], cfg: Config) -> list[str]:
        return [f"测试译文{i + 1}号" for i in range(len(texts))]


_TRANSLATORS = {
    "sakura": SakuraTranslator,
    "llm": GeneralLLMTranslator,
    "mock": MockTranslator,
}


def get_translator(cfg: Config) -> BaseTranslator:
    cls = _TRANSLATORS.get(cfg.translator)
    if cls is None:
        raise ValueError(f"未知翻译引擎: {cfg.translator}")
    if cls is MockTranslator:
        return cls()
    return cls(cfg)
