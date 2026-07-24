"""通用工具。"""
from __future__ import annotations

import json
import re
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


def save_json(obj, path: Path):
    path = Path(path)
    ensure_dir(path.parent)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
