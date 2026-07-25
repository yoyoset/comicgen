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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .manifest import LAYOUT_DIR, LAYOUT_FILE, MODEL_MANIFEST, ModelSpec

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
    """model_dir 或 model_files 绑定变更后调用，否则仍会返回旧结果。"""
    _cache.clear()


# ==================================================================== 四态状态机
# 对应 subgen 的 ModelStatus：不是一个"有/没有"的布尔。已实测踩过两个坑：
# is_model_cached("lama") 原先只要 import 包成功就报"已就绪"，权重根本没下也报绿；
# manga-ocr-base 的 HF 快照曾经分裂成两个目录，"目录存在"本身不是可靠判据。

STATUS_READY = "ready"
STATUS_MISSING = "missing"
STATUS_PATH_BROKEN = "path_broken"
STATUS_SIZE_MISMATCH = "size_mismatch"
STATUS_INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class ModelStatus:
    state: str
    path: Path | None = None
    size: int = 0
    expected_size: int = 0
    missing_members: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_ready(self) -> bool:
        return self.state == STATUS_READY


def status_of(
    path: Path | None,
    *,
    is_binding: bool,
    layout: str,
    exists: bool,
    size: int = 0,
    expected_size: int = 0,
    missing_members: tuple[str, ...] = (),
) -> ModelStatus:
    """纯判定函数：不碰磁盘，只根据已经查好的事实下结论。可表驱动单测。"""
    if path is None or not exists:
        return ModelStatus(STATUS_PATH_BROKEN if is_binding else STATUS_MISSING, path=path)
    if layout == LAYOUT_DIR:
        if missing_members:
            return ModelStatus(STATUS_INCOMPLETE, path=path, missing_members=missing_members)
        return ModelStatus(STATUS_READY, path=path, size=size)
    # LAYOUT_FILE
    if expected_size and size != expected_size:
        return ModelStatus(STATUS_SIZE_MISMATCH, path=path, size=size, expected_size=expected_size)
    return ModelStatus(STATUS_READY, path=path, size=size, expected_size=expected_size)


def _missing_members(dir_path: Path, member_files: tuple[tuple[str, ...], ...]) -> tuple[str, ...]:
    """按 manifest 的 AND-of-OR 成员表检查目录，返回缺失的（每组取首个名字代表）。"""
    try:
        present = {p.name for p in dir_path.iterdir() if p.is_file()}
    except OSError:
        return tuple(g[0] for g in member_files)
    return tuple(g[0] for g in member_files if not any(f in present for f in g))


def _repo_cache_dirname(repo: str) -> str:
    return "models--" + repo.replace("/", "--")


def _dir_search_candidates(spec: ModelSpec, model_dir: str | None) -> list[Path]:
    """LAYOUT_DIR 模型的候选目录：有 repo 的按 HF snapshot 布局搜，没有 repo 的（如 EasyOCR）
    把搜索根本身和已知默认存储目录当候选。"""
    candidates: list[Path] = []
    if spec.repo:
        dirname = _repo_cache_dirname(spec.repo)
        for root in gguf_search_roots(model_dir):
            for hub_dir in (root, root / "hub"):
                snapshots = hub_dir / dirname / "snapshots"
                if snapshots.is_dir():
                    candidates.extend(sorted(p for p in snapshots.iterdir() if p.is_dir()))
    else:
        # 无 repo（如 EasyOCR）：只信任显式指定的模型目录本身与已知默认存储位置，
        # 不能把 HF 缓存根这类无关大目录也当候选——那会把"目录存在但不相关"
        # 误判成"这个模型部分下载了"。
        if model_dir:
            candidates.append(Path(model_dir))
        default = Path.home() / ".EasyOCR" / "model"
        if default.is_dir():
            candidates.append(default)

    seen: set[str] = set()
    uniq: list[Path] = []
    for c in candidates:
        k = str(c).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def resolve_status(key: str, *, bindings: Mapping[str, str] | None = None,
                    model_dir: str | None = None) -> ModelStatus:
    """做实际磁盘 I/O，返回该模型的当前状态。"""
    spec = MODEL_MANIFEST[key]
    bound = (bindings or {}).get(key)
    is_binding = bool(bound)

    if spec.layout == LAYOUT_FILE:
        if is_binding:
            p = Path(bound)
            exists = p.is_file()
            size = p.stat().st_size if exists else 0
            return status_of(p, is_binding=True, layout=LAYOUT_FILE, exists=exists,
                              size=size, expected_size=spec.size_bytes)
        found = find_local_gguf(spec.filename, model_dir) if spec.filename else None
        if found is None:
            return ModelStatus(STATUS_MISSING)
        size = found.stat().st_size
        return status_of(found, is_binding=False, layout=LAYOUT_FILE, exists=True,
                          size=size, expected_size=spec.size_bytes)

    # LAYOUT_DIR
    if is_binding:
        p = Path(bound)
        exists = p.is_dir()
        missing = _missing_members(p, spec.member_files) if exists else tuple(g[0] for g in spec.member_files)
        return status_of(p, is_binding=True, layout=LAYOUT_DIR, exists=exists, missing_members=missing)

    best_incomplete: tuple[Path, tuple[str, ...]] | None = None
    for cand in _dir_search_candidates(spec, model_dir):
        if not cand.is_dir():  # 候选目录本身不存在，不算"找到但不完整"
            continue
        missing = _missing_members(cand, spec.member_files)
        if not missing:
            return status_of(cand, is_binding=False, layout=LAYOUT_DIR, exists=True, missing_members=())
        if best_incomplete is None:
            best_incomplete = (cand, missing)
    if best_incomplete:
        cand, missing = best_incomplete
        return status_of(cand, is_binding=False, layout=LAYOUT_DIR, exists=True, missing_members=missing)
    return ModelStatus(STATUS_MISSING)


