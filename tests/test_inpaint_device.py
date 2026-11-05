"""Inpainter 的设备选择与 LaMa 加载：CPU 版 torch 上必须能加载（GPU 组件没装的用户就是这个环境）。

2026-11-05 在 CPU 安装包上实测：big-lama.pt 的张量存在 CUDA 上，simple_lama 的
torch.jit.load 不带 map_location，CPU 版 torch 加载即报错，整页抹字失败。
"""
from __future__ import annotations

import torch

from comic_gen.pipeline import inpaint as inpaint_mod
from comic_gen.pipeline.inpaint import Inpainter, resolve_device


def test_resolve_device_falls_back_to_cpu_without_cuda():
    assert resolve_device("cuda", cuda_available=False) == "cpu"
    assert resolve_device("cuda", cuda_available=True) == "cuda"
    assert resolve_device("cpu", cuda_available=True) == "cpu"


class _Tiny(torch.nn.Module):
    def forward(self, image, mask):
        return image * (1 - mask)


def test_inpainter_loads_with_map_location_and_falls_back(tmp_path, monkeypatch):
    path = tmp_path / "lama.pt"
    torch.jit.save(torch.jit.script(_Tiny()), str(path))
    calls = []
    real_load = torch.jit.load

    def spy(p, map_location=None, **kw):
        calls.append(map_location)
        return real_load(p, map_location=map_location, **kw)

    monkeypatch.setattr(torch.jit, "load", spy)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setenv("LAMA_MODEL", "stale-path-from-elsewhere.pt")
    inp = Inpainter("cuda", model_path=str(path))
    assert inp.device == "cpu"
    assert calls == ["cpu"]                       # 必须带 map_location，否则 CUDA 存的权重在 CPU torch 上加载失败
    assert "LAMA_MODEL" not in inpaint_mod.os.environ


def test_inpainter_runs_a_page_on_cpu(tmp_path, monkeypatch):
    from PIL import Image

    from comic_gen.core.models import Region

    path = tmp_path / "lama.pt"
    torch.jit.save(torch.jit.script(_Tiny()), str(path))
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    inp = Inpainter("cuda", model_path=str(path))
    img = Image.new("RGB", (64, 64), "white")
    r = Region(bbox=[10, 10, 20, 20])
    out = inp.inpaint_page(img, [r])
    assert out.size[0] >= 64 and out.size[1] >= 64
