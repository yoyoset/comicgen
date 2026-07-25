"""本地 GGUF 权重定位：先在磁盘上找，找不到才联网。

为什么需要这个：huggingface_hub 只认它自己写出来的 `hub/models--*/snapshots/` 布局。
同机的 subgen 项目把 GGUF 平铺在 `HF_HOME` 根目录下，comic-gen 原本看不见，
于是即使权重就在盘上，也会去调 `list_repo_files()` 联网列目录——网络一抖就直接崩
（实测：SSL UNEXPECTED_EOF，见 dist/comicgen/comicgen.log 与 README 的"离线运行"一节）。

按文件名递归搜索这个做法对齐 subgen 的 `find_file_recursive`（已在那边长期验证可行），
但有两处收紧：只遍历 .gguf 且带结果缓存；同名多份时取修改时间最新的，而不是遍历栈序随机取。
"""
from __future__ import annotations

import os
from pathlib import Path

# 这些子目录不可能放着可直接加载的 .gguf，遍历时剪掉——F:\hf_cache 这类共享根很大，
# 全量遍历（subgen 的做法）在盘大时明显变慢。
_SKIP_DIRS = {"blobs", ".locks", "xet", ".git", ".cache", "__pycache__"}

_cache: dict[tuple, Path | None] = {}


def gguf_search_roots(model_dir: str | None = None) -> list[Path]:
    """本地权重可能存在的根目录，按优先级排序、去重。

    必须包含 HF_HOME **根目录**而不只是 HF_HOME/hub —— subgen 的平铺文件就在根下。
    （subgen 自己的 find_in_hf_cache 只看 hub/，它不受影响是因为它的 models_dir 本身就是根；
    comic-gen 不能照抄这个疏漏。）
    """
    candidates: list[str | None] = [
        (model_dir or "").strip().strip('"') or None,
        os.environ.get("HF_HUB_CACHE"),
        os.environ.get("HF_HOME"),
        os.path.join(os.environ["HF_HOME"], "hub") if os.environ.get("HF_HOME") else None,
        str(Path.home() / ".cache" / "huggingface" / "hub"),
    ]
    roots: list[Path] = []
    seen: set[str] = set()
    for c in candidates:
        if not c:
            continue
        p = Path(c)
        key = str(p).rstrip("\\/").lower()
        if key in seen:
            continue
        seen.add(key)
        if p.is_dir():
            roots.append(p)
    return roots


def find_local_gguf(filename: str, model_dir: str | None = None) -> Path | None:
    """在本地各缓存根里按文件名找 .gguf，全程不联网。找不到返回 None（不抛错）。"""
    if not filename:
        return None
    target = Path(filename).name.lower()
    roots = gguf_search_roots(model_dir)
    key = (target, tuple(str(r) for r in roots))
    if key in _cache:
        return _cache[key]

    hits: list[Path] = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
            for fn in filenames:
                # 只认完整同名文件：.incomplete 之类的下载残留不会命中
                if fn.lower() == target:
                    hits.append(Path(dirpath) / fn)

    result: Path | None = None
    if hits:
        # 同名多份（典型：hub 布局一份 + 平铺一份）取最新，避免拿到早期损坏/半旧的那份
        result = max(hits, key=lambda p: p.stat().st_mtime)
    _cache[key] = result
    return result


def clear_cache() -> None:
    """model_dir 设置变更后调用，否则仍会返回旧根下的结果。"""
    _cache.clear()
