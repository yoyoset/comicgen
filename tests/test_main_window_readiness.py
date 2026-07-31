"""MainWindow._models_not_ready_message 的离线测试。

只测这一个纯逻辑方法：它只读 self.settings.model_files/model_dir，不碰任何 Qt 控件，
所以不需要构造真正的 QMainWindow/QApplication——用一个只带 settings 属性的假 self 调用
未绑定方法即可，避免在没有 UI 测试基础设施的项目里为这一条测试搭一整套离屏 Qt 环境。

resolve_status 会兜底搜索 HF_HOME/HF_HUB_CACHE/~/.cache/huggingface/~/.cache/torch/hub
这类真实环境路径（这台开发机上真的装着模型），必须把它们全部指到空的 tmp_path，
否则测试结果取决于本机是否装过模型——不是自包含测试数据。
"""
from pathlib import Path

import pytest

from comic_gen.core.config import Config
from comic_gen.core.model_locator import clear_cache
from comic_gen.ui.main_window import MainWindow


class _FakeSettings:
    def __init__(self, model_dir: str, model_files=None):
        self.model_dir = model_dir
        self.model_files = model_files or {}


class _FakeWindow:
    def __init__(self, model_dir: str, model_files=None):
        self.settings = _FakeSettings(model_dir, model_files)


@pytest.fixture(autouse=True)
def _isolate_model_search(tmp_path, monkeypatch):
    """把所有本地权重搜索的兜底路径都指到空目录，防止本机真实缓存渗进测试结果。"""
    empty_home = tmp_path / "fake_home"
    empty_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: empty_home)
    # os.path.expanduser("~")（torch.hub.get_dir() 内部用它拼 TORCH_HOME 默认值）在 Windows
    # 上认 HOME/USERPROFILE，不认 Path.home()——只 monkeypatch Path.home 测出来是漏的
    # （实测：本机 torch hub checkpoints 目录下真的有一份 big-lama.pt，第一版测试直接漏判）。
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setenv("USERPROFILE", str(empty_home))
    for var in ("HF_HOME", "HF_HUB_CACHE", "TORCH_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    clear_cache()
    yield
    clear_cache()


def test_missing_models_produce_a_message_listing_each_one(tmp_path):
    model_dir = tmp_path / "empty_model_dir"
    model_dir.mkdir()
    cfg = Config(translator="mock", source_lang="ja")
    msg = MainWindow._models_not_ready_message(_FakeWindow(str(model_dir)), cfg)
    assert msg is not None
    # mock 翻译引擎不需要模型文件，detect/lama/ocr_ja 三个都应该被列出（空目录，找不到任何一个）
    assert msg.count("·") == 3


def test_bound_but_broken_path_is_reported_as_not_ready(tmp_path):
    model_dir = tmp_path / "empty_model_dir"
    model_dir.mkdir()
    broken = tmp_path / "does_not_exist.gguf"
    cfg = Config(translator="mock", source_lang="ja")
    msg = MainWindow._models_not_ready_message(
        _FakeWindow(str(model_dir), model_files={"lama": str(broken)}), cfg
    )
    assert msg is not None
    assert "已不在原位置" in msg


def test_all_models_ready_returns_none(tmp_path, monkeypatch):
    """反过来验证：所有 needed_model_keys 都判定 ready 时不应该拦截。直接把
    resolve_status 换成一个恒返回 READY 的假实现，不需要构造真实模型文件。"""
    from comic_gen.core.model_locator import ModelStatus, STATUS_READY

    def _fake_resolve_status(key, *, bindings=None, model_dir=None):
        return ModelStatus(STATUS_READY, path=Path("fake"), size=1)

    monkeypatch.setattr("comic_gen.core.model_locator.resolve_status", _fake_resolve_status)
    cfg = Config(translator="mock", source_lang="ja")
    msg = MainWindow._models_not_ready_message(_FakeWindow(str(tmp_path)), cfg)
    assert msg is None
