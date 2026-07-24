"""comic-gen 桌面界面（PySide6/Qt）。

原生窗口、原生文件/文件夹选择（不经浏览器/temp）、后台线程跑流水线。
流水线全部复用 comic_gen.pipeline。
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.config import Config
from ..pipeline.runner import Runner

_LANGS = [("日文", "ja"), ("英文", "en"), ("简体中文", "zh"), ("其它", "other")]
_ENGINES = [("Sakura（本地，日文）", "sakura"), ("DeepSeek（API）", "deepseek"), ("Mock（测试）", "mock")]
_DIRS = [("自动", "auto"), ("横排", "horizontal"), ("竖排", "vertical")]

# pip 命令，用于在 huggingface_hub 常量中重定向缓存目录
def _set_cache(model_dir: str):
    base = (model_dir or "").strip().strip('"') or None
    if not base:
        return
    Path(base).mkdir(parents=True, exist_ok=True)
    hub = str(Path(base) / "hub")
    os.environ["HF_HOME"] = base
    os.environ["HF_HUB_CACHE"] = hub
    try:
        import huggingface_hub.constants as hc

        hc.HF_HUB_CACHE = hub
        if hasattr(hc, "HF_HOME"):
            hc.HF_HOME = base
    except Exception:
        pass


def _check_repo(repo: str) -> str:
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(repo, local_files_only=True)
        return "已有"
    except Exception:
        return "缺失"


# ============================================================ 后台工作线程
class _Worker(QtCore.QObject):
    log = QtCore.Signal(str)
    progress = QtCore.Signal(float, str)   # (0~1, 描述)
    finished = QtCore.Signal(object)       # output_path 或异常字符串
    load_progress = QtCore.Signal(int, int)

    def __init__(self, runner, src, out_arg):
        super().__init__()
        self.runner = runner
        self.src = src
        self.out_arg = out_arg

    @QtCore.Slot()
    def run(self):
        try:
            def cb(i, total, stage):
                self.progress.emit(i / max(1, total), f"{stage} {i}/{total}")
            def lcb(i, n):
                self.load_progress.emit(i, n)
            self.runner.log = lambda m: self.log.emit(str(m))
            out = self.runner.run(self.src, output_path=self.out_arg, progress=cb, load_cb=lcb)
            self.finished.emit(Path(out))
        except Exception as e:
            self.finished.emit(f"失败：{e}\n{traceback.format_exc()}")


class _RerenderWorker(QtCore.QObject):
    log = QtCore.Signal(str)
    progress = QtCore.Signal(float, str)
    finished = QtCore.Signal(object)

    def __init__(self, runner, work_dir):
        super().__init__()
        self.runner = runner
        self.work_dir = work_dir

    @QtCore.Slot()
    def run(self):
        try:
            def cb(i, total, stage):
                self.progress.emit(i / max(1, total), f"{stage} {i}/{total}")
            self.runner.log = lambda m: self.log.emit(str(m))
            out = self.runner.rerender(self.work_dir, progress=cb)
            self.finished.emit(Path(out))
        except Exception as e:
            self.finished.emit(f"失败：{e}\n{traceback.format_exc()}")


class _ModelWorker(QtCore.QObject):
    log = QtCore.Signal(str)
    finished = QtCore.Signal(str)

    def __init__(self, model_dir, want_sakura, do_download):
        super().__init__()
        self.model_dir = model_dir
        self.want_sakura = want_sakura
        self.do_download = do_download

    @QtCore.Slot()
    def run(self):
        msgs = []
        try:
            _set_cache(self.model_dir)
            plan = [
                ("检测模型", "ogkalu/comic-text-and-bubble-detector", "detect"),
                ("OCR manga-ocr", "kha-white/manga-ocr-base", "ocr"),
            ]
            if self.want_sakura:
                plan.append(("Sakura 日译中", "SakuraLLM/Sakura-7B-Qwen2.5-v1.0-GGUF", "sakura"))

            def build(kind):
                if kind == "detect":
                    from ..pipeline.detect import ComicDetector
                    cfg = Config()
                    ComicDetector(cfg.detector_repo, cfg.device, cfg.det_threshold)
                elif kind == "ocr":
                    from ..pipeline.ocr import OcrEngine
                    OcrEngine(Config(source_lang="ja"))
                elif kind == "sakura":
                    from ..pipeline.translate import SakuraTranslator
                    SakuraTranslator(Config(translator="sakura", source_lang="ja"))

            for name, repo, kind in plan:
                status = _check_repo(repo)
                if status == "已有":
                    msgs.append(f"[OK] {name}：已有（复用）")
                    self.log.emit(f"{name} 已有")
                    continue
                if not self.do_download:
                    msgs.append(f"[缺] {name}：未下载（仅检查）")
                    continue
                self.log.emit(f"下载 {name} …")
                try:
                    build(kind)
                    msgs.append(f"[OK] {name}：已下载")
                except Exception as e:
                    msgs.append(f"[FAIL] {name}：{e}")
            # LaMa
            try:
                from ..pipeline.inpaint import Inpainter
                Inpainter("cuda")
                msgs.append("[OK] 抹字 LaMa：已就绪（系统缓存）")
            except Exception as e:
                msgs.append(f"[FAIL] LaMa：{e}")
            loc = (self.model_dir or "").strip().strip('"') or ("exe同级 models/hf" if getattr(sys, "frozen", False) else "默认 HF 缓存")
            self.finished.emit("模型目录：" + loc + "\n" + "\n".join(msgs))
        except Exception as e:
            self.finished.emit("失败：" + str(e) + "\n" + traceback.format_exc())


# ============================================================ 主窗口
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("comic-gen 漫画自动翻译")
        self.resize(1100, 720)
        self._runner = None
        self._thread = None
        self._worker = None

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._build_translate_tab(), "翻译")
        tabs.addTab(self._build_model_tab(), "模型管理")
        self.setCentralWidget(tabs)

        self._update_rec_engine()

        # 默认模型目录：冻结时用 exe 同级，否则留空（用全局）
        if getattr(sys, "frozen", False):
            base = os.path.dirname(sys.executable)
            default_model = os.path.join(base, "models", "hf")
            os.makedirs(default_model, exist_ok=True)
            _set_cache(default_model)
            self.model_dir_edit.setText(default_model)

    # ---------------------------------------------------- 翻译页
    def _build_translate_tab(self):
        page = QtWidgets.QWidget()
        lay = QtWidgets.QFormLayout()
        page.setLayout(lay)

        self.input_edit = QtWidgets.QLineEdit()
        lay.addRow("输入路径", self._input_row())

        self.output_edit = QtWidgets.QLineEdit()
        self.output_edit.setPlaceholderText("留空 = 输入同级 _translated")
        lay.addRow("输出路径", self._row(self.output_edit, "save"))

        self.model_dir_edit = QtWidgets.QLineEdit()
        self.model_dir_edit.setPlaceholderText("留空 = 默认（见模型管理）")
        lay.addRow("模型目录", self._row(self.model_dir_edit, "folder"))

        self.lang_combo = QtWidgets.QComboBox()
        for n, v in _LANGS:
            self.lang_combo.addItem(n, v)
        self.lang_combo.currentIndexChanged.connect(self._update_rec_engine)
        lay.addRow("源语言", self.lang_combo)

        self.engine_combo = QtWidgets.QComboBox()
        for n, v in _ENGINES:
            self.engine_combo.addItem(n, v)
        lay.addRow("翻译引擎", self.engine_combo)

        self.dir_combo = QtWidgets.QComboBox()
        for n, v in _DIRS:
            self.dir_combo.addItem(n, v)
        lay.addRow("排版方向", self.dir_combo)

        self.key_edit = QtWidgets.QLineEdit()
        self.key_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.key_edit.setPlaceholderText("仅 DeepSeek 引擎需要")
        lay.addRow("DeepSeek API Key", self.key_edit)

        self.dpi_spin = QtWidgets.QSpinBox()
        self.dpi_spin.setRange(72, 600)
        self.dpi_spin.setValue(150)
        self.dpi_spin.setSuffix(" DPI")
        self.dpi_spin.setToolTip("PDF 渲染分辨率；150 快且够用，300 画质好但慢")
        lay.addRow("PDF DPI", self.dpi_spin)

        self.start_btn = QtWidgets.QPushButton("开始翻译")
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn = QtWidgets.QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        self.rerender_btn = QtWidgets.QPushButton("重渲染")
        self.rerender_btn.setToolTip("编辑 pages.json 后重新渲染译文（需选择上次翻译的输出目录）")
        self.rerender_btn.clicked.connect(self._on_rerender)
        br = QtWidgets.QHBoxLayout()
        br.addWidget(self.start_btn, 2)
        br.addWidget(self.stop_btn, 1)
        br.addWidget(self.rerender_btn, 1)
        lay.addRow(br)

        self.progress_bar = QtWidgets.QProgressBar()
        self.status_lbl = QtWidgets.QLabel("就绪")
        pl = QtWidgets.QHBoxLayout()
        pl.addWidget(self.progress_bar, 1)
        pl.addWidget(self.status_lbl)
        lay.addRow(pl)

        self.log_edit = QtWidgets.QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.preview_list = QtWidgets.QListWidget()
        self.preview_list.setViewMode(QtWidgets.QListWidget.IconMode)
        self.preview_list.setIconSize(QtCore.QSize(160, 220))
        self.preview_list.setResizeMode(QtWidgets.QListWidget.Adjust)
        sp = QtWidgets.QSplitter()
        sp.addWidget(self.log_edit)
        sp.addWidget(self.preview_list)
        sp.setSizes([300, 600])
        lay.addRow(sp)
        return page

    def _row(self, edit, mode):
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        b = QtWidgets.QPushButton("浏览…")
        b.clicked.connect(lambda: self._pick(edit, mode))
        h.addWidget(b)
        return w

    def _input_row(self):
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.input_edit, 1)
        b_dir = QtWidgets.QPushButton("选文件夹…")
        b_dir.setToolTip("选漫画图片文件夹")
        b_dir.clicked.connect(lambda: self._pick(self.input_edit, "folder"))
        b_file = QtWidgets.QPushButton("选文件…")
        b_file.setToolTip("选 ZIP / CBZ / EPUB / PDF / RAR / CBR")
        b_file.clicked.connect(lambda: self._pick(self.input_edit, "open"))
        h.addWidget(b_dir)
        h.addWidget(b_file)
        return w

    def _pick(self, edit, mode):
        if mode == "folder":
            d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择文件夹")
            if d:
                edit.setText(d)
        elif mode == "open":
            d, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "选择文件", "",
                "漫画文件 (*.zip *.cbz *.epub *.pdf *.rar *.cbr);;所有文件 (*.*)")
            if d:
                edit.setText(d)
        elif mode == "save":
            d, _ = QtWidgets.QFileDialog.getSaveFileName(self, "选择输出", edit.text() or "", "所有文件 (*.*)")
            if d:
                edit.setText(d)

    def _update_rec_engine(self):
        lang = self.lang_combo.currentData()
        rec = "sakura" if lang == "ja" else "deepseek"
        for i in range(self.engine_combo.count()):
            if self.engine_combo.itemData(i) == rec:
                self.engine_combo.setCurrentIndex(i)
                break

    # ---------------------------------------------------- 模型页
    def _build_model_tab(self):
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        tip = QtWidgets.QLabel(
            "模型可跨项目复用：填写已有 HF 缓存目录（如本机 F:\\hf_cache）即可复用，不必下载。\n"
            "检测(~170MB)/OCR(~450MB)/Sakura(~4.3GB) 走指定目录；抹字 LaMa 走系统缓存（~200MB）。"
        )
        tip.setWordWrap(True)
        lay.addWidget(tip)

        row = QtWidgets.QHBoxLayout()
        self.mgr_dir_edit = QtWidgets.QLineEdit()
        self.mgr_dir_edit.setPlaceholderText(r"如 F:\hf_cache 或 F:\manga\models")
        row.addWidget(QtWidgets.QLabel("目录"))
        row.addWidget(self.mgr_dir_edit, 1)
        b = QtWidgets.QPushButton("浏览…")
        b.clicked.connect(lambda: self._pick(self.mgr_dir_edit, "folder"))
        row.addWidget(b)
        lay.addLayout(row)

        self.sakura_check = QtWidgets.QCheckBox("涉及 Sakura（日译中，~4.3GB）")
        lay.addWidget(self.sakura_check)

        brow = QtWidgets.QHBoxLayout()
        self.check_btn = QtWidgets.QPushButton("仅检查现有")
        self.check_btn.clicked.connect(lambda: self._on_models(False))
        self.dl_btn = QtWidgets.QPushButton("补全缺失模型")
        self.dl_btn.clicked.connect(lambda: self._on_models(True))
        brow.addWidget(self.check_btn)
        brow.addWidget(self.dl_btn)
        lay.addLayout(brow)

        self.mgr_status = QtWidgets.QPlainTextEdit()
        self.mgr_status.setReadOnly(True)
        lay.addWidget(self.mgr_status)
        return page

    def _on_models(self, do_download):
        d = self.mgr_dir_edit.text().strip()
        if not d:
            d = self.model_dir_edit.text().strip()
        want = self.sakura_check.isChecked()
        self._start_model_worker(d, want, do_download)

    def _start_model_worker(self, model_dir, want_sakura, do_download):
        self.check_btn.setEnabled(False)
        self.dl_btn.setEnabled(False)
        self._m_thread = QtCore.QThread()
        self._m_worker = _ModelWorker(model_dir, want_sakura, do_download)
        self._m_worker.moveToThread(self._m_thread)
        self._m_thread.started.connect(self._m_worker.run)
        self._m_worker.finished.connect(self._on_model_done)
        self._m_worker.finished.connect(self._m_thread.quit)
        self._m_thread.start()

    def _on_model_done(self, text):
        self.mgr_status.setPlainText(text)
        self.check_btn.setEnabled(True)
        self.dl_btn.setEnabled(True)

    # ---------------------------------------------------- 翻译执行
    def _on_start(self):
        src = self.input_edit.text().strip().strip('"')
        if not src:
            QtWidgets.QMessageBox.warning(self, "提示", "请先选择输入")
            return
        out_arg = self.output_edit.text().strip().strip('"') or None
        model_dir = self.model_dir_edit.text().strip() or None
        lang = self.lang_combo.currentData()
        engine = self.engine_combo.currentData()
        if engine == "sakura" and lang != "ja":
            QtWidgets.QMessageBox.warning(self, "引擎不匹配", "Sakura 为日文专用，请改源语言或用 DeepSeek。")
            return
        if engine == "deepseek" and not (self.key_edit.text().strip() or os.environ.get("DEEPSEEK_API_KEY")):
            QtWidgets.QMessageBox.warning(self, "缺 API Key", "请填写 DeepSeek API Key。")
            return
        _set_cache(model_dir or "")
        cfg = Config(translator=engine, direction=self.dir_combo.currentData(),
                     source_lang=lang, deepseek_api_key=self.key_edit.text().strip(),
                     pdf_dpi=self.dpi_spin.value())
        self._get_runner(cfg)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress_bar.setValue(0)
        self.status_lbl.setText("启动中…")
        self.log_edit.clear()
        self._thread = QtCore.QThread()
        self._worker = _Worker(self._runner, src, out_arg)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self._on_log)
        self._worker.progress.connect(self._on_progress)
        self._worker.load_progress.connect(self._on_load)
        self._worker.finished.connect(self._on_done)
        self._worker.finished.connect(self._thread.quit)
        self._thread.start()

    def _get_runner(self, cfg):
        if self._runner is None:
            self._runner = Runner(cfg)
        else:
            self._runner.cfg = cfg

    def _on_log(self, msg):
        self.log_edit.appendPlainText(msg)

    def _on_progress(self, f, desc):
        self.progress_bar.setValue(int(f * 100))
        self.status_lbl.setText(desc)

    def _on_load(self, i, n):
        self.status_lbl.setText(f"读取文档 {i}/{n} 页")
        self.progress_bar.setValue(int(min(1.0, 0.3 * i / max(1, n)) * 100))

    def _on_done(self, result):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if isinstance(result, Path):
            self.status_lbl.setText("完成：" + str(result))
            self._load_preview(result)
            QtWidgets.QMessageBox.information(self, "完成", f"输出：{result}")
        else:
            self.status_lbl.setText("失败")
            QtWidgets.QMessageBox.critical(self, "失败", result)

    def _load_preview(self, out):
        self.preview_list.clear()
        d = Path(out)
        if d.is_file():
            d = d.parent / d.stem
        trans = d / "translated"
        if not trans.exists():
            return
        imgs = sorted([p for p in trans.rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")])[:24]
        for p in imgs:
            it = QtWidgets.QListWidgetItem(QtGui.QIcon(str(p)), p.name)
            self.preview_list.addItem(it)

    def _on_rerender(self):
        """编辑 pages.json 后重新渲染译文。"""
        out = self.output_edit.text().strip().strip('"')
        if not out:
            d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择翻译输出目录（包含 pages.json）")
            if not d:
                return
            out = d
        out_path = Path(out)
        pages_file = out_path / "pages.json" if out_path.is_dir() else out_path.parent / out_path.stem / "pages.json"
        if not pages_file.exists():
            QtWidgets.QMessageBox.warning(self, "找不到 pages.json", f"输出目录或其上级应包含 pages.json：{pages_file}")
            return
        model_dir = self.model_dir_edit.text().strip() or None
        _set_cache(model_dir or "")
        cfg = Config(translator=self.engine_combo.currentData(),
                     source_lang=self.lang_combo.currentData(),
                     deepseek_api_key=self.key_edit.text().strip(),
                     pdf_dpi=self.dpi_spin.value())
        self._get_runner(cfg)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.rerender_btn.setEnabled(False)
        self.progress_bar.setValue(0)
        self.status_lbl.setText("重渲染中…")
        self.log_edit.clear()
        self._thread = QtCore.QThread()
        self._worker = _RerenderWorker(self._runner, out_path)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self._on_log)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_done)
        self._worker.finished.connect(self._thread.quit)
        self._thread.start()

    def _on_stop(self):
        for t in (self._thread, self._m_thread):
            if t and t.isRunning():
                t.quit()
                t.wait(3000)
                if t.isRunning():
                    t.terminate()
                    t.wait(1000)
        self.start_btn.setEnabled(True)
        self.rerender_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText("已停止")

    def closeEvent(self, event):
        self._on_stop()
        super().closeEvent(event)


# ============================================================ 入口
def main():
    if getattr(sys, "frozen", False):
        # EXE：无控制台时把日志写文件，便于排错
        if sys.stdout is None:
            log = open(os.path.join(os.path.dirname(sys.executable), "comicgen.log"), "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stderr = log
        try:
            from ..pipeline._cuda_runtime import preload_cuda_runtime
            preload_cuda_runtime()
        except Exception:
            pass
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()