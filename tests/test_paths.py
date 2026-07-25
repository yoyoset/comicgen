"""core.paths 的离线测试：全部用 COMICGEN_APP_DIR / monkeypatch 隔离，不碰真实文件系统位置。"""
import sys

from comic_gen.core import paths


def test_app_dir_honors_override(tmp_path, monkeypatch):
    monkeypatch.setenv("COMICGEN_APP_DIR", str(tmp_path))
    assert paths.app_dir() == tmp_path


def test_app_dir_frozen_uses_executable_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("COMICGEN_APP_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    fake_exe = tmp_path / "comicgen-ui.exe"
    fake_exe.write_text("x")
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    assert paths.app_dir() == tmp_path


def test_app_dir_dev_resolves_to_repo_root_not_venv(monkeypatch):
    """开发态必须落到含 pyproject.toml 的仓库根，绝不能是 .venv\\Scripts。"""
    monkeypatch.delenv("COMICGEN_APP_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    root = paths.app_dir()
    assert (root / "pyproject.toml").exists()
    assert "venv" not in str(root).lower()


def test_settings_path_is_next_to_app_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("COMICGEN_APP_DIR", str(tmp_path))
    assert paths.settings_path() == tmp_path / "settings.json"


def test_legacy_settings_path_uses_localappdata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    p = paths.legacy_settings_path()
    assert str(p) == r"C:\Users\test\AppData\Local\comic-gen\settings.json"


def test_detect_default_model_dir_prefers_hub_cache(monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", r"F:\hf_cache\hub")
    monkeypatch.setenv("HF_HOME", r"F:\other")
    assert paths.detect_default_model_dir() == r"F:\hf_cache"


def test_detect_default_model_dir_falls_back_to_hf_home(monkeypatch):
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", r"F:\hf_cache")
    assert paths.detect_default_model_dir() == r"F:\hf_cache"


def test_detect_default_model_dir_empty_when_nothing_set(monkeypatch, tmp_path):
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.setattr(paths.Path, "home", staticmethod(lambda: tmp_path))
    assert paths.detect_default_model_dir() == ""


def test_probe_writable_succeeds_on_new_dir(tmp_path):
    target = tmp_path / "new_dir"
    assert paths.probe_writable(target) is None
    assert target.exists()
    assert not (target / ".comicgen_write_test").exists()  # 探针文件已清理


def test_probe_writable_reports_error_on_file_collision(tmp_path):
    """路径已被同名文件占用时 mkdir 会抛 FileExistsError（OSError 子类），必须报错而不是崩溃。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("x")
    msg = paths.probe_writable(blocker)
    assert msg is not None
    assert "不可写" in msg
