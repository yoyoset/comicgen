"""输入解包与输出打包：文件夹 / ZIP(CBZ) / EPUB。"""
from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from ..core.environment import find_unrar
from ..core.errors import InputError
from ..core.utils import ensure_dir, is_image, list_images, natural_key


@dataclass
class LoadedInput:
    kind: str  # folder | zip | epub
    input_path: Path
    work_dir: Path  # 图片所在根目录（folder 时为输入文件夹本身）
    pages: list[Path]  # 排序后的图片绝对路径
    rel_names: list[str]  # 每页相对 work_dir 的 posix 名，用于打包与中间产物命名
    meta: dict = field(default_factory=dict)


def load_input(input_path: str | Path, extract_dir: str | Path, cfg=None, load_cb=None) -> LoadedInput:
    p = Path(input_path)
    # 最常见的用户错误：路径打错字/文件被移走。旧行为是 p.is_dir()==False 且无后缀时
    # 报"不支持的输入类型: xxx"，把"路径不存在"伪装成"格式不支持"，误导排查方向。
    if not p.exists():
        raise InputError(f"输入路径不存在：{p}。请检查路径是否正确。")
    if p.is_dir():
        if load_cb:
            load_cb(1, 1)
        return _load_folder(p)
    suffix = p.suffix.lower()
    if suffix in (".zip", ".cbz"):
        return _load_zip(p, Path(extract_dir))
    if suffix in (".rar", ".cbr"):
        return _load_rar(p, Path(extract_dir))
    if suffix == ".epub":
        return _load_epub(p, Path(extract_dir))
    if suffix == ".pdf":
        return _load_pdf(p, Path(extract_dir), getattr(cfg, "pdf_dpi", 150), load_cb)
    raise InputError(f"不支持的输入类型: {p}（支持 文件夹 / ZIP / CBZ / RAR / CBR / EPUB / PDF）")


# ---------------------------------------------------------------- 输入

def _load_folder(p: Path) -> LoadedInput:
    pages = list_images(p, recursive=True)
    if not pages:
        raise InputError(f"文件夹中没有图片: {p}")
    return LoadedInput("folder", p, p, pages, [str(pg.relative_to(p)).replace("\\", "/") for pg in pages])


def _extract_archive(p: Path, out: Path) -> Path:
    src = ensure_dir(out / "src")
    try:
        with zipfile.ZipFile(p) as zf:
            zf.extractall(src)
    except (OSError, zipfile.BadZipFile) as e:
        # 文件不存在/不可读/不是有效 zip——之前是裸 FileNotFoundError/BadZipFile 一路
        # 抛到最外层，CLI/UI 都只能显示原始堆栈。这是用户最容易撞到的错误之一
        # （路径打错字），值得包成人话。
        raise InputError(f"无法打开输入文件：{p}（{e}）", technical=str(e)) from e
    return src


def _load_zip(p: Path, out: Path) -> LoadedInput:
    src = _extract_archive(p, out)
    pages = sorted(
        (f for f in src.rglob("*") if f.is_file() and is_image(f)),
        key=lambda f: natural_key(str(f.relative_to(src))),
    )
    if not pages:
        raise InputError(f"压缩包中没有图片: {p}")
    rel = [str(f.relative_to(src)).replace("\\", "/") for f in pages]
    return LoadedInput("zip", p, src, pages, rel)


def _load_rar(p: Path, out: Path) -> LoadedInput:
    """RAR/CBR 输入：用 rarfile 解包（需系统 UnRAR.exe，常见于 WinRAR）。输出按 CBZ(zip) 同构。"""
    try:
        import rarfile
    except ImportError as e:
        raise InputError("打开 RAR/CBR 需要 rarfile：pip install rarfile") from e
    # 本机常见 WinRAR 安装位置兜底（单一来源：core.environment.UNRAR_CANDIDATES）
    unrar = find_unrar()
    if unrar:
        rarfile.UNRAR_TOOL = unrar
    src = ensure_dir(out / "src")
    try:
        with rarfile.RarFile(p) as rf:
            rf.extractall(src)
    except Exception as e:
        raise InputError(f"RAR/CBR 解压失败（请确认已安装 WinRAR 或 UnRAR）：{e}") from e
    pages = sorted(
        (f for f in src.rglob("*") if f.is_file() and is_image(f)),
        key=lambda f: natural_key(str(f.relative_to(src))),
    )
    if not pages:
        raise InputError(f"压缩包中没有图片: {p}")
    rel = [str(f.relative_to(src)).replace("\\", "/") for f in pages]
    return LoadedInput("zip", p, src, pages, rel)  # 输出按 zip/CBZ 处理