def resolve_model_path(key: str, *, bindings: Mapping[str, str] | None = None,
                        model_dir: str | None = None) -> Path | None:
    st = resolve_status(key, bindings=bindings, model_dir=model_dir)
    return st.path if st.is_ready else None


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def status_text(s: ModelStatus) -> tuple[str, str]:
    """(人话, 颜色)。颜色用于 UI 直接设前景色。"""
    if s.state == STATUS_READY:
        return f"已就绪 · {_human_size(s.size)}", "#2e8b2e"
    if s.state == STATUS_MISSING:
        return "未安装", "#888888"
    if s.state == STATUS_PATH_BROKEN:
        return "绑定的文件已不在原位置（可能被移动或删除了）", "#b8860b"
    if s.state == STATUS_SIZE_MISMATCH:
        return (f"文件大小异常（期望 {_human_size(s.expected_size)}，"
                f"实际 {_human_size(s.size)}），可能下载不完整"), "#c0392b"
    if s.state == STATUS_INCOMPLETE:
        return f"目录不完整，缺少：{', '.join(s.missing_members)}", "#c0392b"
    return s.state, "#888888"


def find_duplicates(key: str, *, model_dir: str | None = None) -> list[Path]:
    """按文件名找该模型的全部本地副本（不只是第一个命中）。仅对 LAYOUT_FILE 有意义。"""
    spec = MODEL_MANIFEST[key]
    if spec.layout != LAYOUT_FILE or not spec.filename:
        return []
    target = spec.filename.lower()
    hits: list[Path] = []
    for root in gguf_search_roots(model_dir):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
            for fn in filenames:
                if fn.lower() == target:
                    hits.append(Path(dirpath) / fn)
    # 搜索根本身会重叠（HF_HOME 与 HF_HOME/hub 都在根列表里，前者的递归遍历已经
    # 覆盖后者），同一个物理文件可能从两个根各命中一次——按解析后的绝对路径去重，
    # 否则会把"只有一份"误报成"有重复"。
    seen: set[str] = set()
    uniq: list[Path] = []
    for h in hits:
        k = str(h.resolve()).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(h)
    return uniq


def trash_destination(p: Path) -> Path:
    """同目录下的 _trash\\ 子目录——结构上保证与原文件同卷，改名而非跨盘复制。

    同名冲突时加数字后缀，从不覆盖已有的回收项。
    """
    trash_dir = p.parent / "_trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    dest = trash_dir / p.name
    if not dest.exists():
        return dest
    i = 1
    while True:
        candidate = trash_dir / f"{p.stem}_{i}{p.suffix}"
        if not candidate.exists():
            return candidate
        i += 1
