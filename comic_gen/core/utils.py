"""通用工具。"""
from __future__ import annotations

import json
import os
import re
import shutil
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


def atomic_save_image(image, path) -> None:
    """把 PIL 图片原子写入 path（PNG）：同目录临时文件 + os.replace。

    pages.json 会在图片写完之后才把状态改成 rendered，所以坏图本身会随重跑自愈；
    但进程被恰好杀在写盘中途时，translated/ 下会留一张截断的 PNG——校对画布的
    preview_path 优先取渲染图，这张坏图会让校对页显示一块破图，直到重跑覆盖它。
    用原子替换让"要么没有、要么完整"，不留下可被读到的半张图。
    """
    path = Path(path)
    ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            image.save(f, format="PNG")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def estimate_output_requirement(pages: list) -> int:
    """估算一次任务在 work_dir 里需要多少新磁盘空间（字节）。

    按各输入图的磁盘大小求和后再乘系数：每种输入都要在 work_dir 里再产出一份
    译文图（translated/），文件夹/压缩包还要再复制/打包成一份最终产物，PDF 的光栅化
    图只会比原 PDF 更大。2.5 是偏保守的粗略系数，宁可高估让用户提前腾空间，也不要
    跑到一半把盘写满。统计失败（单个文件被锁/权限拒绝）按 0 计，不把整个检查拖垮。
    """
    total = 0
    for p in pages:
        try:
            total += Path(p).stat().st_size
        except OSError:
            continue
    return int(total * 2.5)


def available_space(path: Path) -> int:
    """path 所在磁盘的可用空间（字节）；探测失败抛 OSError 由调用方兜底。"""
    target = Path(path)
    base = target if target.exists() else target.parent if target.parent.exists() else target.anchor
    return shutil.disk_usage(base).free
