"""程序自身位置与便携配置路径：对应 subgen 的 paths.rs。

Qt-free。配置文件跟 exe 走（便携软件），不落 %LOCALAPPDATA%——
这是本轮"照抄 subgen"的用户决定，见项目 memory offline-and-model-sharing。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_APP_NAME = "comic-gen"
_LEGACY_APP_NAME = "comic-gen"  # 旧位置（%LOCALAPPDATA%）用的目录名，与新名字凑巧相同


def app_dir() -> Path:
    """程序自身所在目录：EXE 同级，或开发态的仓库根。

    COMICGEN_APP_DIR 环境变量可强制覆盖——app_dir() 否则无法单测
    （pytest 运行时既不是 frozen，也不该真的探测/写入仓库根）。
    """
    override = os.environ.get("COMICGEN_APP_DIR")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return _find_repo_root()


def _find_repo_root() -> Path:
    """从本文件位置向上找含 pyproject.toml 或 .git 的目录。

    不能用 sys.executable：开发态那是 .venv\\Scripts\\python.exe，落在那里毫无意义。
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            return parent
    return here.parents[2]  # comic_gen/core/paths.py -> comic_gen/core -> comic_gen -> 仓库根


def settings_path() -> Path:
    return app_dir() / "settings.json"


def legacy_settings_path() -> Path:
    """迁移前的旧位置：%LOCALAPPDATA%\\comic-gen\\settings.json（非 Windows 落 XDG_CONFIG_HOME）。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / _LEGACY_APP_NAME / "settings.json"


def detect_default_model_dir() -> str:
    """探测本机已有的 HuggingFace 缓存根，首次运行弹窗用它预填模型目录。

    优先级：HF_HUB_CACHE（若设置，通常指向 .../hub，取其父目录作为根）
    → HF_HOME → 用户默认缓存目录。找不到任何线索时返回空串，
    由调用方决定兜底（弹窗里再展示"未检测到，请手动选择"）。
    """
    hub_cache = os.environ.get("HF_HUB_CACHE")
    if hub_cache:
        p = Path(hub_cache)
        return str(p.parent if p.name == "hub" else p)
    home = os.environ.get("HF_HOME")
    if home:
        return home
    default = Path.home() / ".cache" / "huggingface"
    return str(default) if default.exists() else ""


def probe_writable(d: str | Path) -> str | None:
    """目录存在不等于可写：网络盘/只读挂载能正常列目录，写的时候才失败。

    成功返回 None；失败返回人话错误信息（供 UI 直接展示）。
    """
    d = Path(d)
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".comicgen_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return None
    except OSError as e:
        return f"目录不可写：{e}"