def _load_pdf(p: Path, out: Path, dpi: int = 200, load_cb=None) -> LoadedInput:
    """PDF 输入：每页渲染为 PNG，后续按图片处理；输出再合并回 PDF。"""
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise InputError("打开 PDF 需要 PyMuPDF：pip install pymupdf") from e
    src = ensure_dir(out / "src")
    pages: list[Path] = []
    with fitz.open(p) as doc:
        total = doc.page_count
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        for i, page in enumerate(doc, 1):
            pix = page.get_pixmap(matrix=mat, alpha=False)
            fname = f"page_{i:04d}.png"
            pix.save(src / fname)
            pages.append(src / fname)
            if load_cb:
                try:
                    load_cb(i, total)
                except Exception:
                    pass
    if not pages:
        raise InputError(f"PDF 中没有可渲染页面: {p}")
    rel = [pg.name for pg in pages]
    return LoadedInput("pdf", p, src, pages, rel)


def _load_epub(p: Path, out: Path) -> LoadedInput:
    src = _extract_archive(p, out)
    image_map = {}  # arcname -> abs path
    for f in src.rglob("*"):
        if f.is_file() and is_image(f):
            image_map[str(f.relative_to(src)).replace("\\", "/")] = f
    ordered = _epub_image_order(src)
    names = [n for n in ordered if n in image_map]
    if not names:  # 兜底：全部图片自然排序
        names = sorted(image_map, key=natural_key)
    if not names:
        raise InputError(f"EPUB 中没有图片: {p}")
    return LoadedInput("epub", p, src, [image_map[n] for n in names], names)


def _epub_image_order(src: Path) -> list[str]:
    """按 OPF spine 顺序提取每页 xhtml 中的图片，返回相对 EPUB 根的 arcname 列表。"""
    try:
        container = ET.parse(src / "META-INF" / "container.xml")
        ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
        rootfile = container.find(".//c:rootfile", ns)
        if rootfile is None:
            return []
        opf_path = (src / rootfile.attrib["full-path"]).resolve()
        opf = ET.parse(opf_path)
        opf_ns = {"o": "http://www.idpf.org/2007/opf"}
        manifest = {}
        for item in opf.findall(".//o:manifest/o:item", opf_ns):
            manifest[item.attrib["id"]] = item.attrib["href"]
        result = []
        for ref in opf.findall(".//o:spine/o:itemref", opf_ns):
            href = manifest.get(ref.attrib["idref"], "")
            if not href:
                continue
            xhtml_path = (opf_path.parent / unquote(href)).resolve()
            if is_image(xhtml_path):  # 非标准但存在：spine 直接指向图片
                result.append(str(xhtml_path.relative_to(src)).replace("\\", "/"))
                continue
            if not xhtml_path.exists():
                continue
            text = xhtml_path.read_text(encoding="utf-8", errors="ignore")
            imgs = re.findall(r"<img[^>]+src=[\"']([^\"']+)[\"']", text, re.I)
            imgs += re.findall(r"<image[^>]+href=[\"']([^\"']+)[\"']", text, re.I)
            for s in imgs:
                img_path = (xhtml_path.parent / unquote(s)).resolve()
                try:
                    result.append(str(img_path.relative_to(src)).replace("\\", "/"))
                except ValueError:
                    continue
        return result
    except Exception:
        return []


# ---------------------------------------------------------------- 输出

def default_output_path(loaded: LoadedInput) -> Path:
    p = loaded.input_path
    if loaded.kind == "folder":
        return p.parent / (p.name + "_translated")
    suffix = {
        ".cbz": ".cbz", ".cbr": ".cbz", ".rar": ".cbz",  # RAR 不可写，输出转 CBZ
        ".epub": ".epub", ".pdf": ".pdf",
    }.get(p.suffix.lower(), ".zip")
    return p.parent / (p.stem + "_translated" + suffix)


def _resolve_page_image(name: str, translated_dir: Path, orig_map: dict[str, Path]) -> Path | None:
    """优先用翻译后的图；该页翻译失败（检测/OCR/翻译/渲染任一阶段出错）没有产出时，
    回退用原图，而不是把这一页整个跳过。

    之前的行为是任何一页失败就不写进 translated_dir，打包时原样跳过——JobReport 里
    能看到失败计数，但交付的文件页数比原作少，很容易被忽略。回退原图虽然那一页没有
    译文，至少页数和原作对得上，用户翻到那页能看到原文而不是发现书缺了一页。
    """
    img = translated_dir / name
    if img.exists():
        return img
    orig = orig_map.get(name)
    if orig is not None and Path(orig).exists():
        return Path(orig)
    return None


