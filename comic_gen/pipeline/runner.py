"""流水线编排：解包 → 检测 → OCR → 翻译 → 抹字渲染 → 打包。

中间产物 pages.json 每阶段落盘，支持断点续跑与译后重渲染。
工作目录结构：
    <work_dir>/pages.json        全部中间结果
    <work_dir>/translated/       翻译后的图片（按输入相对路径组织）
    <work_dir>/src/              压缩包解包出的原图（zip/epub）
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from PIL import Image

from ..core.config import Config
from ..core.job import JobReport
from ..core.models import (
    STATUS_DETECTED,
    STATUS_OCRED,
    STATUS_PENDING,
    STATUS_RENDERED,
    STATUS_TRANSLATED,
    Page,
)
from ..core.utils import ensure_dir, load_json, save_json
from .loader import LoadedInput, default_output_path, load_input, pack_output

_STAGES = ("detect", "ocr", "translate", "render")


class Runner:
    def __init__(self, cfg: Config | None = None, log=print):
        self.cfg = cfg or Config()
        self.log = log
        self._detector = None
        self._ocr = None
        self._inpainter = None
        self._translator = None

    # ------------------------------------------------------------ 懒加载模型
    def _get_detector(self):
        if self._detector is None:
            from .detect import ComicDetector

            self.log("加载检测模型…")
            self._detector = ComicDetector(self.cfg.detector_repo, self.cfg.device, self.cfg.det_threshold)
        return self._detector

    def _get_ocr(self):
        if self._ocr is None:
            from .ocr import OcrEngine

            self.log("加载 OCR 模型…")
            self._ocr = OcrEngine(self.cfg)
        return self._ocr

    def _get_inpainter(self):
        if self._inpainter is None:
            from .inpaint import Inpainter

            self.log("加载抹字模型…")
            self._inpainter = Inpainter(self.cfg.device)
        return self._inpainter

    def _get_translator(self):
        name = self.cfg.translator
        if self._translator is None or getattr(self._translator, "name", None) != name:
            from .translate import get_translator

            self.log(f"加载翻译引擎: {name}")
            self._translator = get_translator(self.cfg)
        return self._translator

    def _needed_model_keys(self) -> list[str]:
        keys = ["detect", "lama"]
        if self.cfg.source_lang == "ja":
            keys.append("ocr_ja")
        if self.cfg.translator in ("sakura", "llm"):
            keys.append(self.cfg.translator)
        return keys

    def _go_offline_if_ready(self) -> None:
        """本次任务要用的模型都在本地时切离线，免得网络不通时白等 HF 的 etag 重试。"""
        from ..core.environment import enable_offline_mode, is_model_cached

        missing = [k for k in self._needed_model_keys()
                   if not is_model_cached(k, bindings=self.cfg.model_paths)]
        if missing:
            self.log(f"以下模型尚未就位，需要联网获取: {'、'.join(missing)}")
            return
        enable_offline_mode()
        self.log("模型均已在本地，本次运行不联网")

    # ------------------------------------------------------------ 中间产物
    @staticmethod
    def _pages_file(work_dir: Path) -> Path:
        return work_dir / "pages.json"

    def _save_pages(self, work_dir: Path, pages: list[Page], input_path: Path | None = None):
        data = {"pages": [p.to_dict() for p in pages]}
        if input_path is not None:
            data["input"] = str(input_path.resolve())
        save_json(data, self._pages_file(work_dir))

    def _load_pages(self, work_dir: Path, input_path: Path) -> dict[str, Page]:
        """读取缓存；缓存与当前输入不一致时丢弃（避免不同输入污染同一工作目录）。"""
        f = self._pages_file(work_dir)
        if not f.exists():
            return {}
        try:
            data = load_json(f)
            if data.get("input") and data["input"] != str(input_path.resolve()):
                return {}
            return {p["image"]: Page.from_dict(p) for p in data.get("pages", [])}
        except Exception:
            return {}

    # ------------------------------------------------------------ 主流程
    def run(self, input_path: str | Path, output_path: str | Path | None = None,
            progress=None, rerender: bool = False, load_cb=None,
            should_cancel: Callable[[], bool] | None = None,
            work_dir_override: str | Path | None = None) -> JobReport:
        """input_path 必须是最初翻译时的原始输入（pages.json 里记录的指纹），
        而不是翻译产出的工作目录/输出路径——否则 _load_pages 的指纹校验不通过，
        缓存会被当作"输入不一致"丢弃，rerender 实质上会从零重跑。
        跨会话场景（不知道原始输入路径，只知道工作目录）用 work_dir_override 绕开
        _work_dir 的重新推导，直接指定工作目录；此时仍需传入正确的 input_path
        以便重新解包出干净的原图供抹字/渲染使用。
        """
        input_path = Path(input_path)
        self._go_offline_if_ready()
        work_dir = Path(work_dir_override) if work_dir_override else self._work_dir(input_path, output_path)
        extract_dir = work_dir / "_extract"
        if extract_dir.exists():  # 清空旧解包，防止不同输入互相污染
            shutil.rmtree(extract_dir)

        def _load_cb(i, n):
            if load_cb:
                try:
                    load_cb(i, n)
                except Exception:
                    pass
            self.log(f"[load] {i}/{n}")

        loaded = load_input(input_path, extract_dir, self.cfg, load_cb=_load_cb)
        if output_path is None:
            output_path = default_output_path(loaded)
        output_path = Path(output_path)
        self._check_output_safe(input_path, output_path)
        trans_dir = ensure_dir(work_dir / "translated")

        pages = self._init_pages(loaded, work_dir, rerender)
        self._save_pages(work_dir, pages, input_path)
        self.log(f"共 {len(pages)} 页，工作目录: {work_dir}")

        page_img = {p.image: path for p, path in zip(pages, loaded.pages)}

        def open_page(p: Page) -> Image.Image:
            return Image.open(page_img[p.image]).convert("RGB")

        def cancelled() -> bool:
            if should_cancel is None:
                return False
            try:
                return bool(should_cancel())
            except Exception:
                return False

        # 每个阶段间隙检查取消：当前页处理完、落盘后才退出，pages.json 始终完好。
        # 命中取消直接返回，跳过 pack_output（不产出不完整的最终包）。

        # 1. 检测
        todo = [p for p in pages if p.status == STATUS_PENDING]
        if todo:
            det = self._get_detector()
            for i, p in enumerate(todo):
                if cancelled():
                    self.log("已停止（检测阶段）")
                    return JobReport.cancelled(pages)
                try:
                    p.regions = det.detect(open_page(p))
                    p.width, p.height = Image.open(page_img[p.image]).size
                    p.status = STATUS_DETECTED
                except Exception as e:
                    p.error = f"检测失败: {e}"
                    self.log(f"[警告] {p.image} 检测失败: {e}")
                self._save_pages(work_dir, pages, input_path)
                self._progress(progress, "detect", i + 1, len(todo))

        # 2. OCR
        todo = [p for p in pages if p.status == STATUS_DETECTED]
        if todo:
            ocr = self._get_ocr()
            for i, p in enumerate(todo):
                if cancelled():
                    self.log("已停止（OCR 阶段）")
                    return JobReport.cancelled(pages)
                try:
                    ocr.recognize_regions(open_page(p), p.regions)
                    p.status = STATUS_OCRED
                except Exception as e:
                    p.error = f"OCR 失败: {e}"
                    self.log(f"[警告] {p.image} OCR 失败: {e}")
                self._save_pages(work_dir, pages, input_path)
                self._progress(progress, "ocr", i + 1, len(todo))

        # 3. 翻译
        todo = [p for p in pages if p.status == STATUS_OCRED]
        if todo:
            translator = self._get_translator()
            for i, p in enumerate(todo):
                if cancelled():
                    self.log("已停止（翻译阶段）")
                    self._save_pages(work_dir, pages, input_path)
                    return JobReport.cancelled(pages)
                regions = [r for r in p.regions if r.source_text.strip()]
                if regions:
                    texts = [r.source_text for r in regions]
                    try:
                        results = translator.translate(texts, self.cfg)
                        for r, t in zip(regions, results):
                            r.translated_text = t
                        p.status = STATUS_TRANSLATED
                    except Exception as e:
                        p.error = f"翻译失败: {e}"
                        self.log(f"[警告] {p.image} 翻译失败: {e}")
                else:
                    p.status = STATUS_TRANSLATED
                self._progress(progress, "translate", i + 1, len(todo))
            self._save_pages(work_dir, pages, input_path)

        # 4. 抹字 + 渲染
        todo = [p for p in pages if p.status == STATUS_TRANSLATED]
        if todo:
            from .render import render_page

            inpainter = self._get_inpainter()
            for i, p in enumerate(todo):
                if cancelled():
                    self.log("已停止（渲染阶段）")
                    return JobReport.cancelled(pages)
                try:
                    img = open_page(p)
                    text_regions = [r for r in p.regions if r.source_text.strip()]
                    cleaned = inpainter.inpaint_page(img, text_regions)
                    out_img = render_page(cleaned, [r for r in text_regions if r.translated_text.strip()], self.cfg)
                    out_path = trans_dir / p.image
                    ensure_dir(out_path.parent)
                    out_img.save(out_path)
                    p.status = STATUS_RENDERED
                except Exception as e:
                    p.error = f"抹字渲染失败: {e}"
                    self.log(f"[警告] {p.image} 抹字渲染失败: {e}")
                self._save_pages(work_dir, pages, input_path)
                self._progress(progress, "render", i + 1, len(todo))

        result = pack_output(loaded, trans_dir, output_path, pdf_dpi=self.cfg.pdf_dpi)
        report = JobReport.from_pages(pages, result)
        self.log(f"完成: {result}（{report.summary}）")
        return report

    # ------------------------------------------------------------ 重渲染（译后编辑）
    def rerender(self, input_path: str | Path, output_path: str | Path | None = None,
                 progress=None, should_cancel: Callable[[], bool] | None = None,
                 work_dir_override: str | Path | None = None) -> JobReport:
        return self.run(input_path, output_path, progress=progress, rerender=True,
                         should_cancel=should_cancel, work_dir_override=work_dir_override)

    # ------------------------------------------------------------ 内部
    def _work_dir(self, input_path: Path, output_path) -> Path:
        if input_path.is_dir():
            return ensure_dir(Path(output_path) if output_path else input_path.parent / (input_path.name + "_translated"))
        out = Path(output_path) if output_path else input_path.parent / (input_path.stem + "_translated" + input_path.suffix)
        return ensure_dir(out.parent / out.stem)

    def _init_pages(self, loaded: LoadedInput, work_dir: Path, rerender: bool) -> list[Page]:
        cached = self._load_pages(work_dir, loaded.input_path)
        pages = []
        for name in loaded.rel_names:
            p = cached.get(name, Page(image=name))
            if rerender and p.status in (STATUS_TRANSLATED, STATUS_RENDERED):
                p.status = STATUS_TRANSLATED  # 强制回到渲染前
            pages.append(p)
        return pages

    def _progress(self, cb, stage: str, i: int, total: int):
        if cb:
            try:
                cb(i, total, stage)
            except Exception:
                pass
        self.log(f"[{stage}] {i}/{total}")

    @staticmethod
    def _check_output_safe(input_path: Path, output_path: Path):
        """防止输出覆盖原图源。"""
        try:
            ip = input_path.resolve()
            op = output_path.resolve()
        except Exception:
            return
        if op == ip:
            raise ValueError("输出路径与输入相同，会覆盖原图，已阻止。请指定其它输出路径。")
        if input_path.is_dir() and ip in op.parents:
            raise ValueError("输出目录位于输入目录内，可能污染/覆盖原图，已阻止。请指定输入目录外的路径。")
