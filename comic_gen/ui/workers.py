"""QThread 工作对象：桥接 core/pipeline 与 Qt 信号。不含业务决策，只做调用转发。"""
from __future__ import annotations

import threading
import time
import traceback

from PySide6 import QtCore

from ..core.errors import ComicGenError, format_job_failure
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
    paused = QtCore.Signal()                  # 真正进入暂停状态（模型已释放，等继续）
    finished = QtCore.Signal(object)          # JobReport 或异常字符串

    def __init__(self, runner, request: JobRequest, cancel_event,
                 pause_requested: threading.Event | None = None,
                 resume_event: threading.Event | None = None):
        super().__init__()
        self.runner = runner
        self.request = request
        self.cancel_event = cancel_event
        # 暂停支持：两个事件都由 JobController 持有（UI 线程 set，工作线程读）。
        # 都传 None 表示该路径不提供暂停能力（批量队列目前就是这样）。
        self.pause_requested = pause_requested
        self.resume_event = resume_event

    # ------------------------------------------------------------ 暂停回调
    def _should_pause(self) -> bool:
        return bool(self.pause_requested and self.pause_requested.is_set())

    def _resume_wait(self) -> None:
        """阻塞直到用户点了「继续」或收到停止请求。事件由 UI 线程的 resume() 设置；
        0.5s 轮询让停止请求最多延迟半秒生效，不会被一次长暂停卡死。"""
        while not self.cancel_event.is_set():
            if self.resume_event.wait(0.5):
                self.resume_event.clear()
                return

    def _on_paused(self) -> None:
        self.paused.emit()

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
            pause_kwargs = {}
            if self.pause_requested is not None and self.resume_event is not None:
                pause_kwargs = {
                    "should_pause": self._should_pause,
                    "resume_wait": self._resume_wait,
                    "on_paused": self._on_paused,
                }
            if self.request.is_rerender:
                report = self.runner.rerender(
                    self.request.input_path, self.request.output_path,
                    progress=cb, should_cancel=self.cancel_event.is_set,
                    work_dir_override=self.request.work_dir_override, **pause_kwargs,
                )
            else:
                report = self.runner.run(
                    self.request.input_path, output_path=self.request.output_path,
                    progress=cb, load_cb=lcb, should_cancel=self.cancel_event.is_set,
                    **pause_kwargs,
                )
            self.finished.emit(report)
        except ComicGenError as e:
            self.runner.log(f"[失败] {e.technical}")  # 技术细节记日志，不丢
            self.finished.emit(format_job_failure(e))
        except Exception as e:
            # 未预期的异常：完整堆栈进 run.log（对话框可能只展示 format_job_failure 的
            # 人话部分，比如 OSError 的友好提示），技术细节不能因为 UI 简化就丢掉。
            self.runner.log(f"[失败] {traceback.format_exc()}")
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
                    # 下载把权重放进了磁盘，但 model_locator 的搜索结果缓存可能还停
                    # 在"没找到"——不清掉的话，下一个任务的离线检查会继续以为缺这个
                    # 模型，翻译引擎又会走一遍联网兜底重下（实测路径，默认缓存模式下
                    # apply_model_dir 不会替我们清缓存）。
                    from ..core.model_locator import clear_cache as _clear_locator_cache
                    _clear_locator_cache()
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


class DiagnosticsWorker(QtCore.QObject):
    """在后台线程跑环境自检（CUDA/llama/UnRAR/磁盘/全部模型状态）。

    detect_environment(check_models=True) 对每个模型走 resolve_status 的本地搜索，
    在共享大模型目录（几十万文件）上实测要几秒——之前 _run_diagnostics 在 UI 线程
    同步调用，每次启动和每次扫描完成后界面都会卡死几秒。这里挪到线程里，结果就绪
    再切回 UI 线程。
    """

    finished = QtCore.Signal(object)  # EnvReport 或 Exception

    def __init__(self, model_dir: str, bindings: dict):
        super().__init__()
        self.model_dir = model_dir
        self.bindings = bindings

    @QtCore.Slot()
    def run(self):
        from ..core.environment import detect_environment

        try:
            report = detect_environment(model_dir=self.model_dir, bindings=self.bindings)
        except Exception as e:  # noqa: BLE001 —— 自检自身失败也要反馈，不能把异常吞进线程
            self.finished.emit(e)
            return
        self.finished.emit(report)


class ComponentWorker(QtCore.QObject):
    """后台下载/安装 GPU 组件（core.gpu_components.install）。

    和 ModelWorker 不同，这里的下载是自己写的分块循环，取消能在 1MB 粒度内生效。
    finished(ok, 人话消息, 技术细节)：失败时人话进界面红字，技术细节进日志框。
    """

    progress = QtCore.Signal(str, object, object)  # 阶段, 已完成字节, 总字节（>2^31，用 object 防溢出）
    log = QtCore.Signal(str)
    finished = QtCore.Signal(bool, str, str)

    def __init__(self, cancel_event: threading.Event):
        super().__init__()
        self.cancel_event = cancel_event

    @QtCore.Slot()
    def run(self):
        from ..core import gpu_components as gc

        t0 = time.monotonic()
        try:
            gc.install(progress=lambda s, d, t: self.progress.emit(s, d, t),
                       log=self.log.emit, cancel=self.cancel_event)
        except gc.InstallError as e:
            self.finished.emit(False, str(e), e.technical)
            return
        except Exception as e:  # noqa: BLE001 —— 未预期异常也要回到界面，不能让线程无声死掉
            self.finished.emit(False, f"安装失败：{e}", traceback.format_exc())
            return
        mins = (time.monotonic() - t0) / 60
        self.finished.emit(True, f"安装完成（用时 {mins:.1f} 分钟），重启程序后生效。", "")
