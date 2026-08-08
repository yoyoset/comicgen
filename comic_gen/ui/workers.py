"""QThread 工作对象：桥接 core/pipeline 与 Qt 信号。不含业务决策，只做调用转发。"""
from __future__ import annotations

import threading
import time
import traceback

from PySide6 import QtCore

from ..core.errors import ComicGenError, format_job_failure  # noqa: F401 (re-export for callers)
from ..core.job import JobRequest
from ..core.manifest import MODEL_MANIFEST
from ..core.settings import AppSettings

# Runner._progress 按阶段回调，之前 UI 直接拿 i/total 当整体进度——四个阶段各自把
# 进度条从 0 填到 100%，用户体感是"一轮任务进度条填满四次"，而不是一次跑完整个任务。
_STAGE_ORDER = ["detect", "ocr", "translate", "render"]
_STAGE_LABELS = {"detect": "检测", "ocr": "OCR识别", "translate": "翻译", "render": "抹字渲染"}


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分钟"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}小时{minutes}分钟"


def overall_progress(stage: str, i: int, total: int, elapsed_s: float) -> tuple[float, str]:
    """把"当前阶段内的 i/total"换算成跨四阶段单调递增的总进度 + 粗略 ETA。

    纯函数：不碰 Qt/Runner，方便离线测试。ETA 用"目前为止的平均速度直接线性外推"，
    未就绪的前几个百分点（fraction 太小）估算噪声太大，不显示。
    """
    stage_idx = _STAGE_ORDER.index(stage) if stage in _STAGE_ORDER else 0
    stage_fraction = i / max(1, total)
    fraction = (stage_idx + stage_fraction) / len(_STAGE_ORDER)
    desc = f"{_STAGE_LABELS.get(stage, stage)} {i}/{total}"
    if fraction > 0.02 and elapsed_s > 1:
        remaining_s = elapsed_s * (1 - fraction) / fraction
        desc += f"　预计剩余 {_format_duration(remaining_s)}"
    return fraction, desc


class PipelineWorker(QtCore.QObject):
    """跑一次翻译或重渲染任务。run/rerender 由 request.is_rerender 区分，取代此前两个近乎重复的 Worker 类。"""

    log = QtCore.Signal(str)
    progress = QtCore.Signal(float, str)      # (0~1, 描述)
    loadProgress = QtCore.Signal(int, int)
    finished = QtCore.Signal(object)          # JobReport 或异常字符串

    def __init__(self, runner, request: JobRequest, cancel_event):
        super().__init__()
        self.runner = runner
        self.request = request
        self.cancel_event = cancel_event

    @QtCore.Slot()
    def run(self):
        try:
            start = time.monotonic()

            def cb(i, total, stage):
                fraction, desc = overall_progress(stage, i, total, time.monotonic() - start)
                self.progress.emit(fraction, desc)

            def lcb(i, n):
                self.loadProgress.emit(i, n)

            self.runner.log = lambda m: self.log.emit(str(m))
            if self.request.is_rerender:
                report = self.runner.rerender(
                    self.request.input_path, self.request.output_path,
                    progress=cb, should_cancel=self.cancel_event.is_set,
                    work_dir_override=self.request.work_dir_override,
                )
            else:
                report = self.runner.run(
                    self.request.input_path, output_path=self.request.output_path,
                    progress=cb, load_cb=lcb, should_cancel=self.cancel_event.is_set,
                )
            self.finished.emit(report)
        except ComicGenError as e:
            self.runner.log(f"[失败] {e.technical}")  # 技术细节记日志，不丢
            self.finished.emit(format_job_failure(e))
        except Exception as e:
            self.finished.emit(format_job_failure(e))


