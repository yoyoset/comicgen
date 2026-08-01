"""4.3 回归测试：GGUF 翻译引擎（sakura/llm）之前硬编码 n_gpu_layers=-1，设置页选
"CPU" 对这两个引擎完全不起作用——四个模型里只有 detect/inpaint 认 cfg.device。
用假 Llama 类捕获实际传入的 n_gpu_layers，不需要真的加载 GGUF 权重。
"""
from comic_gen.pipeline._cuda_runtime import preload_cuda_runtime

# 和 _LlamaGGUFTranslator.__init__ 同样的原因：llama_cpp 的 CUDA 后端 DLL 依赖
# torch 自带的运行时（torch/lib），必须先把这个目录加进 DLL 搜索路径，否则连
# `import llama_cpp` 本身都会在加载 llama.dll 时报 FileNotFoundError。
preload_cuda_runtime()

import llama_cpp
import pytest

from comic_gen.core.config import Config
from comic_gen.core.model_locator import STATUS_READY, ModelStatus
from comic_gen.pipeline.translate import SakuraTranslator
import comic_gen.pipeline.translate as translate_mod


@pytest.fixture
def fake_llama(monkeypatch):
    calls = []

    class FakeLlama:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(llama_cpp, "Llama", FakeLlama)
    monkeypatch.setattr(
        translate_mod, "resolve_status",
        lambda name, bindings=None: ModelStatus(STATUS_READY, path=__import__("pathlib").Path("fake.gguf")),
    )
    return calls


def test_cuda_device_offloads_all_layers(fake_llama):
    SakuraTranslator(Config(translator="sakura", source_lang="ja", device="cuda"))
    assert fake_llama[-1]["n_gpu_layers"] == -1


def test_cpu_device_uses_zero_gpu_layers(fake_llama):
    SakuraTranslator(Config(translator="sakura", source_lang="ja", device="cpu"))
    assert fake_llama[-1]["n_gpu_layers"] == 0
