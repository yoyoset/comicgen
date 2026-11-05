"""组合根：只负责造零件、接线、菜单/状态栏。不含业务决策——那些在 JobController/core 里。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ..app.review_session import ReviewSession, load_config_for_review
from ..core.environment import apply_model_dir
from ..core.errors import ComicGenError, format_job_failure
from ..core.job import (
    JobReport,
    JobRequest,
    format_failure_report,
    read_original_input,
    resolve_pages_json,
)
from ..core.manifest import MODEL_MANIFEST
from ..core.settings import AppSettings
from ..pipeline.runner import Runner
from ..pipeline.translate import engine_lang_conflict
from .batch_controller import BatchController
from .job_controller import (
    STATE_CANCELLING,
    STATE_PAUSED,
    STATE_PAUSING,
    STATE_RUNNING,
    JobController,
)
from .views.batch_view import BatchView
from .views.components_view import ComponentsView
from .views.diagnostics_view import DiagnosticsView
from .views.first_run_dialog import FirstRunDialog
from .views.model_view import ModelView
from .views.multi_folder_dialog import MultiFolderDialog
from .views.review_view import ReviewView
from .views.settings_view import SettingsView
from .views.translate_view import TranslateView
from .workers import (
    ComponentWorker,
    DiagnosticsWorker,
    ModelWorker,
    ReviewRerenderWorker,
    ScanWorker,
)

# 关窗时给任务线程的宽限：翻译阶段单页可能跑几十秒（LLM 推理），协作式取消只在页边界
# 生效，3 秒的固定等待不够；超过这个上限仍不退出才强制关闭（宁可放弃等，也不能挂死）。
_SHUTDOWN_JOB_WAIT_S = 120


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
        self._m_cancel_event: threading.Event | None = None
        self._s_thread: QtCore.QThread | None = None
        self._s_worker: ScanWorker | None = None
        self._d_thread: QtCore.QThread | None = None
        self._d_worker: DiagnosticsWorker | None = None
        self._rv_thread: QtCore.QThread | None = None
        self._rv_worker: ReviewRerenderWorker | None = None
        self._review_session: ReviewSession | None = None
        self._c_thread: QtCore.QThread | None = None
        self._c_worker: ComponentWorker | None = None
        self._c_cancel_event: threading.Event | None = None

        self.translate_view = TranslateView()
        self.batch_view = BatchView()
        self.settings_view = SettingsView()
        self.model_view = ModelView()
        self.diagnostics_view = DiagnosticsView()
        self.review_view = ReviewView()
        self.components_view = ComponentsView()

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self.translate_view, "翻译")
        tabs.addTab(self.batch_view, "批量")
        tabs.addTab(self.review_view, "校对")
        tabs.addTab(self.settings_view, "设置")
        tabs.addTab(self.model_view, "模型管理")
        tabs.addTab(self.components_view, "组件")
        tabs.addTab(self.diagnostics_view, "环境自检")
        self.setCentralWidget(tabs)

        # 单任务与批量共用同一个 Runner：模型缓存跨两种入口复用（各自再加载一份几十 GB
        # 的权重是这个应用承受不起的成本）。两者互斥执行（各入口互相拦 is_busy），
        # 不会同时跑两个任务占爆 12GB 显存。
        self._runner = Runner()
        self.job_controller = JobController(self._runner)
        self.batch_controller = BatchController(self._runner)

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
        act_help = m_help.addAction("使用说明")
        act_help.triggered.connect(self._show_help)
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
        self.translate_view.pauseRequested.connect(self.job_controller.pause)
        self.translate_view.resumeRequested.connect(self.job_controller.resume)
        self.translate_view.rerenderRequested.connect(self._on_rerender)
        self.translate_view.reviewRequested.connect(self._on_review_open_last)
        self.translate_view.openOutputRequested.connect(self._on_open_output)

        self.settings_view.changed.connect(self._on_settings_changed)
        self.translate_view.cleanupToggled.connect(self._on_cleanup_toggled)
        self.batch_view.cleanupToggled.connect(self._on_cleanup_toggled)

        self.model_view.scanRequested.connect(self._start_scan_worker)
        self.model_view.downloadRequested.connect(lambda k: self._start_model_worker([k], True))
        self.model_view.downloadAllMissingRequested.connect(self._on_download_all_missing)
        self.model_view.cancelDownloadRequested.connect(self._on_cancel_download)
        self.model_view.bindRequested.connect(self._on_model_bind)
        self.model_view.clearBindingRequested.connect(self._on_model_clear_binding)
        self.model_view.dedupeRequested.connect(self._on_model_dedupe)

        self.diagnostics_view.recheckRequested.connect(self._run_diagnostics)

        self.components_view.installRequested.connect(self._start_component_worker)
        self.components_view.cancelRequested.connect(self._on_component_cancel)
        self.components_view.removeRequested.connect(self._on_component_remove)
        self.components_view.restartRequested.connect(self._on_restart)

        self.review_view.openRequested.connect(self._on_review_open)
        self.review_view.pageSelected.connect(self._on_review_page_selected)
        self.review_view.applyTextRequested.connect(self._on_review_apply_text)
        self.review_view.rerenderRequested.connect(self._on_review_rerender)
        self.review_view.exportRequested.connect(self._on_review_export)
        self.review_view.attentionItemActivated.connect(self._on_review_attention_activated)

        self.job_controller.stateChanged.connect(self._on_job_state_changed)
        self.job_controller.progress.connect(self._on_job_progress)
        self.job_controller.log.connect(self.translate_view.append_log)
        self.job_controller.loadProgress.connect(self._on_load_progress)
        self.job_controller.finished.connect(self._on_job_finished)

        self.batch_view.addFilesRequested.connect(self._on_batch_add_files)
        self.batch_view.addFolderRequested.connect(self._on_batch_add_folder)
        self.batch_view.removeSelectedRequested.connect(self._on_batch_remove_selected)
        self.batch_view.clearFinishedRequested.connect(self._on_batch_clear_finished)
        self.batch_view.startRequested.connect(self._on_batch_start)
        self.batch_view.stopRequested.connect(self.batch_controller.stop)
        self.batch_view.pauseRequested.connect(self.batch_controller.pause)
        self.batch_view.resumeRequested.connect(self.batch_controller.resume)
        self.batch_view.openOutputRequested.connect(self._on_batch_open_output)
        self.batch_view.reviewRequested.connect(self._on_batch_review)

        self.batch_controller.queueChanged.connect(self._on_batch_queue_changed)
        self.batch_controller.jobStarted.connect(self._on_batch_job_started)
        self.batch_controller.jobProgress.connect(self._on_batch_job_progress)
        self.batch_controller.stateChanged.connect(self._on_batch_state_changed)
        self.batch_controller.queueFinished.connect(self._on_batch_queue_finished)
        self.batch_controller.log.connect(self.batch_view.append_log)

    def _load_settings_into_views(self):
        self.settings_view.load_from(self.settings)
        self.model_view.set_model_dir_label(self.settings.model_dir)
        self.model_view.set_current_bindings(self.settings.model_files)
        self.translate_view.set_last_browse_dir(self.settings.last_input_dir)
        self.batch_view.set_last_browse_dir(self.settings.last_input_dir)
        self.translate_view.set_cleanup_intermediate(self.settings.cleanup_intermediate)
        self.batch_view.set_cleanup_intermediate(self.settings.cleanup_intermediate)
        apply_model_dir(self.settings.model_dir)

    def _restore_window_geometry(self):
        if self.settings.window_geometry_hex:
            try:
                self.restoreGeometry(QtCore.QByteArray.fromHex(self.settings.window_geometry_hex.encode()))
            except Exception:
                pass
        self._ensure_on_screen()

    def _ensure_on_screen(self):
        """恢复的窗口几何可能来自已拔掉的外接显示器——直接还原会落在屏幕外，用户启动后
        什么都看不到（窗口"存在"但不可见，表现为"程序没打开"）。只要窗口没有和任何
        屏幕的可用区域相交就把它挪回 (100,100)。"""
        screens = QtWidgets.QApplication.screens()
        if not screens:
            return
        try:
            frame = self.frameGeometry()
        except Exception:
            return
        visible = any(frame.intersects(screen.availableGeometry()) for screen in screens)
        if not visible:
            self.move(100, 100)

    # ------------------------------------------------------------ 菜单动作
    def _menu_open_input(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择输入文件夹", self.translate_view.default_dir())
        if d:
            self.translate_view.input_edit.setText(d)
            self.centralWidget().setCurrentWidget(self.translate_view)

    def _show_about(self):
        from .. import __version__

        QtWidgets.QMessageBox.about(
            self, "关于 comic-gen",
            f"comic-gen 漫画自动翻译  v{__version__}\n\n"
            "检测 RT-DETRv2 · OCR manga-ocr/EasyOCR · 抹字 LaMa · 翻译 Sakura/通用本地 LLM\n"
            "全部本地推理，无需联网 API。",
        )

    def _show_help(self):
        """首次用户的操作引导：说明"现在该做什么、为什么"。避免用户对着空白界面猜。"""
        QtWidgets.QMessageBox.information(
            self, "使用说明",
            "1. 选输入：文件夹 / ZIP / CBZ / RAR / CBR / EPUB / PDF\n"
            "2. 核对源语言与翻译引擎（默认已按语言自动推荐）\n"
            "3. 点「开始翻译」——模型没就位会先提示去「模型管理」页补齐，不会白跑\n"
            "4. 完成后在「成品预览」查看，或点「校对本次结果」改译文、改完点「导出全部」重新打包\n\n"
            "每页结果实时落盘：任务中断/失败后，用相同输入输出重新运行即可从断点继续，"
            "不会重跑已完成的页。中途想腾出显卡做别的事，点「暂停」会在当前页结束后释放"
            "显存，之后点「继续」从同一位置接着跑。",
        )

    # ------------------------------------------------------------ 设置
    def _on_settings_changed(self):
        self.settings_view.apply_to(self.settings)
        self.settings.save()
        self.model_view.set_model_dir_label(self.settings.model_dir)
        apply_model_dir(self.settings.model_dir)
        self._start_scan_worker()

    def _on_cleanup_toggled(self, checked: bool):
        """翻译页/批量页的「完成后清理中间产物」勾选是同一个持久化偏好的两个入口：
        任一勾选变化立即落盘，并同步另一个页签的勾选，避免两处显示不一致。"""
        self.settings.cleanup_intermediate = checked
        self.settings.save()
        self.translate_view.set_cleanup_intermediate(checked)
        self.batch_view.set_cleanup_intermediate(checked)

    # ------------------------------------------------------------ 翻译
    def _any_job_busy(self) -> bool:
        """单任务或批量队列是否有任何一个在跑。GPU 只有一块，两条路径必须互斥，
        别处入口（开始/重渲染/校对/模型管理/自检）统一走这一个判断。"""
        return self.job_controller.is_busy() or self.batch_controller.is_busy

    def _on_start(self):
        if self.batch_controller.is_busy:
            QtWidgets.QMessageBox.information(self, "提示", "批量队列正在运行，请先停止队列再开始单任务。")
            return
        if self.job_controller.is_busy():
            return
        request = self.translate_view.build_request()
        if not request.input_path:
            QtWidgets.QMessageBox.warning(self, "提示", "请先选择输入")
            return
        # 最常见的用户错误：路径打错字。在弹出"运行中"之前先拦下，比等 Runner
        # 在工作线程里抛 InputError 再弹窗更即时（后者还多一次模型加载的等待）。
        if not Path(request.input_path).exists():
            QtWidgets.QMessageBox.warning(self, "输入路径无效", f"输入路径不存在：{request.input_path}")
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
        self.translate_view.set_review_available(False)  # 新任务开始，上一轮的校对入口失效
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
        if self._any_job_busy():
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
        # paused/pausing 期间任务线程仍存活，同样算 busy：批量/设置/校对都要被拦，
        # 否则暂停中的任务继续跑会撞上另一个同时占 GPU 的任务。
        running = state in (STATE_RUNNING, STATE_PAUSING, STATE_PAUSED, STATE_CANCELLING)
        self.translate_view.set_running(running)
        self.batch_view.set_blocked(running)
        self.settings_view.set_running(running, self._running_settings_summary())
        text = {
            "idle": "就绪",
            "running": "运行中…",
            "pausing": "正在暂停…",
            "paused": "已暂停（显卡资源已释放）",
            "cancelling": "正在停止…",
        }.get(state, state)
        self.status_lbl.setText(text)
        if state == STATE_PAUSING:
            self.translate_view.set_pause_state("requested")
        elif state == STATE_PAUSED:
            self.translate_view.set_pause_state("paused")
        elif state == STATE_RUNNING:
            self.translate_view.set_pause_state("running")
        else:
            self.translate_view.set_pause_state("disabled")
        if state == STATE_RUNNING:
            # 冷启动要加载检测/OCR/翻译/抹字四个模型，可能 30 秒以上——在第一个阶段
            # 进度到达前先明确告诉用户"正在加载模型"，而不是进度条停在 0 毫无说明。
            self.translate_view.set_progress(0, "正在加载模型…")
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
            f"· 全局 DPI：{self.settings.pdf_dpi}\n"
            f"· 完成后清理中间产物：{'是' if self.settings.cleanup_intermediate else '否'}"
        )

    def _on_job_progress(self, fraction: float, desc: str):
        self.translate_view.set_progress(fraction, desc)
        self.status_progress.setValue(int(fraction * 100))
        self.status_lbl.setText(desc)

    def _on_load_progress(self, i: int, n: int):
        # 文档加载阶段（如 PDF 逐页光栅化）之前只写状态栏文字，进度条停在 0——
        # 200 页的 PDF 这一步本身就要花不少时间，看起来像卡死了。
        self.status_lbl.setText(f"读取文档 {i}/{n} 页")
        self.status_progress.setValue(int(i / max(1, n) * 100))

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
                self.translate_view.set_review_available(True)
                path_hint = f"\n\n输出位置：{result.output_path}" if result.output_path else ""
                QtWidgets.QMessageBox.information(self, "完成", result.summary + path_hint)
            elif result.status == "partial":
                if request_used:
                    self._last_completed_request = request_used
                    self._last_completed_output_path = result.output_path
                # 部分失败也有产物可校对，仍点亮「校对本次结果」
                self.translate_view.set_review_available(True)
                self._show_failure_report(result, "部分完成")
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
                self._show_failure_report(result, "失败")
        else:
            QtWidgets.QMessageBox.critical(self, "失败", str(result))

    def _show_failure_report(self, report: JobReport, title: str):
        """失败/部分完成时弹完整报告：可滚动查看全部失败页、可导出到文件、可复制。
        之前只截断展示前 20 条且不可滚动不可导出——几百页的任务失败信息等于没有。"""
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"{title} — {report.summary}")
        dlg.resize(720, 460)
        lay = QtWidgets.QVBoxLayout(dlg)

        text = QtWidgets.QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(format_failure_report(report))
        lay.addWidget(text, 1)

        btn_row = QtWidgets.QHBoxLayout()
        export_btn = QtWidgets.QPushButton("导出到文件…")
        export_btn.clicked.connect(lambda: self._export_failure_report(dlg, report))
        btn_row.addWidget(export_btn)
        copy_btn = QtWidgets.QPushButton("复制全部")
        copy_btn.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(text.toPlainText()))
        btn_row.addWidget(copy_btn)
        btn_row.addStretch(1)
        close_btn = QtWidgets.QPushButton("关闭")
        close_btn.setDefault(True)
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        dlg.exec()

    def _export_failure_report(self, parent, report: JobReport) -> None:
        default = self._last_completed_output_path or Path(self.translate_view.default_dir() or ".")
        default_file = (default if default.is_dir() else default.parent) / "失败报告.txt"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(parent, "导出失败报告", str(default_file), "文本文件 (*.txt)")
        if not path:
            return
        try:
            Path(path).write_text(format_failure_report(report), encoding="utf-8")
        except OSError as e:
            QtWidgets.QMessageBox.warning(parent, "导出失败", f"无法写入文件：{e}")

    # ------------------------------------------------------------ 模型管理
    def _start_model_worker(self, keys: list[str], do_download: bool):
        if self._any_job_busy():
            return
        # 重入保护：换新线程前不等旧线程收尾，旧的 QThread 对象会在还在跑的时候被丢弃
        # 引用——FirstRunDialog._start_scan 那边实测复现过这样会把解释器崩成
        # 0xC0000409（见该文件的注释）。这里是同一个风险模式，照抄同样的守卫。
        if self._m_thread is not None and self._m_thread.isRunning():
            return
        self.model_view.set_busy(True)
        self._m_cancel_event = threading.Event()
        self._m_thread = QtCore.QThread()
        self._m_worker = ModelWorker(self.settings, keys, do_download, self._m_cancel_event)
        self._m_worker.moveToThread(self._m_thread)
        self._m_thread.started.connect(self._m_worker.run)
        self._m_worker.log.connect(self.model_view.show_status)
        self._m_worker.finished.connect(self._on_model_done)
        self._m_worker.finished.connect(self._m_thread.quit)
        self._m_thread.start()

    def _on_cancel_download(self):
        if self._m_cancel_event is not None:
            self._m_cancel_event.set()

    def _on_download_all_missing(self):
        missing = self.model_view.missing_keys()
        if missing:
            self._start_model_worker(missing, True)

    def _on_model_done(self, text: str):
        self.model_view.show_status(text)
        self._start_scan_worker()  # 下载/检查完刷新一遍状态；同时会把 set_busy(False) 一起做了

    def _start_scan_worker(self):
        if self._any_job_busy():
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
        # 环境自检含全部 6 个模型的本地搜索，在共享大模型目录上实测要几秒——不能放
        # UI 线程同步跑（启动/每次扫描完成都会卡界面）。挪到后台线程，和模型扫描
        # 同一个线程模式；重入保护同其它 worker（换线程前不等旧线程收尾的守卫）。
        if self._any_job_busy():
            return
        if self._d_thread is not None and self._d_thread.isRunning():
            return
        self._d_thread = QtCore.QThread()
        self._d_worker = DiagnosticsWorker(self.settings.model_dir, self.settings.model_files)
        self._d_worker.moveToThread(self._d_thread)
        self._d_thread.started.connect(self._d_worker.run)
        self._d_worker.finished.connect(self._on_diagnostics_done)
        self._d_worker.finished.connect(self._d_thread.quit)
        self._d_thread.start()

    def _on_diagnostics_done(self, result):
        from ..core.environment import EnvReport

        if isinstance(result, Exception):
            report = EnvReport(warnings=[f"环境自检失败：{result}"])
        else:
            report = result
        self.diagnostics_view.show_report(report)
        self.components_view.set_cuda_available(report.cuda_available)

    # ------------------------------------------------------------ GPU 组件
    def _start_component_worker(self):
        if self._c_thread is not None and self._c_thread.isRunning():
            return
        self.components_view.set_busy(True)
        self._c_cancel_event = threading.Event()
        self._c_thread = QtCore.QThread()
        self._c_worker = ComponentWorker(self._c_cancel_event)
        self._c_worker.moveToThread(self._c_thread)
        self._c_thread.started.connect(self._c_worker.run)
        self._c_worker.progress.connect(self.components_view.on_progress)
        self._c_worker.log.connect(self.components_view.append_log)
        self._c_worker.finished.connect(self.components_view.on_finished)
        self._c_worker.finished.connect(self._c_thread.quit)
        self._c_thread.start()

    def _on_component_cancel(self):
        if self._c_cancel_event is not None:
            self._c_cancel_event.set()

    def _on_component_remove(self):
        from ..core import gpu_components

        now = gpu_components.schedule_removal()
        self.components_view.append_log(
            "已删除 GPU 组件" if now else "GPU 组件正在使用中，已安排在下次启动时删除")
        self.components_view.refresh()

    def _on_restart(self):
        if self._any_job_busy():
            QtWidgets.QMessageBox.information(self, "无法重启", "有任务正在运行，结束后再重启。")
            return
        import sys

        args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
        if QtCore.QProcess.startDetached(sys.executable, args):
            self.close()
        else:
            QtWidgets.QMessageBox.warning(self, "重启失败", "无法自动重启，请手动关闭后重新打开程序。")

    def _on_open_output(self):
        """「打开输出目录」：在文件管理器中打开最近一次任务的输出位置。
        文件夹输入输出是目录、压缩包/PDF 输入输出是文件，统一打开其所在位置。"""
        target = self._last_completed_output_path
        if target is None:
            return
        if target.is_file():
            target = target.parent
        if target.exists():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(target)))

    # ------------------------------------------------------------ 批量队列
    def _on_batch_add_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "添加漫画文件", self.batch_view.default_dir(),
            "漫画文件 (*.zip *.cbz *.epub *.pdf *.rar *.cbr);;所有文件 (*.*)")
        if not paths:
            return
        self._batch_enqueue(paths, first=paths[0])

    def _on_batch_add_folder(self):
        dlg = MultiFolderDialog(self, self.batch_view.default_dir())
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        dirs = dlg.selected_folders()
        if not dirs:
            return
        self._batch_enqueue(dirs, first=dirs[0])

    def _batch_enqueue(self, paths: list[str], first: str):
        """批量加入任务；重复输入在队列里内联提示"已在队列中"，不弹模态框、不静默丢。"""
        added = 0
        rejects: list[str] = []
        for p in paths:
            err = self.batch_controller.enqueue(p)
            if err is None:
                added += 1
            else:
                rejects.append(err)
        # 记住最近浏览目录，下次文件对话框从这里开始
        self.settings.last_input_dir = str(Path(first).parent)
        self.settings.save()
        self.batch_view.set_last_browse_dir(self.settings.last_input_dir)
        if rejects:
            self.batch_view.set_notice(
                f"已加入 {added} 个任务；{len(rejects)} 个已在队列中跳过：\n" + "\n".join(rejects))
        else:
            self.batch_view.set_notice(f"已加入 {added} 个任务")

    def _on_batch_remove_selected(self):
        job = self.batch_view.selected_job()
        if job is None:
            return
        err = self.batch_controller.remove(job)
        if err is not None:
            self.batch_view.set_notice(err)

    def _on_batch_clear_finished(self):
        n = self.batch_controller.clear_finished()
        if n:
            self.batch_view.set_notice(f"已清空 {n} 个已结束的任务")

    def _on_batch_start(self):
        if self.job_controller.is_busy():
            QtWidgets.QMessageBox.information(self, "提示", "有单个任务正在运行，请先等待它完成或停止。")
            return
        if self.batch_controller.is_busy:
            return
        if not self.batch_controller.queue.jobs():
            self.batch_view.set_notice("队列为空——先把漫画加进队列再开始。")
            return
        if not self.batch_controller.queue.has_work():
            self.batch_view.set_notice("没有待运行的任务——已完成的不会重跑，可点「清空已完成」后重新添加。")
            return
        conflict = engine_lang_conflict(
            self.batch_view.engine_combo.currentData(), self.batch_view.lang_combo.currentData())
        if conflict:
            QtWidgets.QMessageBox.warning(self, "引擎不匹配", conflict)
            return

        apply_model_dir(self.settings.model_dir)
        # 队列里所有任务共用批量页的引擎/语言/方向选择 + 当前设置，先按同一个代表性
        # cfg 过一次模型就绪门槛（和单任务 _on_start 同一个检查），不齐就先让用户去
        # 模型管理页补齐，避免队列跑到第一个任务才在后台线程报模型缺失。
        probe_cfg = self.settings.to_config(JobRequest(
            input_path="", source_lang=self.batch_view.lang_combo.currentData(),
            engine=self.batch_view.engine_combo.currentData()))
        not_ready = self._models_not_ready_message(probe_cfg)
        if not_ready:
            QtWidgets.QMessageBox.warning(self, "模型未就绪", not_ready)
            self.centralWidget().setCurrentWidget(self.model_view)
            return

        self.batch_view.clear_log()
        self.batch_view.set_notice("")
        self.batch_controller.start(self._batch_prepare)

    def _batch_prepare(self, job):
        """批量页选择 + 持久化设置 → (JobRequest, Config)。每个任务一份独立配置，
        工作目录按各自输入推导（Runner._work_dir），队列语义是"N 个独立任务"，不是
        "一个大任务"——指向系列根目录的文件夹输入仍是递归的一个大任务，这是文件夹
        输入本身的语义，批量页不会替用户拆卷。"""
        request = JobRequest(
            input_path=job.input_path,
            output_path=job.output_path,
            source_lang=self.batch_view.lang_combo.currentData(),
            engine=self.batch_view.engine_combo.currentData(),
            direction=self.batch_view.dir_combo.currentData(),
            is_rerender=False,
        )
        return request, self.settings.to_config(request)

    def _on_batch_queue_changed(self):
        self.batch_view.set_jobs(self.batch_controller.queue.jobs())

    def _on_batch_job_started(self, i: int, total: int):
        self.batch_view.set_position_text(f"第 {i + 1}/{total} 个任务")
        self.status_lbl.setText(f"批量队列：第 {i + 1}/{total} 个任务")

    def _on_batch_job_progress(self, fraction: float, desc: str):
        self.batch_view.set_progress(fraction, desc)
        self.status_progress.setVisible(True)
        self.status_progress.setValue(int(fraction * 100))
        self.status_lbl.setText(f"批量：{desc}")

    def _on_batch_state_changed(self, state: str):
        # paused/pausing 期间任务线程仍存活，队列算 busy：单任务/设置入口保持被拦。
        running = state in (STATE_RUNNING, STATE_PAUSING, STATE_PAUSED)
        self.batch_view.set_batch_running(running)
        self.translate_view.set_blocked(running)
        if state == STATE_PAUSING:
            self.batch_view.set_pause_state("requested")
            self.status_lbl.setText("批量队列暂停中…")
        elif state == STATE_PAUSED:
            self.batch_view.set_pause_state("paused")
            self.status_lbl.setText("批量队列已暂停（显卡资源已释放）")
        elif state == STATE_RUNNING:
            self.batch_view.set_pause_state("running")
            self.status_lbl.setText("批量队列运行中…")
        else:
            self.batch_view.set_pause_state("disabled")
            if not self.job_controller.is_busy():
                self.status_progress.setVisible(False)
            self.status_lbl.setText("就绪")

    def _on_batch_queue_finished(self):
        c = self.batch_controller.queue.counts()
        self.batch_view.set_position_text(f"共 {c['total']} 个任务")
        self.batch_view.clear_progress()
        self.batch_view.set_notice(f"队列结束：成功 {c['done']}，失败 {c['failed']}")
        self.status_lbl.setText(f"批量队列完成：成功 {c['done']}，失败 {c['failed']}")

    def _on_batch_open_output(self, job):
        out = job.resolved_output or job.output_path
        if not out:
            self.batch_view.set_notice("该任务还没有输出，等它跑完再打开。")
            return
        target = Path(out)
        if target.is_file():
            target = target.parent
        if target.exists():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(target)))
        else:
            self.batch_view.set_notice(f"输出位置不存在：{out}")

    def _on_batch_review(self, job):
        if self._any_job_busy():
            QtWidgets.QMessageBox.information(self, "提示", "有任务正在运行，请先等待完成。")
            return
        work_dir = self._batch_job_work_dir(job)
        if work_dir is None:
            self.batch_view.set_notice("该任务还没有可校对的产物（没有输出或尚未完成）。")
            return
        self._open_review_dir(str(work_dir))

    def _batch_job_work_dir(self, job) -> Path | None:
        """从队列任务解析其翻译工作目录：压缩包/PDF 的输出是文件，工作目录在
        <输出名去扩展>/ 下；文件夹输入的输出就是工作目录本身。"""
        out = job.resolved_output or job.output_path
        if not out:
            return None
        pages = resolve_pages_json(Path(out))
        if not pages.exists():
            return None
        return pages.parent

    # ------------------------------------------------------------ 校对
    def _on_review_open(self):
        if self._any_job_busy():
            QtWidgets.QMessageBox.information(self, "提示", "有任务正在运行，请先等待完成。")
            return
        start_dir = str(self._last_completed_output_path) if self._last_completed_output_path \
            else self.translate_view.default_dir()
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择翻译工作目录（包含 pages.json）", start_dir)
        if not d:
            return
        self._open_review_dir(d)

    def _on_review_open_last(self):
        """「校对本次结果」：直接打开最近一次完成任务的工作目录，不用再浏览定位。
        这是"结果查看 → 校对"闭环的最后一跳，之前只能靠手动浏览去找输出目录。"""
        if self._any_job_busy():
            QtWidgets.QMessageBox.information(self, "提示", "有任务正在运行，请先等待完成。")
            return
        if self._last_completed_output_path is None:
            return
        self._open_review_dir(str(self._last_completed_output_path))

    def _open_review_dir(self, d: str):
        apply_model_dir(self.settings.model_dir)
        # 传入的可能是"文件夹输入"的输出目录（本身就在 pages.json 旁），也可能是
        # 压缩包/PDF 输入的最终输出文件（如 xxx_translated.cbz，工作目录在
        # xxx_translated/ 下）——统一用 resolve_pages_json 定位 pages.json 所在目录。
        # 之前直接拿 d 当工作目录，压缩包输入的结果从"校对本次结果"打开会报
        # "找不到 pages.json"（ReviewSession 收到的是输出文件路径，不是目录）。
        work_dir = resolve_pages_json(Path(d)).parent
        # 校对/单页重渲染要用产生这批结果时的原始配置（模型路径、设备、字体……），
        # 不是设置页里现在恰好显示的值——job.json 有就优先用它，没有才退回当前设置。
        fallback_cfg = self.settings.to_config(JobRequest(input_path=""))
        cfg = load_config_for_review(work_dir, fallback_cfg)
        try:
            self._review_session = ReviewSession(work_dir, cfg)
        except ComicGenError as e:
            QtWidgets.QMessageBox.warning(self, "无法打开", e.user_message)
            return

        self.review_view.set_work_dir_label(d)
        self.review_view.load_pages(self._review_session.page_images(), self._review_attention_tuples())
        self.centralWidget().setCurrentWidget(self.review_view)

    def _review_attention_tuples(self) -> list[tuple[str, int, str]]:
        return [(a.page_image, a.region_index, a.reason) for a in self._review_session.attention_items()]

    def _on_review_page_selected(self, image: str):
        if self._review_session is None:
            return
        page = self._review_session.get_page(image)
        pixmap = QtGui.QPixmap(str(self._review_session.preview_path(image)))
        self.review_view.show_page(image, pixmap, page.regions)

    def _on_review_apply_text(self, image: str, region_index: int, text: str):
        if self._review_session is None:
            return
        self._review_session.update_region_text(image, region_index, text)
        self._review_session.save()
        self.review_view.refresh_attention(self._review_attention_tuples())

    def _on_review_rerender(self, image: str):
        if self._review_session is None:
            return
        if self._any_job_busy():
            QtWidgets.QMessageBox.information(self, "提示", "有任务正在运行，请先等待完成。")
            return
        # 同一套重入保护（FirstRunDialog 实测复现过丢引用导致 0xC0000409 的那类崩溃）：
        # 换新线程前不等旧线程收尾。
        if self._rv_thread is not None and self._rv_thread.isRunning():
            return
        self.review_view.set_busy(True)
        self._rv_thread = QtCore.QThread()
        self._rv_worker = ReviewRerenderWorker(self._review_session, image)
        self._rv_worker.moveToThread(self._rv_thread)
        self._rv_thread.started.connect(self._rv_worker.run)
        self._rv_worker.finished.connect(self._on_review_rerender_done)
        self._rv_worker.finished.connect(self._rv_thread.quit)
        self._rv_thread.start()

    def _on_review_rerender_done(self, image: str, result):
        self.review_view.set_busy(False)
        if isinstance(result, Exception):
            msg = format_job_failure(result) if isinstance(result, ComicGenError) else str(result)
            QtWidgets.QMessageBox.warning(self, "重渲染失败", msg)
            return
        page = self._review_session.get_page(image)
        pixmap = QtGui.QPixmap(str(result))
        self.review_view.show_page(image, pixmap, page.regions)
        self.review_view.refresh_attention(self._review_attention_tuples())

    def _on_review_export(self):
        """校对页「导出全部」：重新抹字渲染全部页面并重新打包最终输出。

        单页重渲染只更新工作目录 translated/ 里的图，不会更新最终交付物（文件夹
        根目录的散图 / zip / pdf）——如果用户以为"校对改过了"就完成了，拿到手的
        输出其实是旧图，这是"界面看着完成、产物没跟上"的坑。这里走和「重渲染」
        相同的 Runner.rerender 路径（用校对会话的工作目录做 work_dir_override、
        用它存的那份原始配置），把校对修改真正落进交付物。
        """
        if self._any_job_busy():
            QtWidgets.QMessageBox.information(self, "提示", "有任务正在运行，请先等待完成。")
            return
        if self._review_session is None:
            return
        original_input = self._review_session.original_input
        if not original_input:
            QtWidgets.QMessageBox.warning(self, "无法导出", "找不到原始输入记录，无法重新打包。")
            return
        if QtWidgets.QMessageBox.question(
            self, "导出全部",
            "将重新抹字渲染全部页面并重新打包最终输出，把校对修改落进交付物。\n"
            "需要 GPU 推理，耗时取决于页数；未改动的页也会重跑一遍渲染以保证产物一致。\n\n继续？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        ) != QtWidgets.QMessageBox.Yes:
            return

        cfg = self._review_session.cfg
        request = JobRequest(
            input_path=original_input, output_path=None,
            source_lang=cfg.source_lang, engine=cfg.translator, direction=cfg.direction,
            is_rerender=True, work_dir_override=str(self._review_session.work_dir),
        )
        apply_model_dir(self.settings.model_dir)
        self.translate_view.clear_log()
        self.status_progress.setVisible(True)
        self.status_progress.setValue(0)
        self.translate_view.set_review_available(False)
        self._pending_request = request
        try:
            self.job_controller.start(request, cfg)
        except RuntimeError as e:
            QtWidgets.QMessageBox.warning(self, "提示", str(e))

    def _on_review_attention_activated(self, image: str, region_index: int):
        if self._review_session is None:
            return
        if self.review_view.current_image() != image:
            self.review_view.select_page(image)  # 同线程直连信号，回来后画布已经刷新
        if region_index >= 0:
            self.review_view.select_region(region_index)

    # ------------------------------------------------------------ 关闭
    def closeEvent(self, event: QtGui.QCloseEvent):
        self.job_controller.shutdown()
        self.batch_controller.stop()
        if self._m_cancel_event is not None:
            self._m_cancel_event.set()
        if self._c_cancel_event is not None:
            self._c_cancel_event.set()  # 下载循环 1MB 内响应；.part 保留，下次续传
        self._wait_for_shutdown_threads()
        self.settings.window_geometry_hex = bytes(self.saveGeometry().toHex()).decode()
        self.settings.save()
        super().closeEvent(event)

    def _wait_for_shutdown_threads(self):
        """等所有后台线程收尾再让窗口关闭。

        之前只对每个线程 wait(3000) 然后继续关窗——翻译任务的一页可能跑几十秒
        （LLM 推理），3 秒超时后直接退出会让 QThread 在 run() 还没返回时就被销毁
        （"QThread: Destroyed while thread is still running"，任务看似"关掉了"
        实际还在后台跑/可能崩）。改成：取消已请求，给任务线程一个宽松上限，期间
        泵事件循环让 UI 保持响应，状态栏说明在等什么；上限到了才强制继续。
        批量队列和单任务用同一个 Runner、同一个宽松上限——两个入口只会有一个在跑。
        """
        job_like = [
            t for t in (self.job_controller._thread, self.batch_controller._thread)
            if t is not None and t.isRunning()
        ]
        if job_like:
            self.status_lbl.setText("正在等待任务安全停止（当前页处理完即退出）…")
            deadline = time.monotonic() + _SHUTDOWN_JOB_WAIT_S
            while time.monotonic() < deadline:
                if not any(t.isRunning() for t in job_like):
                    break
                for t in job_like:
                    t.wait(200)
                QtWidgets.QApplication.processEvents()
            for t in job_like:
                if t.isRunning():
                    self.status_lbl.setText(
                        f"任务线程 {_SHUTDOWN_JOB_WAIT_S}s 内未停止，已强制关闭（进度已落盘）")

        # 模型/扫描/自检线程同样要等收尾，不能只处理翻译任务的线程——直接退出会让还在跑的
        # QThread 对象在解释器关闭时失去引用，是 FirstRunDialog.closeEvent 已经用同样
        # 方式防过的那类崩溃。这几类线程都很快，3 秒足够。
        for thread in (self._m_thread, self._s_thread, self._rv_thread, self._d_thread, self._c_thread):
            if thread is not None and thread.isRunning():
                thread.quit()
                thread.wait(3000)