class ModelWorker(QtCore.QObject):
    """按 key 逐个检查/下载模型：模型清单来自 core.manifest.MODEL_MANIFEST，
    与流水线共用同一份 repo/文件定义。构造各自的引擎对象会顺带触发它们各自的
    自动下载逻辑（HF from_pretrained / hf_hub_download / simple_lama_inpainting）。
    """

    log = QtCore.Signal(str)
    finished = QtCore.Signal(str)

    def __init__(self, settings: AppSettings, keys: list[str], do_download: bool,
                 cancel_event: threading.Event | None = None):
        super().__init__()
        self.settings = settings
        self.keys = keys
        self.do_download = do_download
        # 协作式取消：只在"开始下一个模型之前"检查，不能打断正在进行的单次下载
        # （huggingface_hub/llama_cpp 的下载调用是阻塞的 C 级调用，没有可安全中断的
        # 钩子）。批量补全多个模型时，用户至少能在当前这个下完后叫停，好过完全没有
        # 取消能力、只能干等或强杀进程。
        self.cancel_event = cancel_event

    @QtCore.Slot()
    def run(self):
        from ..core.config import Config
        from ..core.environment import apply_model_dir, is_model_cached

        msgs = []
        try:
            apply_model_dir(self.settings.model_dir)
            bindings = self.settings.model_files

            def build(key: str):
                if key == "detect":
                    from ..pipeline.detect import ComicDetector
                    cfg = Config(model_paths=bindings)
                    ComicDetector(bindings.get("detect") or cfg.detector_repo, cfg.device, cfg.det_threshold)
                elif key == "ocr_ja":
                    from ..pipeline.ocr import OcrEngine
                    OcrEngine(Config(source_lang="ja", model_paths=bindings))
                elif key == "ocr_other":
                    from ..pipeline.ocr import OcrEngine
                    OcrEngine(Config(source_lang="en", model_paths=bindings))
                elif key == "sakura":
                    from ..pipeline.translate import SakuraTranslator
                    SakuraTranslator(Config(translator="sakura", source_lang="ja", model_paths=bindings))
                elif key == "llm":
                    from ..pipeline.translate import GeneralLLMTranslator
                    GeneralLLMTranslator(Config(translator="llm", source_lang="en", model_paths=bindings))
                elif key == "lama":
                    from ..pipeline.inpaint import Inpainter
                    # 用设置页的 device 而不是硬编码 "cuda"——CPU 机器上按 "cuda" 构造
                    # 会报设备错误，把"已经下载成功"误报成下载失败（Inpainter 构造过程
                    # 就是触发权重下载的入口）。
                    Inpainter(self.settings.device, model_path=bindings.get("lama"))

            total = len(self.keys)
            for i, key in enumerate(self.keys, start=1):
                if self.cancel_event is not None and self.cancel_event.is_set():
                    self.log.emit(f"已停止（用户取消，剩余 {total - i + 1} 个未处理）")
                    msgs.append(f"已停止，剩余 {total - i + 1} 个模型未处理")
                    break
                spec = MODEL_MANIFEST[key]
                cached = is_model_cached(key, model_dir=self.settings.model_dir, bindings=bindings)
                if cached:
                    msgs.append(f"[OK] {spec.label}：已有（复用）")
                    self.log.emit(f"({i}/{total}) {spec.label} 已有")
                    continue
                if not self.do_download:
                    msgs.append(f"[缺] {spec.label}（{spec.approx_size}）：未下载（仅检查）")
                    continue
                self.log.emit(f"({i}/{total}) 下载 {spec.label}（{spec.approx_size}）…")
                try:
                    build(key)
                    msgs.append(f"[OK] {spec.label}：已下载")
                    self.log.emit(f"({i}/{total}) {spec.label} 下载完成")
                except Exception as e:
                    msgs.append(f"[FAIL] {spec.label}：{e}")
                    self.log.emit(f"({i}/{total}) {spec.label} 下载失败：{e}")

            self.finished.emit("\n".join(msgs))
        except Exception as e:
            self.finished.emit("失败：" + str(e) + "\n" + traceback.format_exc())


class ReviewRerenderWorker(QtCore.QObject):
    """校对界面"重渲染本页"：真实跑一次 LaMa 抹字 + 渲染，GPU 推理不能放主线程，
    否则点一下按钮界面就冻一次——和其它耗时操作（翻译任务/模型下载）同样的理由。
    """

    finished = QtCore.Signal(str, object)  # image, Path（成功）或 Exception（失败）

    def __init__(self, session, image: str):
        super().__init__()
        self.session = session
        self.image = image

    @QtCore.Slot()
    def run(self):
        try:
            path = self.session.rerender_page(self.image)
            self.finished.emit(self.image, path)
        except Exception as e:
            self.finished.emit(self.image, e)


class ScanWorker(QtCore.QObject):
    """在后台线程算全部 6 个模型的状态 + GGUF 查重，避免共享大目录（几十万文件）时卡 UI。"""

    finished = QtCore.Signal(dict, dict)  # (key -> ModelStatus, key -> list[Path] 重复项)

    def __init__(self, settings: AppSettings):
        super().__init__()
        self.settings = settings

    @QtCore.Slot()
    def run(self):
        from ..core.model_locator import find_duplicates, scan_all_status

        status = scan_all_status(bindings=self.settings.model_files, model_dir=self.settings.model_dir)
        dupes = {
            key: dups
            for key in MODEL_MANIFEST
            if len(dups := find_duplicates(key, model_dir=self.settings.model_dir)) > 1
        }
        self.finished.emit(status, dupes)