def _clean_stale_output_images(out: Path, expected_rel_names: set[str]) -> None:
    """删除输出目录里不在当前任务页清单中的图片文件。

    文件夹输入的输出目录（通常也是工作目录）在多次运行间复用：旧任务多出的页、或换过
    输入后留下的旧图，会在最终交付物里残留——deliverable 的页数/内容比当前输入多，
    是"污染旧任务目录"的经典问题。只删图片文件、且跳过 translated/ 与 _extract/
    这些中间产物子目录，不动 pages.json/job.json/run.log 等状态文件（它们本来也不是
    图片，is_image 天然过滤）。
    """
    protected = (out / "translated", out / "_extract")
    for f in out.rglob("*"):
        if not (f.is_file() and is_image(f)):
            continue
        if any(f.is_relative_to(root) for root in protected):
            continue
        rel = str(f.relative_to(out)).replace("\\", "/")
        if rel not in expected_rel_names:
            f.unlink(missing_ok=True)


def pack_output(loaded: LoadedInput, translated_dir: Path, output_path: Path,
                 pdf_dpi: int = 150, log=None) -> Path:
    """把翻译后的图片按输入同构打包。log 不为 None 时，对每个回退用原图的页调用一次。"""
    translated_dir = Path(translated_dir)
    output_path = Path(output_path)
    orig_map = dict(zip(loaded.rel_names, loaded.pages))

    def resolve(name: str) -> Path | None:
        img = _resolve_page_image(name, translated_dir, orig_map)
        if img is not None and log is not None and not (translated_dir / name).exists():
            log(f"[警告] {name} 翻译未完成，打包时使用原图")
        return img

    if loaded.kind == "folder":
        out = ensure_dir(output_path)
        # 输出目录复用旧任务时，先清掉不属于当前输入的图片，避免交付物里残留旧页
        _clean_stale_output_images(out, set(loaded.rel_names))
        for name in loaded.rel_names:
            img = resolve(name)
            if img is not None:
                ensure_dir((out / name).parent)
                shutil.copy2(img, out / name)
        return out
    if loaded.kind == "zip":  # 含 ZIP/CBZ 与 RAR/CBR（_load_rar 也归此）
        ensure_dir(output_path.parent)
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in loaded.rel_names:
                img = resolve(name)
                if img is not None:
                    zf.write(img, name)
        return output_path
    if loaded.kind == "pdf":
        return _pack_pdf(loaded, translated_dir, output_path, pdf_dpi, log=log)
    if loaded.kind == "epub":
        staging = loaded.work_dir
        for name in loaded.rel_names:
            img = resolve(name)
            dst = staging / name
            if img is not None and Path(img).resolve() != dst.resolve():
                shutil.copy2(img, dst)
        ensure_dir(output_path.parent)
        with zipfile.ZipFile(output_path, "w") as zf:
            mt = staging / "mimetype"
            if mt.exists():  # EPUB 规范：mimetype 必须为首项且不压缩
                zf.write(mt, "mimetype", compress_type=zipfile.ZIP_STORED)
            for f in sorted(staging.rglob("*")):
                if f.is_file() and f.name != "mimetype":
                    arc = str(f.relative_to(staging)).replace("\\", "/")
                    zf.write(f, arc, compress_type=zipfile.ZIP_DEFLATED)
        return output_path
    raise ValueError(f"未知输入类型: {loaded.kind}")


def _pack_pdf(loaded: LoadedInput, translated_dir: Path, output_path: Path,
              pdf_dpi: int = 150, log=None) -> Path:
    """把翻译后的每页 PNG 合并回 PDF（每页整页铺图）。"""
    import fitz

    orig_map = dict(zip(loaded.rel_names, loaded.pages))
    ensure_dir(output_path.parent)
    out = fitz.open()
    for name in loaded.rel_names:
        img = _resolve_page_image(name, translated_dir, orig_map)
        if img is None:
            continue
        if log is not None and not (translated_dir / name).exists():
            log(f"[警告] {name} 翻译未完成，打包时使用原图")
        # 以图片像素 @pdf_dpi 推导页面尺寸（与输入渲染 DPI 一致；回退的原图本来就是
        # 按 pdf_dpi 光栅化出来的 PNG，尺寸假设同样成立）
        from PIL import Image as _IMG

        with _IMG.open(img) as im:
            w_pt, h_pt = im.size[0] * 72 / pdf_dpi, im.size[1] * 72 / pdf_dpi
        page = out.new_page(width=w_pt, height=h_pt)
        page.insert_image(page.rect, filename=str(img))
    out.save(str(output_path))
    out.close()
    return output_path
