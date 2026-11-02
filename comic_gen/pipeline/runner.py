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
from ..core.job import JobReport, build_job_snapshot, save_pages
from ..core.models import (
    STATUS_DETECTED,
    STATUS_OCRED,
    STATUS_PENDING,
    STATUS_RENDERED,
    STATUS_TRANSLATED,
    Page,
)
from ..core.utils import (
    atomic_save_image,
    available_space,
    ensure_dir,
    estimate_output_requirement,
    load_json,
    save_json,
)
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

    def _go_offline_if_ready(self) -> bool:
        """本次任务要用的模型都在本地时切离线，免得网络不通时白等 HF 的 etag 重试。

        返回是否真的切了离线——调用方（run）负责在任务结束后调用 restore_offline_mode
        恢复原状，离线只是"本次运行"的状态，不能残留成进程级永久态（否则之后在模型
        管理页下载一个当时没下好的模型会被 offline 挡住）。
        """
        from ..core.environment import enable_offline_mode, is_model_cached

        missing = [k for k in self._needed_model_keys()
                   if not is_model_cached(k, bindings=self.cfg.model_paths)]
        if missing:
            self.log(f"以下模型尚未就位，需要联网获取: {'、'.join(missing)}")
            return False
        enable_offline_mode()
        self.log("模型均已在本地，本次运行不联网")
        return True

    # ------------------------------------------------------------ 中间产物
    @staticmethod
    def _pages_file(work_dir: Path) -> Path:
        return work_dir / "pages.json"

    def _save_pages(self, work_dir: Path, pages: list[Page], input_path: Path | None = None):
        # 与 core.job.save_pages 共用同一份 pages.json 格式实现——校对会话写出的
        # pages.json 和 Runner 写出的必须同构（两边互相读写同一份文件），格式逻辑
        # 只存在一处，改字段不会只有一边生效。
        save_pages(
            self._pages_file(work_dir),
            pages,
            str(input_path.resolve()) if input_path is not None else None,
        )

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
        except Exception as e:
            # 原子写盘保证崩溃不会留下截断 JSON，所以读到失败要么是磁盘异常、要么是
            # 新版本 schema 不兼容旧文件——两者都不能静默当"没有缓存"，那会把整个任务
            # 从零重跑并让用户以为一切正常。仍返回空（没有别的恢复路径），但留下日志。
            self.log(f"[警告] 读取 pages.json 失败，本次按全新任务处理：{e}")
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
            work_dir_override: str | Path | None = None,
            should_pause: Callable[[], bool] | None = None,
            resume_wait: Callable[[], None] | None = None,
            on_paused: Callable[[], None] | None = None) -> JobReport:
        """input_path 必须是最初翻译时的原始输入（pages.json 里记录的指纹），
        而不是翻译产出的工作目录/输出路径——否则 _load_pages 的指纹校验不通过，
        缓存会被当作"输入不一致"丢弃，rerender 实质上会从零重跑。
        跨会话场景（不知道原始输入路径，只知道工作目录）用 work_dir_override 绕开
        _work_dir 的重新推导，直接指定工作目录；此时仍需传入正确的 input_path
        以便重新解包出干净的原图供抹字/渲染使用。

        暂停支持（should_pause/resume_wait/on_paused 三者搭配使用，省略即无暂停能力）：
        - should_pause() 返回 True 表示收到暂停请求；
        - 在暂停点 _wait_if_paused 会先释放全部已加载模型的显存（暂停期间 GPU 空闲），
          再阻塞调用 resume_wait() 等待"继续"；
        - on_paused() 在真正进入暂停状态后调用一次（UI 用它把按钮切成「继续」）；
        - resume_wait() 返回后若 should_cancel() 为 True，视为暂停期间被停止，任务以
          cancelled 收尾（pages.json 已落盘，可重跑续上）。
        """
        input_path = Path(input_path)
        # 覆盖保护要在解包/建目录之前做，越早拒绝越好。output_path 为 None 时会走
        # default_output_path，那条路径固定是"<输入>_translated"形式的兄弟路径，
        # 结构上不可能和输入冲突，不需要在算出真实 loaded 之前单独检查；只有用户
        # 显式指定了 output_path 才有冲突的可能，这里能在触碰任何文件前先挡掉。
        # 旧实现是在 load_input()（已经清空/重新解包 _extract）之后才检查，晚了。
        if output_path is not None:
            self._check_output_safe(input_path, Path(output_path))

        offline_enabled = self._go_offline_if_ready()
        work_dir = Path(work_dir_override) if work_dir_override else self._work_dir(input_path, output_path)

        # 叶子模块（ocr/translate/render）里用 logging.getLogger(__name__) 记的警告/
        # 提示，之前各自 print(file=sys.stderr)，到不了 UI 日志面板。这里把它们桥接
        # 到 self.log（Runner 自己的调用点、UI 已经接好线的那个回调），并额外挂一份
        # 带时间戳的 <work_dir>/run.log。只在 run() 执行期间挂着，跑完就卸载——
        # 不想让测试里构造几十个 Runner() 都各自往同一个模块级 logger 上堆 handler。
        from ..infra.logging import (
            attach_callback_handler,
            attach_job_file_handler,
            detach_handler,
        )

        cb_handler = attach_callback_handler(lambda msg: self.log(msg))
        file_handler = attach_job_file_handler(work_dir)
        try:
            report = self._run_body(
                input_path, output_path, work_dir, progress, rerender, load_cb, should_cancel,
                should_pause, resume_wait, on_paused,
            )
        finally:
            # 离线只是本次运行的状态（_go_offline_if_ready 的语义），跑完恢复原状——
            # 否则残留的 offline env 会挡住之后在模型管理页下载当时还没下好的模型。
            if offline_enabled:
                from ..core.environment import restore_offline_mode

                restore_offline_mode()
            detach_handler(cb_handler)
            detach_handler(file_handler)

        # 清理必须放在日志 handler 卸载之后：run.log 的 FileHandler 在 finally 里才
        # close，此前删除它在 Windows 上会因文件被占用而失败（sharing violation）。
        # 只对完整成功的任务执行——部分完成/失败/取消都保留中间产物，供重跑续上。
        if self.cfg.cleanup_intermediate and isinstance(report, JobReport) \
                and report.status == "ok":
            self._cleanup_intermediates(work_dir)
        return report

    def _run_body(self, input_path: Path, output_path, work_dir: Path, progress, rerender: bool,
                   load_cb, should_cancel: Callable[[], bool] | None,
                   should_pause: Callable[[], bool] | None = None,
                   resume_wait: Callable[[], None] | None = None,
                   on_paused: Callable[[], None] | None = None) -> JobReport:
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
        # 供 run() 在日志 handler 卸载后做中间产物清理（文件夹输入的工作目录里混着
        # 交付物散图，压缩包/PDF 的交付物是输出文件、整个工作目录都可删）。
        self._cleanup_kind = loaded.kind
        if output_path is None:
            output_path = default_output_path(loaded)
        output_path = Path(output_path)
        trans_dir = ensure_dir(work_dir / "translated")

        pages = self._init_pages(loaded, work_dir, rerender)
        # 全新任务（没有任何页面命中缓存）时清空 translated/：里面只可能是上一个
        # 不同输入/旧任务留下的图，全部会被重渲染覆盖，留着只占磁盘、且可能让校对
        # 预览读到错图。续跑/重渲染（有已处理页）时保留——部分页的重渲染图不能丢。
        if not any(p.status != STATUS_PENDING for p in pages):
            stale_trans = work_dir / "translated"
            if stale_trans.is_dir():
                shutil.rmtree(stale_trans)
            trans_dir = ensure_dir(work_dir / "translated")
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

        # 每个阶段间隙检查取消：当前页处理完、落盘后才退出，pages.json 始终完好。
        # 命中取消直接返回，跳过 pack_output（不产出不完整的最终包）。
        # 四阶段的循环骨架完全相同（todo 过滤、取消检查、逐页 try/落盘/进度、阶段计时），
        # 只有"每页做什么"不同——统一走 _run_stage，不再四处各写一份几乎一样的 15 行循环。

        # 1. 检测
        todo = [p for p in pages if p.status == STATUS_PENDING]
        if todo:
            from .detect import reading_order

            rtl = self.cfg.source_lang == "ja"

            def _detect_page(p):
                # 引擎在页内取而不是阶段开头取：暂停释放显存时，_release_models 清空的
                # 是 Runner 的缓存；如果这里还挂着阶段开头拿到的那份引用，释放等于白做
                # （模型对象仍被闭包引用，GC 回收不了）。页内 get_or_rebuild 在未暂停时
                # 命中缓存是 O(1) 的签名比较，无感知开销；暂停后恢复则自然重建。
                det = self._get_detector()
                # 一次打开同时拿到尺寸与推理输入——旧实现 open 两次（detect 一次、
                # 再 Image.open(...).size 一次），每次都要读文件头/解压，白费一页开销。
                img = open_page(p)
                p.width, p.height = img.size
                p.regions = reading_order(det.detect(img), rtl=rtl)
                p.status = STATUS_DETECTED

            report = self._run_stage(
                work_dir, pages, input_path, output_path, todo, Stage.DETECT.value,
                cancel_label="检测", error_prefix="检测失败", progress_label="detect",
                should_cancel=should_cancel, progress=progress,
                resolved_model_paths=resolved_model_paths, stage_timings=stage_timings,
                per_page=_detect_page,
                should_pause=should_pause, resume_wait=resume_wait, on_paused=on_paused,
            )
            if report is not None:
                return report

        # 2. OCR
        todo = [p for p in pages if p.status == STATUS_DETECTED]
        if todo:
            from .detect import suppress_duplicate_text_runs

            def _ocr_page(p):
                ocr = self._get_ocr()
                ocr.recognize_regions(open_page(p), p.regions)
                # 同一段竖排文字被检测成多个 y 重合的窄框时，OCR 出的文本相同——
                # 不去重的话同一句台词会被翻译/渲染重复 N 遍（实测 167.JPEG 重复 7 遍）。
                p.regions = suppress_duplicate_text_runs(p.regions)
                p.status = STATUS_OCRED

            report = self._run_stage(
                work_dir, pages, input_path, output_path, todo, Stage.OCR.value,
                cancel_label="OCR", error_prefix="OCR 失败", progress_label="ocr",
                should_cancel=should_cancel, progress=progress,
                resolved_model_paths=resolved_model_paths, stage_timings=stage_timings,
                per_page=_ocr_page,
                should_pause=should_pause, resume_wait=resume_wait, on_paused=on_paused,
            )
            if report is not None:
                return report

        # 3. 翻译（四阶段里最贵：LLM 推理，一页数秒到数十秒，必须逐页落盘——
        # 崩溃/断电/强杀只丢"当前这一页"，重启后从断点续，不重译已完成的页）
        todo = [p for p in pages if p.status == STATUS_OCRED]
        if todo:
            def _translate_page(p):
                translator = self._get_translator()
                regions = [r for r in p.regions if r.source_text.strip()]
                if regions:
                    results = translator.translate([r.source_text for r in regions], self.cfg)
                    for r, t in zip(regions, results):
                        r.translated_text = t
                # 没有原文的区域也要推进状态，不能卡住后面的渲染阶段
                p.status = STATUS_TRANSLATED

            report = self._run_stage(
                work_dir, pages, input_path, output_path, todo, Stage.TRANSLATE.value,
                cancel_label="翻译", error_prefix="翻译失败", progress_label="translate",
                should_cancel=should_cancel, progress=progress,
                resolved_model_paths=resolved_model_paths, stage_timings=stage_timings,
                per_page=_translate_page,
                should_pause=should_pause, resume_wait=resume_wait, on_paused=on_paused,
            )
            if report is not None:
                return report

        # 4. 抹字 + 渲染
        todo = [p for p in pages if p.status == STATUS_TRANSLATED]
        if todo:
            def _render_page(p):
                inpainter = self._get_inpainter()
                out_img = render_single_page(open_page(p), p, self.cfg, inpainter, log=self.log)
                # 原子写图：进程被杀在写盘中途时不留下截断 PNG 被校对页读到
                atomic_save_image(out_img, trans_dir / p.image)
                p.status = STATUS_RENDERED

            report = self._run_stage(
                work_dir, pages, input_path, output_path, todo, Stage.RENDER.value,
                cancel_label="渲染", error_prefix="抹字渲染失败", progress_label="render",
                should_cancel=should_cancel, progress=progress,
                resolved_model_paths=resolved_model_paths, stage_timings=stage_timings,
                per_page=_render_page,
                should_pause=should_pause, resume_wait=resume_wait, on_paused=on_paused,
            )
            if report is not None:
                return report

        result = pack_output(loaded, trans_dir, output_path, pdf_dpi=self.cfg.pdf_dpi, log=self.log)
        report = JobReport.from_pages(pages, result)
        self.log(f"完成: {result}（{report.summary}）")
        return report

    def _run_stage(self, work_dir, pages, input_path, output_path, todo, stage, cancel_label,
                   error_prefix, progress_label, should_cancel, progress, resolved_model_paths,
                   stage_timings, per_page, should_pause=None, resume_wait=None,
                   on_paused=None) -> JobReport | None:
        """跑一个流水线阶段（todo 里的页面逐一处理）。

        返回取消时的 JobReport（调用方直接 return），正常完成返回 None。
        per_page(p) 负责一页在这个阶段的真正工作并置目标状态；异常在这里统一落
        error/error_stage（core.errors.Stage 的字符串值），并逐页落盘——这是断点续跑
        的根基，四个阶段共用同一套骨架，改取消/暂停/落盘/计时要同步改一处而不是四处。
        """
        if not todo:
            return None
        stage_start = time.monotonic()
        for i, p in enumerate(todo):
            if self._cancelled(should_cancel):
                self.log(f"已停止（{cancel_label}阶段）")
                # 取消前防御性再落一次盘：已处理页的进度必须可恢复
                self._save_pages(work_dir, pages, input_path)
                return JobReport.cancelled(pages, output_path)
            if self._wait_if_paused(should_pause, resume_wait, should_cancel, on_paused):
                # 暂停期间收到停止请求：与取消同路径收尾（pages.json 在上一个循环里
                # 已落盘，这里再防御性落一次）
                self._save_pages(work_dir, pages, input_path)
                return JobReport.cancelled(pages, output_path)
            try:
                per_page(p)
                p.error = ""
                p.error_stage = ""
            except Exception as e:
                p.error = f"{error_prefix}: {e}"
                p.error_stage = stage
                self.log(f"[警告] {p.image} {error_prefix}: {e}")
            self._save_pages(work_dir, pages, input_path)
            self._progress(progress, progress_label, i + 1, len(todo))
        stage_timings[stage] = {
            "duration_s": round(time.monotonic() - stage_start, 2), "page_count": len(todo),
        }
        self._save_job_snapshot(work_dir, resolved_model_paths, stage_timings)
        return None

    def _cleanup_intermediates(self, work_dir: Path) -> None:
        """删除中间产物，只保留最终交付物（勾选 cfg.cleanup_intermediate 且任务完整
        成功时由 run() 调用）。

        文件夹输入：交付物就是工作目录根目录的散图（pack_output 已复制好），只删
        translated/、_extract/ 与 pages.json/job.json/run.log 这些中间/状态文件；
        压缩包/PDF 输入：交付物是输出文件，整个工作目录（解包原图 + 渲染图 + 状态
        文件）都是中间产物，整体删掉。

        清理是 best-effort：个别文件被占用删不掉只记日志警告，不能让任务在成功后
        反而报失败。
        """
        try:
            if getattr(self, "_cleanup_kind", "folder") == "folder":
                for sub in ("translated", "_extract"):
                    d = work_dir / sub
                    if d.is_dir():
                        shutil.rmtree(d)
                for name in ("pages.json", "job.json", "run.log"):
                    (work_dir / name).unlink(missing_ok=True)
            elif work_dir.is_dir():
                shutil.rmtree(work_dir)
            self.log("[清理] 任务完成，已删除中间产物，仅保留最终交付物")
        except OSError as e:
            self.log(f"[警告] 清理中间产物失败（已保留中间文件，可手动删除）：{e}")

    @staticmethod
    def _cancelled(should_cancel: Callable[[], bool] | None) -> bool:
        """should_cancel 回调本身抛异常时按"未取消"处理——取消探测失败不该让任务崩溃。"""
        if should_cancel is None:
            return False
        try:
            return bool(should_cancel())
        except Exception:
            return False

    def _wait_if_paused(self, should_pause: Callable[[], bool] | None,
                        resume_wait: Callable[[], None] | None,
                        should_cancel: Callable[[], bool] | None,
                        on_paused: Callable[[], None] | None) -> bool:
        """暂停点（在每页处理前调用）：收到暂停请求就释放显存并阻塞等「继续」。

        返回 True 表示暂停期间被停止（调用方以 cancelled 收尾），False 表示已恢复。
        模型释放见 release_models——把缓存清空，恢复后下一页的 get_or_rebuild 会重建。
        """
        if should_pause is None or resume_wait is None:
            return False
        try:
            paused = bool(should_pause())
        except Exception:
            return False
        if not paused:
            return False
        if on_paused is not None:
            try:
                on_paused()
            except Exception:
                pass
        self.log("已暂停，正在释放显卡资源…")
        self.release_models()
        self.log("已暂停，显卡资源已释放，可以放心做其它需要 GPU 的事。")
        try:
            resume_wait()
        except Exception:
            pass
        if self._cancelled(should_cancel):
            self.log("已停止（暂停期间收到停止请求）")
            return True
        self.log("已恢复运行，重新加载模型…")
        return False

    def release_models(self) -> None:
        """卸载全部已加载的模型并释放显存（暂停时调用，恢复后按需重建）。

        llama.cpp 翻译引擎有显式 close()（释放 C 层分配的模型/上下文显存）；
        torch 系模型（detect/OCR/LaMa）靠丢引用 + gc.collect() + torch.cuda.empty_cache()。
        清空的是 Runner 各引擎缓存，阶段闭包不得再持有旧引擎引用——_run_body 里
        per_page 都在页内重新 get_or_rebuild，暂停释放后才能被真正回收。
        """
        for cache in (self._detector_cache, self._ocr_cache,
                      self._inpainter_cache, self._translator_cache):
            engine = getattr(cache, "_value", None)
            if engine is not None:
                self._close_engine(engine)
            cache.clear()
        import gc

        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def _close_engine(self, engine) -> None:
        """对带显式资源句柄的引擎做释放前的关闭。只认 `.llm`（llama.cpp 的 Llama），
        torch 系模型没有需要主动 close 的句柄，交给 GC + empty_cache。"""
        llm = getattr(engine, "llm", None)
        close = getattr(llm, "close", None)
        if callable(close):
            try:
                close()
            except Exception as e:
                self.log(f"[警告] 释放翻译引擎显存失败：{e}")

    # ------------------------------------------------------------ 重渲染（译后编辑）
    def rerender(self, input_path: str | Path, output_path: str | Path | None = None,
                 progress=None, should_cancel: Callable[[], bool] | None = None,
                 work_dir_override: str | Path | None = None,
                 should_pause: Callable[[], bool] | None = None,
                 resume_wait: Callable[[], None] | None = None,
                 on_paused: Callable[[], None] | None = None) -> JobReport:
        return self.run(input_path, output_path, progress=progress, rerender=True,
                         should_cancel=should_cancel, work_dir_override=work_dir_override,
                         should_pause=should_pause, resume_wait=resume_wait, on_paused=on_paused)

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
