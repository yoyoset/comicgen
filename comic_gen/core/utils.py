"""通用工具。"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def natural_key(s: str):
    """自然数排序 key：page2 < page10。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def is_image(p: Path) -> bool:
    return p.suffix.lower() in IMAGE_EXTS


def list_images(folder: Path, recursive: bool = True) -> list[Path]:
    """列出图片，自然排序。默认递归子目录（漫画常按分卷子文件夹组织）。"""
    root = Path(folder)
    it = root.rglob("*") if recursive else root.iterdir()
    files = [p for p in it if p.is_file() and is_image(p)]
    return sorted(files, key=lambda p: natural_key(str(p.relative_to(root))))


def ensure_dir(p: Path) -> Path:
    Path(p).mkdir(parents=True, exist_ok=True)
    return Path(p)


def atomic_write_text(path, text: str) -> None:
    """把 text 写入 path，用「同目录临时文件 + 原子替换」避免半写文件。

    不能直接 path.write_text()：pages.json / job.json / settings.json 是任务恢复的
    唯一依据，进程在写盘中途被杀会留下截断/损坏的 JSON——_load_pages 只能把它当
    "没有缓存"整体丢弃，等于丢掉整个任务的断点（翻译阶段可能已跑几十分钟）。
    临时文件与目标同目录保证同一文件系统上，os.replace 在 Windows/NTFS 上是原子的；
    写盘中途失败时清理临时文件并原样保留目标文件，绝不留下半份内容。
    """
    path = Path(path)
    ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_json(obj, path: Path):
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
