"""core.environment 的离线测试：重点覆盖实测踩过的两个真问题。

(1) is_model_cached("lama") 原先只要 import 包成功就报"已就绪"，权重根本没下也报绿。
(2) is_model_cached 原先忽略 model_dir，而同一份 EnvReport 里 find_model_path 却用了它，
    两者会给出矛盾答案。
"""
import pytest

from comic_gen.core import model_locator
from comic_gen.core.environment import detect_environment, find_model_path, is_model_cached
from comic_gen.core.manifest import MODEL_MANIFEST


@pytest.fixture(autouse=True)
def isolate_env(tmp_path, monkeypatch):
    """所有用例都不该看到真机上的 F:\\hf_cache / ~/.EasyOCR / torch hub 默认权重目录，
    否则会被真实磁盘状态污染——这台机器上三处都真的有实测用过的模型文件。"""
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf_home_unused"))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.setattr(model_locator.Path, "home", staticmethod(lambda: tmp_path / "fake_home"))
    monkeypatch.setattr("torch.hub.get_dir", lambda: str(tmp_path / "fake_torch_hub"))
    model_locator.clear_cache()
    yield
    model_locator.clear_cache()


def test_lama_not_cached_without_real_file(tmp_path):
    """核心回归用例：即使 simple_lama_inpainting 包本身已安装（import 会成功），
    只要 big-lama.pt 没有绑定/没找到，就必须报未就绪——不能靠"包能不能 import"来判断。"""
    assert is_model_cached("lama", model_dir=str(tmp_path)) is False


def test_lama_cached_when_bound_to_real_file(tmp_path):
    spec = MODEL_MANIFEST["lama"]
    f = tmp_path / "big-lama.pt"
    f.write_bytes(spec.magic + b"\x00" * (spec.size_bytes - len(spec.magic)))
    assert is_model_cached("lama", bindings={"lama": str(f)}) is True


def test_lama_size_mismatch_not_cached(tmp_path):
    """截断的文件不能被当成"已就绪"，否则会在真正跑任务时才崩，而不是在诊断页就被拦下。"""
    spec = MODEL_MANIFEST["lama"]
    f = tmp_path / "big-lama.pt"
    f.write_bytes(spec.magic + b"\x00" * 100)  # 远小于 spec.size_bytes
    assert is_model_cached("lama", bindings={"lama": str(f)}) is False


def test_is_model_cached_and_find_model_path_agree(tmp_path):
    """同一次查询下两个函数不能互相矛盾：is_model_cached 说没有，find_model_path 就不该给出路径。"""
    for key in MODEL_MANIFEST:
        cached = is_model_cached(key, model_dir=str(tmp_path))
        path = find_model_path(key, model_dir=str(tmp_path))
        assert cached == (path is not None), f"{key}: is_model_cached={cached} 但 find_model_path={path}"


def test_is_model_cached_respects_model_dir():
    """两个不同的 model_dir 对同一个模型可能给出不同答案——参数必须真的生效。"""
    spec = MODEL_MANIFEST["sakura"]
    import tempfile
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        from pathlib import Path
        (Path(d1) / spec.filename).write_bytes(b"x" * spec.size_bytes)
        assert is_model_cached("sakura", model_dir=d1) is True
        assert is_model_cached("sakura", model_dir=d2) is False


def test_detect_environment_model_status_matches_models_cached(tmp_path):
    report = detect_environment(model_dir=str(tmp_path))
    for key in MODEL_MANIFEST:
        assert report.model_status[key].is_ready == report.models_cached[key]


def test_detect_environment_all_six_keys_present(tmp_path):
    report = detect_environment(model_dir=str(tmp_path))
    assert set(report.models_cached) == set(MODEL_MANIFEST)
    assert set(report.model_status) == set(MODEL_MANIFEST)


def test_detect_environment_skips_model_check_when_disabled(tmp_path):
    report = detect_environment(model_dir=str(tmp_path), check_models=False)
    assert report.models_cached == {}
    assert report.model_status == {}
