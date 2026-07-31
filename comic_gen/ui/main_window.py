"""组合根：只负责造零件、接线、菜单/状态栏。不含业务决策——那些在 JobController/core 里。"""
from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.environment import apply_model_dir, detect_environment
from ..core.job import JobReport, JobRequest, read_original_input, resolve_pages_json
from ..core.manifest import MODEL_MANIFEST
from ..core.settings import AppSettings
from ..pipeline.translate import engine_lang_conflict
from .job_controller import STATE_RUNNING, JobController
from .views.diagnostics_view import DiagnosticsView
from .views.first_run_dialog import FirstRunDialog
from .views.model_view import ModelView
from .views.settings_view import SettingsView
from .views.translate_view import TranslateView
from .workers import ModelWorker, ScanWorker


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("comic-gen 漫画自动翻译")
        self.resize(1100, 760)

        self.settings = AppSettings.load()
        self._last_completed_request: JobRequest | None = None
        self._last_completed_output_path: Path | None = None  # JobReport.output_path，见 _on_rerender 注释
        self._pending_request: JobRequest | None = None  # 当前提交给 controller 的请求，finished 时用于记录
        self._m_thread: QtCore.QThread | None = None
        self._m_worker: ModelWorker | None = None
        self._s_thread: QtCore.QThread | None = None
        self._s_worker: ScanWorker | None = None

        self.translate_view = TranslateView()
        self.settings_view = SettingsView()
        self.model_view = ModelView()
        self.diagnostics_view = DiagnosticsView()

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self.translate_view, "翻译")
        tabs.addTab(self.settings_view, "设置")
        tabs.addTab(self.model_view, "模型管理")
        tabs.addTab(self.diagnostics_view, "环境自检")
        self.setCentralWidget(tabs)

        self.job_controller = JobController()

        self._build_menu()
        self._build_status_bar()
        self._wire_signals()
        if not self.settings.setup_completed:
            FirstRunDialog(self.settings, self).exec()
        self._load_settings_into_views()
        self._restore_window_geometry()

        QtCore.QTimer.singleShot(50, self._run_diagnostics)

    # ------------------------------------------------------------ 搭建
    def _build_menu(self):
        m_file = self.menuBar().addMenu("文件(&F)")
        act_open = m_file.addAction("打开输入…")
        act_open.triggered.connect(self._menu_open_input)
        m_file.addSeparator()
        act_quit = m_file.addAction("退出")
        act_quit.triggered.connect(self.close)

        m_view = self.menuBar().addMenu("查看(&V)")
        act_settings = m_view.addAction("设置")
        act_settings.triggered.connect(lambda: self.centralWidget().setCurrentWidget(self.settings_view))
        act_diag = m_view.addAction("环境自检")
        act_diag.triggered.connect(lambda: self.centralWidget().setCurrentWidget(self.diagnostics_view))

        m_help = self.menuBar().addMenu("帮助(&H)")
        act_about = m_help.addAction("关于")
        act_about.triggered.connect(self._show_about)

    def _build_status_bar(self):
        bar = self.statusBar()
        self.status_lbl = QtWidgets.QLabel("就绪")
        bar.addWidget(self.status_lbl, 1)
        self.status_progress = QtWidgets.QProgressBar()
        self.status_progress.setMaximumWidth(220)
        self.status_progress.setVisible(False)
        bar.addPermanentWidget(self.status_progress)

    def _wire_signals(self):
        self.translate_view.startRequested.connect(self._on_start)
        self.translate_view.stopRequested.connect(self.job_controller.cancel)
        self.translate_view.rerenderRequested.connect(self._on_rerender)

        self.settings_view.changed.connect(self._on_settings_changed)

        self.model_view.scanRequested.connect(self._start_scan_worker)
        self.model_view.downloadRequested.connect(lambda k: self._start_model_worker([k], True))
        self.model_view.downloadAllMissingRequested.connect(self._on_download_all_missing)
        self.model_view.bindRequested.connect(self._on_model_bind)
        self.model_view.clearBindingRequested.connect(self._on_model_clear_binding)
        self.model_view.dedupeRequested.connect(self._on_model_dedupe)

        self.diagnostics_view.recheckRequested.connect(self._run_diagnostics)

        self.job_controller.stateChanged.connect(self._on_job_state_changed)
        self.job_controller.progress.connect(self._on_job_progress)
        self.job_controller.log.connect(self.translate_view.append_log)
        self.job_controller.loadProgress.connect(self._on_load_progress)
        self.job_controller.finished.connect(self._on_job_finished)

    def _load_settings_into_views(self):
        self.settings_view.load_from(self.settings)
        self.model_view.set_model_dir_label(self.settings.model_dir)
        self.model_view.set_current_bindings(self.settings.model_files)
        self.translate_view.set_last_browse_dir(self.settings.last_input_dir)
        apply_model_dir(self.settings.model_dir)

    def _restore_window_geometry(self):
        if self.settings.window_geometry_hex:
            try:
                self.restoreGeometry(QtCore.QByteArray.fromHex(self.settings.window_geometry_hex.encode()))
            except Exception:
                pass

    # ------------------------------------------------------------ 菜单动作
    def _menu_open_input(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择输入文件夹", self.translate_view.default_dir())
        if d:
            self.translate_view.input_edit.setText(d)
            self.centralWidget().setCurrentWidget(self.translate_view)

    def _show_about(self):
        QtWidgets.QMessageBox.about(
            self, "关于 comic-gen",
            "comic-gen 漫画自动翻译\n\n"
            "检测 RT-DETRv2 · OCR manga-ocr/EasyOCR · 抹字 LaMa · 翻译 Sakura/通用本地 LLM\n"
            "全部本地推理，无需联网 API。",
        )

    # ------------------------------------------------------------ 设置
    def _on_settings_changed(self):
        self.settings_view.apply_to(self.settings)
        self.settings.save()
        self.model_view.set_model_dir_label(self.settings.model_dir)
        apply_model_dir(self.settings.model_dir)
        self._start_scan_worker()

    # ------------------------------------------------------------ 翻译
    def _on_start(self):
        if self.job_controller.is_busy():
            return
        request = self.translate_view.build_request()
        if not request.input_path:
            QtWidgets.QMessageBox.warning(self, "提示", "请先选择输入")
            return
        conflict = engine_lang_conflict(request.engine, request.source_lang)
        if conflict:
            QtWidgets.QMessageBox.warning(self, "引擎不匹配", conflict)
            return

        apply_model_dir(self.settings.model_dir)
        cfg = self.settings.to_config(request)

        not_ready = self._models_not_ready_message(cfg)
        if not_ready:
            QtWidgets.QMessageBox.warning(self, "模型未就绪", not_ready)
            self.centralWidget().setCurrentWidget(self.model_view)
            return

        self.settings.last_input_dir = str(Path(request.input_path).parent)
        if request.output_path:
            self.settings.last_output_dir = str(Path(request.output_path).parent)
        self.settings.save()

        self.translate_view.clear_log()
        self.status_progress.setVisible(True)
        self.status_progress.setValue(0)
        try:
            self._pending_request = request
            self.job_controller.start(request, cfg)
        except RuntimeError as e:
            QtWidgets.QMessageBox.warning(self, "提示", str(e))

    def _models_not_ready_message(self, cfg) -> str | None:
        """开始翻译前的门槛检查：之前只校验非空输入+引擎语言兼容，不检查任何模型
        是否就位。点"稍后再说"的新用户第一次点"开始翻译"会在不定长等待后撞上
        运行期异常——本该在这里提前拦下，给一句能直接照做的人话。
        needed_model_keys/resolve_status 和 Runner._go_offline_if_ready 用的是同一套
        数据源，不重复定义"这次任务需要哪些模型"的判断逻辑。
        """
        from ..core.model_locator import resolve_status, status_text
        from ..pipeline.registry import needed_model_keys

        problems = []
        for key in needed_model_keys(cfg):
            st = resolve_status(key, bindings=self.settings.model_files, model_dir=self.settings.model_dir)
            if not st.is_ready:
                text, _ = status_text(st)
                problems.append(f"· {MODEL_MANIFEST[key].label}：{text}")
        if not problems:
            return None
        return "以下模型还没准备好，请先到「模型管理」页下载或定位：\n\n" + "\n".join(problems)

    def _on_rerender(self):
        if self.job_controller.is_busy():
            return

        if self._last_completed_request is not None and self._last_completed_output_path is not None and \
                resolve_pages_json(self._last_completed_output_path).exists():
            # 快路径：本会话内刚跑完一个任务，直接复用同一对 (input, 已解析的最终 output)——
            # 用已解析路径而非原始 request.output_path（后者可能是 None），
            # 保证 work_dir 与最终输出位置都与原来完全一致（见 runner.run 的 work_dir_override 说明：
            # 重渲染不能凭输出目录反推原始输入，必须传回同一对 input/output）。
            base = self._last_completed_request
            request = JobRequest(
                input_path=base.input_path, output_path=str(self._last_completed_output_path),
                source_lang=base.source_lang, engine=base.engine, direction=base.direction,
                is_rerender=True,
            )
        else:
            d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择翻译工作目录（包含 pages.json）", self.translate_view.default_dir())
            if not d:
                return
            pages_file = resolve_pages_json(d)
            if not pages_file.exists():
                QtWidgets.QMessageBox.warning(self, "找不到 pages.json", f"该目录不像是有效的翻译工作目录：{pages_file}")
                return
            original_input = read_original_input(pages_file)
            if not original_input:
                QtWidgets.QMessageBox.warning(self, "无法定位原始输入", "pages.json 中缺少原始输入记录，无法重渲染。")
                return
            QtWidgets.QMessageBox.information(
                self, "跨会话重渲染",
                "已从 pages.json 恢复原始输入路径。\n最终产物将打包到默认位置"
                "（<原始输入名>_translated），而非本次浏览的目录，请留意日志中的完成路径。",
            )
            request = JobRequest(
                input_path=original_input, output_path=None,
                source_lang=self.translate_view.lang_combo.currentData(),
                engine=self.translate_view.engine_combo.currentData(),
                direction=self.translate_view.dir_combo.currentData(),
                is_rerender=True, work_dir_override=d,
            )

        conflict = engine_lang_conflict(request.engine, request.source_lang)
        if conflict:
            QtWidgets.QMessageBox.warning(self, "引擎不匹配", conflict)
            return

        apply_model_dir(self.settings.model_dir)
        cfg = self.settings.to_config(request)
        self.translate_view.clear_log()
        self.status_progress.setVisible(True)
        self.status_progress.setValue(0)
        try:
            self._pending_request = request
            self.job_controller.start(request, cfg)
        except RuntimeError as e:
            QtWidgets.QMessageBox.warning(self, "提示", str(e))

    def _on_job_state_changed(self, state: str):
        running = state == STATE_RUNNING or state == "cancelling"
        self.translate_view.set_running(running)
        self.settings_view.set_running(running, self._running_settings_summary())
        text = {"idle": "就绪", "running": "运行中…", "cancelling": "正在停止…"}.get(state, state)
        self.status_lbl.setText(text)
        if state == "idle":
            self.status_progress.setVisible(False)

    def _running_settings_summary(self) -> str:
        req = self._pending_request
        if req is None:
            return ""
        return (
            f"· 模型目录：{self.settings.model_dir or '（默认 HF 缓存）'}\n"
            f"· 推理设备：{self.settings.device}\n"
            f"· 翻译引擎：{req.engine}　源语言：{req.source_lang}\n"
            f"· 检测阈值：{self.settings.det_threshold}\n"
            f"· PDF DPI：{self.settings.pdf_dpi}"
        )

    def _on_job_progress(self, fraction: float, desc: str):
        self.translate_view.set_progress(fraction, desc)
        self.status_progress.setValue(int(fraction * 100))
        self.status_lbl.setText(desc)

    def _on_load_progress(self, i: int, n: int):
        self.status_lbl.setText(f"读取文档 {i}/{n} 页")

    def _on_job_finished(self, result):
        # 记录刚提交的请求（若成功/部分成功），供"重渲染"快路径复用；必须用当初实际提交的
        # request（可能来自 rerender 的跨会话回退分支，其 input_path 与 translate_view 当前
        # 字段无关），不能重新从视图字段派生——否则会记错。
        request_used = self._pending_request
        self._pending_request = None

        if isinstance(result, JobReport):
            self.translate_view.show_result(result)
            if result.status == "ok":
                if request_used:
                    self._last_completed_request = request_used
                    self._last_completed_output_path = result.output_path
                QtWidgets.QMessageBox.information(self, "完成", result.summary)
            elif result.status == "partial":
                if request_used:
                    self._last_completed_request = request_used
                    self._last_completed_output_path = result.output_path
                detail = "\n".join(f"· {f.image}：{f.message}" for f in result.failed_pages[:20])
                QtWidgets.QMessageBox.warning(self, "部分完成", f"{result.summary}\n\n{detail}")
            elif result.status == "cancelled":
                # 之前只设了状态栏文字，JobReport.cancelled 里写好的"已完成 X/Y 页，
                # 用相同路径重新运行即可继续"这句话从来到不了用户眼前。同时把已解析的
                # 输出路径回填进输出框——用户下次若改填/清空这个字段，Runner._work_dir
                # 会解析到不同目录，整卷会从零重来，回填能让默认的"再点一次开始"保持安全。
                self.status_lbl.setText("已停止")
                if result.output_path:
                    self.translate_view.output_edit.setText(str(result.output_path))
                QtWidgets.QMessageBox.information(self, "已停止", result.summary)
            else:  # failed
                detail = "\n".join(f"· {f.image}：{f.message}" for f in result.failed_pages[:20])
                QtWidgets.QMessageBox.critical(self, "失败", f"{result.summary}\n\n{detail}")
        else:
            QtWidgets.QMessageBox.critical(self, "失败", str(result))

    # ------------------------------------------------------------ 模型管理
    def _start_model_worker(self, keys: list[str], do_download: bool):
        if self.job_controller.is_busy():
            return
        # 重入保护：换新线程前不等旧线程收尾，旧的 QThread 对象会在还在跑的时候被丢弃
        # 引用——FirstRunDialog._start_scan 那边实测复现过这样会把解释器崩成
        # 0xC0000409（见该文件的注释）。这里是同一个风险模式，照抄同样的守卫。
        if self._m_thread is not None and self._m_thread.isRunning():
            return
        self.model_view.set_busy(True)
        self._m_thread = QtCore.QThread()
        self._m_worker = ModelWorker(self.settings, keys, do_download)
        self._m_worker.moveToThread(self._m_thread)
        self._m_thread.started.connect(self._m_worker.run)
        self._m_worker.finished.connect(self._on_model_done)
        self._m_worker.finished.connect(self._m_thread.quit)
        self._m_thread.start()

    def _on_download_all_missing(self):
        missing = self.model_view.missing_keys()
        if missing:
            self._start_model_worker(missing, True)

    def _on_model_done(self, text: str):
        self.model_view.show_status(text)
        self._start_scan_worker()  # 下载/检查完刷新一遍状态；同时会把 set_busy(False) 一起做了

    def _start_scan_worker(self):
        if self.job_controller.is_busy():
            return
        # 同上一处的重入保护：_on_settings_changed 每次设置变更都会调这个方法，
        # 拖动滑块/spinbox 连续触发时旧扫描线程可能还没跑完。
        if self._s_thread is not None and self._s_thread.isRunning():
            return
        self.model_view.set_busy(True)
        self._s_thread = QtCore.QThread()
        self._s_worker = ScanWorker(self.settings)
        self._s_worker.moveToThread(self._s_thread)
        self._s_thread.started.connect(self._s_worker.run)
        self._s_worker.finished.connect(self._on_scan_done)
        self._s_worker.finished.connect(self._s_thread.quit)
        self._s_thread.start()

    def _on_scan_done(self, statuses: dict, dupes: dict):
        self.model_view.set_current_bindings(self.settings.model_files)
        self.model_view.refresh(statuses, dupes)
        self.model_view.set_busy(False)
        self._run_diagnostics()

    def _on_model_bind(self, key: str, path: str):
        self.settings.model_files[key] = path
        self.settings.save()
        self._start_scan_worker()

    def _on_model_clear_binding(self, key: str):
        if self.settings.model_files.pop(key, None) is not None:
            self.settings.save()
            self._start_scan_worker()

    def _on_model_dedupe(self, key: str):
        from ..core.model_locator import find_duplicates, trash_destination

        dups = find_duplicates(key, model_dir=self.settings.model_dir)
        if len(dups) <= 1:
            return
        bound = self.settings.model_files.get(key)
        keep = Path(bound) if bound and Path(bound) in dups else dups[0]
        moved, errors = [], []
        for p in dups:
            if p == keep:
                continue
            try:
                p.rename(trash_destination(p))
                moved.append(str(p))
            except OSError as e:
                errors.append(f"{p}: {e}")
        msg = f"{MODEL_MANIFEST[key].label}：保留 {keep}，移入回收站 {len(moved)} 份"
        if errors:
            msg += "\n失败：\n" + "\n".join(errors)
        self.model_view.show_status(msg)
        self._start_scan_worker()

    # ------------------------------------------------------------ 环境自检
    def _run_diagnostics(self):
        report = detect_environment(model_dir=self.settings.model_dir, bindings=self.settings.model_files)
        self.diagnostics_view.show_report(report)

    # ------------------------------------------------------------ 关闭
    def closeEvent(self, event: QtGui.QCloseEvent):
        self.job_controller.shutdown()
        # 模型/扫描线程同样要等收尾，不能只处理翻译任务的线程——直接退出会让还在跑的
        # QThread 对象在解释器关闭时失去引用，是 FirstRunDialog.closeEvent 已经用同样
        # 方式防过的那类崩溃。
        for thread in (self._m_thread, self._s_thread):
            if thread is not None and thread.isRunning():
                thread.quit()
                thread.wait(3000)
        self.settings.window_geometry_hex = bytes(self.saveGeometry().toHex()).decode()
        self.settings.save()
        super().closeEvent(event)
