"""翻译引擎：DeepSeek API / 本地 Sakura(GGUF) / mock（流水线自测）。"""
from __future__ import annotations

import re

from ..core.config import Config

_SYS_PROMPT = (
    "你是专业的日本漫画翻译。把给定的日文漫画台词翻译成{lang}。"
    "要求：口语自然、符合漫画语境；保留原文语气和情绪；"
    "拟声词可意译；不要输出任何解释。"
)

_SAKURA_SYS = (
    "你是一个轻小说翻译模型，可以流畅通顺地以日本轻小说的风格将日文翻译成简体中文，"
    "并联系上下文正确使用人称代词，不要擅自脱离原文情节。"
)


class BaseTranslator:
    name = "base"

    def translate(self, texts: list[str], cfg: Config) -> list[str]:
        raise NotImplementedError

    def _translate_batch_llm(self, texts: list[str], cfg: Config, call_llm) -> list[str]:
        """整页编号批量翻译，保持对齐；解析失败则逐条重试。"""
        numbered = "\n".join(f"{i + 1}. {t.replace(chr(10), ' ')}" for i, t in enumerate(texts))
        prompt = (
            f"把下列编号日文逐条翻译成{cfg.target_lang}，保持编号格式输出，"
            f"每条一行，不要合并不要遗漏：\n{numbered}"
        )
        result = self._parse_numbered(call_llm(prompt), len(texts))
        if result is None:
            result = [
                call_llm(f"把下面的日文翻译成{cfg.target_lang}，只输出译文：\n{t}") for t in texts
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


class DeepSeekTranslator(BaseTranslator):
    name = "deepseek"

    def __init__(self, cfg: Config):
        from openai import OpenAI

        key = cfg.resolved_api_key()
        if not key:
            raise ValueError("缺少 DeepSeek API Key（请在界面填写或设置环境变量 DEEPSEEK_API_KEY）")
        self.client = OpenAI(api_key=key, base_url=cfg.deepseek_base_url)
        self.model = cfg.deepseek_model
        self.cfg = cfg

    def _call(self, prompt: str) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _SYS_PROMPT.format(lang=self.cfg.target_lang)},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )
        return resp.choices[0].message.content.strip()

    def translate(self, texts: list[str], cfg: Config) -> list[str]:
        return self._translate_batch_llm(texts, cfg, self._call)


class SakuraTranslator(BaseTranslator):
    name = "sakura"

    def __init__(self, cfg: Config):
        if cfg.source_lang != "ja":
            raise ValueError("Sakura 为日文专用模型，源语言非日文请改用 deepseek")
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
                "未安装 GPU 版 llama-cpp-python 或 CUDA 运行时缺失，无法使用本地 Sakura 引擎。安装见 README。"
            ) from e
        from pathlib import Path

        from huggingface_hub import hf_hub_download, list_repo_files

        filename = cfg.sakura_filename
        if filename and Path(filename).exists():
            path = filename  # 本地 .gguf 路径（用户可用浏览器/下载工具自行下载）
        else:
            if not filename:
                files = [f for f in list_repo_files(cfg.sakura_repo) if f.lower().endswith(".gguf")]
                # 量化偏好：体积小且质量够用的优先
                for pref in ("q4_k_m", "iq4_xs", "iq4xs", "q5_k_m", "q6_k", "q6k", "q8_0", "q8"):
                    cands = [f for f in files if pref in f.lower()]
                    if cands:
                        filename = cands[0]
                        break
                else:
                    filename = files[0]
            path = hf_hub_download(cfg.sakura_repo, filename)
        self.llm = Llama(model_path=path, n_ctx=4096, n_gpu_layers=-1, verbose=False)
        self.cfg = cfg

    def _call(self, prompt: str) -> str:
        resp = self.llm.create_chat_completion(
            messages=[
                {"role": "system", "content": _SAKURA_SYS},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            top_p=0.3,
            max_tokens=1024,
        )
        return resp["choices"][0]["message"]["content"].strip()

    def translate(self, texts: list[str], cfg: Config) -> list[str]:
        return self._translate_batch_llm(texts, cfg, self._call)


class MockTranslator(BaseTranslator):
    """流水线自测用：不真正翻译，只打标记。"""

    name = "mock"

    def translate(self, texts: list[str], cfg: Config) -> list[str]:
        return [f"测试译文{i + 1}号" for i in range(len(texts))]


_TRANSLATORS = {
    "deepseek": DeepSeekTranslator,
    "sakura": SakuraTranslator,
    "mock": MockTranslator,
}


def get_translator(cfg: Config) -> BaseTranslator:
    cls = _TRANSLATORS.get(cfg.translator)
    if cls is None:
        raise ValueError(f"未知翻译引擎: {cfg.translator}")
    if cls is MockTranslator:
        return cls()
    return cls(cfg)
