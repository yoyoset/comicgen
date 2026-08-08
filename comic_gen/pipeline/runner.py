"""流水线编排：解包 → 检测 → OCR → 翻译 → 抹字渲染 → 打包。

中间产物 pages.json 每阶段落盘，支持断点续跑与译后重渲染。
工作目录结构：
    <work_dir>/pages.json        全部中间结果
    <work_dir>/translated/       翻译后的图片（按输入相对路径组织）
    <work_dir>/src/              压缩包解包出的原图（zip/epub）
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Callable

from PIL import Image

from .. import __version__
from ..core.config import Config
from ..core.errors import OutputError, Stage
from ..core.job import JobReport, build_job_snapshot
from ..core.models import (
    STATUS_DETECTED,
    STATUS_OCRED,
    STATUS_PENDING,
    STATUS_RENDERED,
    STATUS_TRANSLATED,
    Page,
)
from ..core.utils import available_space, ensure_dir, estimate_output_requirement, load_json, save_json
from ..infra.engine_cache import EngineCache
from .loader import LoadedInput, default_output_path, load_input, pack_output

_STAGES = ("detect", "ocr", "translate", "render")


def select_drawable_regions(regions: list) -> list:
    """挑出既有原文也有译文的区域：只有这些该被抹字+重绘。

    只有原文没有译文的区域（编号解析失败/漏译）必须原样保留，绝不能被 LaMa 抹掉——
    抹了却没有译文可画，会在页面上留下不可逆的空白洞，比保留原文更糟。
    """
    return [r for r in regions if r.source_text.strip() and r.translated_text.strip()]


def render_single_page(img: Image.Image, page: Page, cfg: Config, inpainter, log=None) -> Image.Image:
    """跑一页的抹字+渲染，返回处理后的图。只做图像处理，不落盘、不改 page.status——
    调用方决定存到哪、什么时候存。

    从 Runner._run_body 的渲染阶段循环体里抽出来，供两处复用：批量渲染循环（本文件）
    和校对会话的单页重渲染（app/review_session.py）——校对界面改完一个气泡的译文后，
    用户要能立刻看到这一页的效果，不需要（也不该）把其它页跟着重新抹字渲染一遍。
    """
    from .render import render_page

    drawable = select_drawable_regions(page.regions)
    total_text = sum(1 for r in page.regions if r.source_text.strip())
    skipped = total_text - len(drawable)
    if skipped and log is not None:
        log(f"[警告] {page.image} 有 {skipped} 处区域未获得译文，保留原文不抹除")
    cleaned = inpainter.inpaint_page(img, drawable)
    return render_page(cleaned, drawable, cfg)


class Runner:
    def __init__(self, cfg: Config | None = None, log=print):
        self.cfg = cfg or Config()
        self.log = log
        self._detector_cache: EngineCache = EngineCache()
        self._ocr_cache: EngineCache = EngineCache()
        self._inpainter_cache: EngineCache = EngineCache()
        self._translator_cache: EngineCache = EngineCache()

    # ------------------------------------------------------------ 懒加载模型
    #
    # 每个引擎持有自己的 EngineCache（infra.engine_cache），按"依赖字段的签名"判断
    # 缓存是否还有效——JobController 复用同一个 Runner、只换 cfg（改设置不重启整个
    # 应用），只判 `is None` 等于"只要建过一次就永远不重建"（实测复现：源语言从日文
    # 切到英文后再跑一次，_get_ocr() 返回的还是按日文构造的 manga-ocr 实例）。

    def _get_detector(self):
        sig = (self.cfg.model_paths.get("detect"), self.cfg.detector_repo,
               self.cfg.device, self.cfg.det_threshold)

        def factory():
            from .detect import ComicDetector

            self.log("加载检测模型…")
            repo = self.cfg.model_paths.get("detect") or self.cfg.detector_repo
            return ComicDetector(repo, self.cfg.device, self.cfg.det_threshold)

        return self._detector_cache.get_or_rebuild(sig, factory)

    def _get_ocr(self):
        # OcrEngine.__init__ 按 source_lang 分支选 manga-ocr / EasyOCR，且分别绑定
        # ocr_ja/ocr_other 两个不同的模型路径——这几项任一变化都必须重建。cfg.device
        # 也要算进去（4.3 之前 registry.py 的两个 OCR 工厂函数都不认 cfg.device，
        # 现在认了，缓存签名不带上就会出现同一类"改了设置不生效"的陈旧缓存 bug）。
        sig = (self.cfg.source_lang, self.cfg.model_paths.get("ocr_ja"),
               self.cfg.model_paths.get("ocr_other"), self.cfg.device)

        def factory():
            from .ocr import OcrEngine

            self.log("加载 OCR 模型…")
            return OcrEngine(self.cfg)

        return self._ocr_cache.get_or_rebuild(sig, factory)

    def _get_inpainter(self):
        sig = (self.cfg.device, self.cfg.model_paths.get("lama"))

        def factory():
            from .inpaint import Inpainter

            self.log("加载抹字模型…")
            return Inpainter(self.cfg.device, model_path=self.cfg.model_paths.get("lama"))

        return self._inpainter_cache.get_or_rebuild(sig, factory)

    def _get_translator(self):
        # 之前的签名只有 cfg.translator 一项——实测复现的真 bug：GeneralLLMTranslator
        # 在构造时把 source_lang/target_lang 烤进 system_prompt（"把日文台词翻译成
        # 简体中文"这类），只换 target_lang 不换引擎名字，缓存判断不出该重建，会一直
        # 用旧语言对的 system_prompt 推理。同一个引擎名绑定的模型路径变了也要重建，
        # 和 detector/ocr/inpainter 保持一致。cfg.device 现在也影响 GGUF 引擎的
        # n_gpu_layers（4.3），同样要算进签名，否则切 CPU/GPU 不会重建、白改设置。
        sig = (self.cfg.translator, self.cfg.source_lang, self.cfg.target_lang,
               self.cfg.model_paths.get(self.cfg.translator), self.cfg.device)

        def factory():
            from .translate import get_translator

            self.log(f"加载翻译引擎: {self.cfg.translator}")
            return get_translator(self.cfg)

        return self._translator_cache.get_or_rebuild(sig, factory)

    def _needed_model_keys(self) -> list[str]:
        # 从 registry.py 的翻译/OCR 两个注册表推导，不再是这里硬编码的判断——旧代码
        # `if translator in ("sakura","llm")` 是个陷阱（加新引擎要记得同步这个元组），
        # 且从未有任何分支会把 ocr_other 加进来，非日文任务的离线就绪检查一直不完整。
        from .registry import needed_model_keys

        return needed_model_keys(self.cfg)

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

    @staticmethod
    def _job_snapshot_file(work_dir: Path) -> Path:
        return work_dir / "job.json"

    def _resolved_model_paths_snapshot(self) -> dict[str, str]:
        """本次任务要用的每个模型，实际解析到的绝对路径——纯文件系统查询，不加载
        模型。和 _go_offline_if_ready 用的是同一套 resolve_status/_needed_model_keys，
        只是这里要留痕迹而不是只用来判断能不能离线。
        """
        from ..core.model_locator import resolve_status

        result = {}
        for key in self._needed_model_keys():
            st = resolve_status(key, bindings=self.cfg.model_paths)
            if st.path is not None:
                result[key] = str(st.path)
        return result

    def _save_job_snapshot(self, work_dir: Path, resolved_model_paths: dict, stages: dict):
        snapshot = build_job_snapshot(self.cfg, __version__, resolved_model_paths, stages)
        save_json(snapshot, self._job_snapshot_file(work_dir))

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
        # 覆盖保护要在解包/建目录之前做，越早拒绝越好。output_path 为 None 时会走
        # default_output_path，那条路径固定是"<输入>_translated"形式的兄弟路径，
        # 结构上不可能和输入冲突，不需要在算出真实 loaded 之前单独检查；只有用户
        # 显式指定了 output_path 才有冲突的可能，这里能在触碰任何文件前先挡掉。
        # 旧实现是在 load_input()（已经清空/重新解包 _extract）之后才检查，晚了。
        if output_path is not None:
            self._check_output_safe(input_path, Path(output_path))

        self._go_offline_if_ready()
        work_dir = Path(work_dir_override) if work_dir_override else self._work_dir(input_path, output_path)

        # 叶子模块（ocr/translate/render）里用 logging.getLogger(__name__) 记的警告/
        # 提示，之前各自 print(file=sys.stderr)，到不了 UI 日志面板。这里把它们桥接
        # 到 self.log（Runner 自己的调用点、UI 已经接好线的那个回调），并额外挂一份
        # 带时间戳的 <work_dir>/run.log。只在 run() 执行期间挂着，跑完就卸载——
        # 不想让测试里构造几十个 Runner() 都各自往同一个模块级 logger 上堆 handler。
        from ..infra.logging import attach_callback_handler, attach_job_file_handler, detach_handler

        cb_handler = attach_callback_handler(lambda msg: self.log(msg))
        file_handler = attach_job_file_handler(work_dir)
        try:
            return self._run_body(
                input_path, output_path, work_dir, progress, rerender, load_cb, should_cancel
            )
        finally:
            detach_handler(cb_handler)
            detach_handler(file_handler)

    def _run_body(self, input_path: Path, output_path, work_dir: Path, progress, rerender: bool,
                   load_cb, should_cancel: Callable[[], bool] | None) -> JobReport:
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
        trans_dir = ensure_dir(work_dir / "translated")

        pages = self._init_pages(loaded, work_dir, rerender)
        # 磁盘预检必须赶在 _save_pages 之前——否则 pages.json 一写出来，"有没有缓存"
        # 这个判定就永远是"有"，预检对全新任务就永远不执行了。
        self._check_output_space(work_dir, loaded.pages)
        self._save_pages(work_dir, pages, input_path)
        self.log(f"共 {len(pages)} 页，工作目录: {work_dir}")

        # job.json：产生这批结果的完整配置快照 + 各模型实际解析到的路径 + 各阶段
        # 耗时。之前 pages.json 只记了 input 一个字段，出问题后无法知道当时用的
        # 是哪个引擎/阈值/字体/DPI，或者某个模型实际从哪个路径加载——这里补上。
        # 先写一版（此时 stages 还是空的），任一阶段崩溃/被取消，磁盘上至少留着
        # "本次任务请求的是什么配置"这份记录。
        resolved_model_paths = self._resolved_model_paths_snapshot()
        stage_timings: dict[str, dict] = {}
        self._save_job_snapshot(work_dir, resolved_model_paths, stage_timings)

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
            stage_start = time.monotonic()
            from .detect import reading_order

            det = self._get_detector()
            rtl = self.cfg.source_lang == "ja"
            for i, p in enumerate(todo):
                if cancelled():
                    self.log("已停止（检测阶段）")
                    return JobReport.cancelled(pages, output_path)
                try:
                    p.regions = reading_order(det.detect(open_page(p)), rtl=rtl)
                    p.width, p.height = Image.open(page_img[p.image]).size
                    p.status = STATUS_DETECTED
                    p.error = ""
                    p.error_stage = ""
                except Exception as e:
                    p.error = f"检测失败: {e}"
                    p.error_stage = Stage.DETECT.value
                    self.log(f"[警告] {p.image} 检测失败: {e}")
                self._save_pages(work_dir, pages, input_path)
                self._progress(progress, "detect", i + 1, len(todo))
            stage_timings[Stage.DETECT.value] = {
                "duration_s": round(time.monotonic() - stage_start, 2), "page_count": len(todo),
            }
            self._save_job_snapshot(work_dir, resolved_model_paths, stage_timings)

        # 2. OCR
        todo = [p for p in pages if p.status == STATUS_DETECTED]
        if todo:
            stage_start = time.monotonic()
            ocr = self._get_ocr()
            for i, p in enumerate(todo):
                if cancelled():
                    self.log("已停止（OCR 阶段）")
                    return JobReport.cancelled(pages, output_path)
                try:
                    ocr.recognize_regions(open_page(p), p.regions)
                    p.status = STATUS_OCRED
                    p.error = ""
                    p.error_stage = ""
                except Exception as e:
                    p.error = f"OCR 失败: {e}"
                    p.error_stage = Stage.OCR.value
                    self.log(f"[警告] {p.image} OCR 失败: {e}")
                self._save_pages(work_dir, pages, input_path)
                self._progress(progress, "ocr", i + 1, len(todo))
            stage_timings[Stage.OCR.value] = {
                "duration_s": round(time.monotonic() - stage_start, 2), "page_count": len(todo),
            }
            self._save_job_snapshot(work_dir, resolved_model_paths, stage_timings)

        # 3. 翻译
        todo = [p for p in pages if p.status == STATUS_OCRED]
        if todo:
            stage_start = time.monotonic()
            translator = self._get_translator()
            for i, p in enumerate(todo):
                if cancelled():
                    self.log("已停止（翻译阶段）")
                    self._save_pages(work_dir, pages, input_path)
                    return JobReport.cancelled(pages, output_path)
                regions = [r for r in p.regions if r.source_text.strip()]
                if regions:
                    texts = [r.source_text for r in regions]
                    try:
                        results = translator.translate(texts, self.cfg)
                        for r, t in zip(regions, results):
                            r.translated_text = t
                        p.status = STATUS_TRANSLATED
                        p.error = ""
                        p.error_stage = ""
                    except Exception as e:
                        p.error = f"翻译失败: {e}"
                        p.error_stage = Stage.TRANSLATE.value
                        self.log(f"[警告] {p.image} 翻译失败: {e}")
                else:
                    p.status = STATUS_TRANSLATED
                    p.error = ""
                    p.error_stage = ""
                # 翻译是四阶段里最贵的一步（LLM 推理，一页数秒到数十秒），之前这里的
                # _save_pages 在循环外，只在整批翻完后落一次盘——崩溃/断电/强杀会丢掉
                # 这一整批已经跑完的译文，重启后要全部重译。改成逐页落盘，和另外三个
                # 阶段（检测/OCR/渲染）保持一致。
                self._save_pages(work_dir, pages, input_path)
                self._progress(progress, "translate", i + 1, len(todo))
            stage_timings[Stage.TRANSLATE.value] = {
                "duration_s": round(time.monotonic() - stage_start, 2), "page_count": len(todo),
            }
            self._save_job_snapshot(work_dir, resolved_model_paths, stage_timings)

        # 4. 抹字 + 渲染
        todo = [p for p in pages if p.status == STATUS_TRANSLATED]
        if todo:
            stage_start = time.monotonic()
            inpainter = self._get_inpainter()
            for i, p in enumerate(todo):
                if cancelled():
                    self.log("已停止（渲染阶段）")
                    return JobReport.cancelled(pages, output_path)
                try:
                    out_img = render_single_page(open_page(p), p, self.cfg, inpainter, log=self.log)
                    out_path = trans_dir / p.image
                    ensure_dir(out_path.parent)
                    out_img.save(out_path)
                    p.status = STATUS_RENDERED
                    p.error = ""
                    p.error_stage = ""
                except Exception as e:
                    p.error = f"抹字渲染失败: {e}"
                    p.error_stage = Stage.RENDER.value
                    self.log(f"[警告] {p.image} 抹字渲染失败: {e}")
                self._save_pages(work_dir, pages, input_path)
                self._progress(progress, "render", i + 1, len(todo))
            stage_timings[Stage.RENDER.value] = {
                "duration_s": round(time.monotonic() - stage_start, 2), "page_count": len(todo),
            }
            self._save_job_snapshot(work_dir, resolved_model_paths, stage_timings)

        result = pack_output(loaded, trans_dir, output_path, pdf_dpi=self.cfg.pdf_dpi, log=self.log)
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

    def _check_output_space(self, work_dir: Path, input_pages: list[Path]) -> None:
        """开工前预估磁盘需求，明显不够时提前拒绝——不能跑到一半把盘写满，
        让页面损坏/恢复点丢失。只对全新任务（无 pages.json 缓存）强制执行：
        续跑时大部分图已在盘上，剩余增量很小，估算是保守上界，误拦会让人无法续跑。

        磁盘探测/统计失败（网络盘抖动等）时只记日志放行，这是 best-effort 预检，
        不该因为它自身失败就拦住一次本来能跑的任务。
        """
        pages_file = self._pages_file(work_dir)
        if pages_file.exists():
            return
        try:
            required = estimate_output_requirement(input_pages)
            free = available_space(work_dir)
            if required > free:
                human_req = f"{required / (1024 ** 3):.1f} GB"
                human_free = f"{free / (1024 ** 3):.1f} GB"
                raise OutputError(
                    f"输出位置磁盘空间可能不够：预计需要约 {human_req}，当前仅剩 {human_free}。"
                    "请清理磁盘或把输出路径改到空间更大的位置后再试。"
                )
        except OutputError:
            raise
        except OSError as e:
            self.log(f"[警告] 磁盘空间预检失败，跳过：{e}")

    def _progress(self, cb, stage: str, i: int, total: int):
        if cb:
            try:
                cb(i, total, stage)
            except Exception:
                pass
        self.log(f"[{stage}] {i}/{total}")

    @staticmethod
    def _check_output_safe(input_path: Path, output_path: Path):
        """防止输出覆盖原图源。

        路径解析失败时必须拒绝而不是放行——之前 `except Exception: return` 是
        "测不出来就当作安全"，等于这条防线在最需要它的异常路径上失效。测不出来
        就应该拒绝，让用户看到明确的错误而不是可能覆盖原图。
        """
        try:
            ip = input_path.resolve()
            op = output_path.resolve()
        except Exception as e:
            raise OutputError(
                f"无法校验输出路径是否安全（{e}），已阻止。请检查路径是否有效。", technical=str(e)
            ) from e
        if op == ip:
            raise OutputError("输出路径与输入相同，会覆盖原图，已阻止。请指定其它输出路径。")
        if input_path.is_dir() and ip in op.parents:
            raise OutputError("输出目录位于输入目录内，可能污染/覆盖原图，已阻止。请指定输入目录外的路径。")
