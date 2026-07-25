"""翻译引擎：本地 Sakura(GGUF，日译中专用) / 本地通用 LLM(GGUF，多语言) / mock（流水线自测）。

全部本地推理，无需联网 API、无需 API Key。
"""
from __future__ import annotations

import re
import sys

from ..core.config import Config
from ..core.manifest import MODEL_MANIFEST
from ..core.model_locator import STATUS_PATH_BROKEN, STATUS_SIZE_MISMATCH, resolve_status

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

    子类的 name 必须与 core.manifest.MODEL_MANIFEST 的键一致——_resolve_model_path
    用它去查 manifest 拿 repo/filename，也用它去 cfg.model_paths 里查显式绑定。
    子类另需设置 system_prompt、采样参数。
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
        """走 core.model_locator 的四态状态机：显式绑定 → 本地搜索 → 联网下载兜底。

        原先无条件走 list_repo_files() 联网列目录挑量化，网络一抖就整个任务失败
        （实测 SSL UNEXPECTED_EOF，权重当时其实就躺在 F:\\hf_cache 里）。
        绑定失效（文件被移走）或大小不对（下载不完整）时必须直接报错，不能悄悄
        回落去联网下载一份新的——那会让用户设置里的绑定形同虚设。
        """
        spec = MODEL_MANIFEST[self.name]
        st = resolve_status(self.name, bindings=cfg.model_paths)
        if st.is_ready:
            print(f"[翻译模型] 使用：{st.path}", file=sys.stderr)
            return str(st.path)
        if st.state == STATUS_PATH_BROKEN:
            raise RuntimeError(
                f"设置里绑定的{spec.label}文件已找不到：{st.path}\n"
                f"请到「模型管理」页重新定位，或清除这条绑定。"
            )
        if st.state == STATUS_SIZE_MISMATCH:
            raise RuntimeError(
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
            print(f"[翻译模型] 本地未找到 {name}，从 {spec.repo} 下载…", file=sys.stderr)
            return hf_hub_download(spec.repo, name)
        except Exception as e:
            raise RuntimeError(
                f"本地未找到{spec.label}（{name or spec.repo}），联网下载也失败：{e}\n"
                f"可在「模型管理」页把「模型目录」指向已有权重所在目录，或直接指定文件路径。"
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
            raise ValueError("Sakura 为日文专用模型，源语言非日文请改用通用本地引擎（llm）")
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


# ---------------------------------------------------------------- 引擎/语言约束（单一来源）
# 之前"Sakura 仅日文"这条规则在 UI 预校验与 SakuraTranslator.__init__ 里各写一份。
# 这里收成单一映射，UI 校验直接查表；SakuraTranslator 的运行时检查作为纵深防御保留。
ENGINE_LANG_LOCK: dict[str, str] = {"sakura": "ja"}

# 源语言 → 推荐引擎（UI 语言联动引擎下拉时使用）
ENGINE_RECOMMENDATION: dict[str, str] = {"ja": "sakura", "en": "llm", "zh": "llm", "other": "llm"}


def engine_lang_conflict(engine: str, source_lang: str) -> str | None:
    """若引擎与源语言不兼容，返回人话错误信息；兼容则返回 None。"""
    locked = ENGINE_LANG_LOCK.get(engine)
    if locked and locked != source_lang:
        return f"{engine} 引擎仅支持源语言「{locked}」，当前选择的是「{source_lang}」。"
    return None
