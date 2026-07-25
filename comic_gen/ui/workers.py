"""QThread 工作对象：桥接 core/pipeline 与 Qt 信号。不含业务决策，只做调用转发。"""
from __future__ import annotations

import traceback

from PySide6 import QtCore

from ..core.job import JobRequest
from ..core.manifest import MODEL_MANIFEST
from ..core.settings import AppSettings


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
            def cb(i, total, stage):
                self.progress.emit(i / max(1, total), f"{stage} {i}/{total}")

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
        except Exception as e:
            self.finished.emit(f"失败：{e}\n{traceback.format_exc()}")


class ModelWorker(QtCore.QObject):
    """按 key 逐个检查/下载模型：模型清单来自 core.manifest.MODEL_MANIFEST，
    与流水线共用同一份 repo/文件定义。构造各自的引擎对象会顺带触发它们各自的
    自动下载逻辑（HF from_pretrained / hf_hub_download / simple_lama_inpainting）。
    """

    log = QtCore.Signal(str)
    finished = QtCore.Signal(str)

    def __init__(self, settings: AppSettings, keys: list[str], do_download: bool):
        super().__init__()
        self.settings = settings
        self.keys = keys
        self.do_download = do_download

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
                    Inpainter("cuda", model_path=bindings.get("lama"))

            for key in self.keys:
                spec = MODEL_MANIFEST[key]
                cached = is_model_cached(key, model_dir=self.settings.model_dir, bindings=bindings)
                if cached:
                    msgs.append(f"[OK] {spec.label}：已有（复用）")
                    self.log.emit(f"{spec.label} 已有")
                    continue
                if not self.do_download:
                    msgs.append(f"[缺] {spec.label}（{spec.approx_size}）：未下载（仅检查）")
                    continue
                self.log.emit(f"下载 {spec.label}（{spec.approx_size}）…")
                try:
                    build(key)
                    msgs.append(f"[OK] {spec.label}：已下载")
                except Exception as e:
                    msgs.append(f"[FAIL] {spec.label}：{e}")

            self.finished.emit("\n".join(msgs))
        except Exception as e:
            self.finished.emit("失败：" + str(e) + "\n" + traceback.format_exc())


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
