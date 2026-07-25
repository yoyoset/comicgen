"""QThread 工作对象：桥接 core/pipeline 与 Qt 信号。不含业务决策，只做调用转发。"""
from __future__ import annotations

import sys
import traceback

from PySide6 import QtCore

from ..core.job import JobRequest
from ..core.manifest import MODEL_MANIFEST


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
    """模型检查/下载：模型清单来自 core.manifest.MODEL_MANIFEST，与流水线共用同一份 repo 定义。"""

    log = QtCore.Signal(str)
    finished = QtCore.Signal(str)

    def __init__(self, model_dir: str, want_sakura: bool, want_llm: bool, do_download: bool):
        super().__init__()
        self.model_dir = model_dir
        self.want_sakura = want_sakura
        self.want_llm = want_llm
        self.do_download = do_download

    @QtCore.Slot()
    def run(self):
        from ..core.environment import apply_model_dir, is_model_cached

        msgs = []
        try:
            apply_model_dir(self.model_dir)
            plan_keys = ["detect", "ocr_ja"]
            if self.want_sakura:
                plan_keys.append("sakura")
            if self.want_llm:
                plan_keys.append("llm")

            def build(key: str):
                from ..core.config import Config
                if key == "detect":
                    from ..pipeline.detect import ComicDetector
                    cfg = Config()
                    ComicDetector(cfg.detector_repo, cfg.device, cfg.det_threshold)
                elif key == "ocr_ja":
                    from ..pipeline.ocr import OcrEngine
                    OcrEngine(Config(source_lang="ja"))
                elif key == "sakura":
                    from ..pipeline.translate import SakuraTranslator
                    SakuraTranslator(Config(translator="sakura", source_lang="ja"))
                elif key == "llm":
                    from ..pipeline.translate import GeneralLLMTranslator
                    GeneralLLMTranslator(Config(translator="llm", source_lang="en"))

            for key in plan_keys:
                spec = MODEL_MANIFEST[key]
                cached = is_model_cached(key)
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

            # LaMa：非 HF repo，走内置下载
            lama_spec = MODEL_MANIFEST["lama"]
            try:
                from ..pipeline.inpaint import Inpainter
                Inpainter("cuda")
                msgs.append(f"[OK] {lama_spec.label}：已就绪（系统缓存）")
            except Exception as e:
                msgs.append(f"[FAIL] {lama_spec.label}：{e}")

            loc = (self.model_dir or "").strip().strip('"') or (
                "exe同级 models/hf" if getattr(sys, "frozen", False) else "默认 HF 缓存"
            )
            self.finished.emit("模型目录：" + loc + "\n" + "\n".join(msgs))
        except Exception as e:
            self.finished.emit("失败：" + str(e) + "\n" + traceback.format_exc())
