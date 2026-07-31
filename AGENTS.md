# AGENTS.md — comic-gen

漫画自动翻译流水线：丢入 文件夹 / ZIP / CBZ / EPUB → 日文定位 → OCR → 翻译 → 抹字 → 译文贴回 → 同构输出。

## 环境

- Windows 11 / Python 3.13（venv 在 `.venv\`）/ RTX 3060 12GB
- venv Python：`.\.venv\Scripts\python.exe`（所有命令用它，不要依赖系统 python）
- torch 2.6.0+cu124（CUDA 可用）

## 常用命令

```powershell
# 安装（首次）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
.\.venv\Scripts\python.exe -m pip install -e .

# 启动 UI
.\.venv\Scripts\python.exe -m comic_gen.ui.app

# 命令行跑一次任务（不需要 UI；装完 -e . 后可直接用 comicgen 命令）
.\.venv\Scripts\python.exe -m comic_gen.app.job_service <输入路径> -o <输出路径> --translator mock

# 生成自测素材（合成漫画页 + 最小 EPUB）
.\.venv\Scripts\python.exe scripts\make_sample.py
.\.venv\Scripts\python.exe scripts\make_sample_epub.py

# 打包 EXE（产物 dist\comicgen\comicgen-ui.exe，约 5 分钟）
# 用脚本而不是直接跑 PyInstaller：脚本只换 exe 本体 + _internal\，
# dist\comicgen\ 下的 settings.json/日志/用户模型全程不动。
.\scripts\build_exe.ps1
```

## 结构

```
comic_gen/
├── core/        models.py(Page/Region/状态机) config.py(Config，含 model_paths 绑定)
│                manifest.py(6 个模型的单一清册：repo/文件名/精确大小/目录成员表)
│                model_locator.py(本地权重定位 + 四态状态机 ready/missing/path_broken/size_mismatch)
│                settings.py(AppSettings 持久化，唯一的 Config 组装点) paths.py(便携配置路径)
│                environment.py(能力探测 EnvReport) job.py(JobRequest/JobReport/job.json 快照) utils.py
│                errors.py(ComicGenError 层次：ModelError/InputError/OutputError；
│                          format_job_failure 供 UI/CLI 共用，Qt-free)
│                contracts.py(Detector/OcrBackend/Translator 的 Protocol 类型契约)
├── infra/       logging.py(桥接叶子模块日志到 UI 面板 + 每任务 run.log)
│                engine_cache.py(EngineCache：按依赖字段签名判断懒加载缓存是否要重建)
├── pipeline/    loader(解包/打包) detect(RT-DETRv2) ocr(manga-ocr/EasyOCR)
│                translate(sakura/llm/mock，全本地 GGUF) inpaint(LaMa+取色) render(横竖排自适应)
│                registry.py(翻译引擎/OCR 后端的单一注册表，取代硬编码 if/elif 分发)
│                runner(编排+pages.json断点续跑+job.json快照+JobReport+协作式取消)
├── app/         job_service.py（Qt-free 任务入口，`comicgen` CLI 用这个；
│                UI 走 ui/job_controller.py 是因为需要跨任务复用 Runner 的引擎缓存，
│                这里每次调用建一个新 Runner，跑完即弃，不是要取代那条路径）
│                review_session.py（校对会话，Qt-free：读写 pages.json、
│                「需要注意」区域列表、单页重渲染——不碰其它页、不重新打包最终产物）
└── ui/
    ├── main_window.py   组合根：只接线，不含业务决策
    ├── job_controller.py 任务生命周期唯一归属（idle/running/cancelling）
    ├── workers.py       QThread 工作对象（PipelineWorker/ModelWorker/ScanWorker/ReviewRerenderWorker）
    └── views/           纯视图：translate/settings/model/diagnostics/first_run_dialog/review
entry_ui.py      EXE 入口（绝对导入；不要直接用包内 app.py 当入口）
build_exe.spec   PyInstaller 配置
scripts/         自测素材生成 + build_exe.ps1（打包脚本，就地换件不碰用户数据）
tests/sample*    自测产物（不入库）
dist/comicgen/   EXE 产物（不入库，但其中的 settings.json 是用户数据，重新打包不会动它）
docs/            PROPOSAL.md(方案+决策) HANDOFF.md(阶段交接)
```

## 关键设计（改动前必读）

1. **中间产物驱动**：每页所有阶段结果存 `<工作目录>/pages.json`，含 `input` 指纹（绝对路径），指纹不符则丢弃缓存。状态机：pending→detected→ocred→translated→rendered；`rerender=True` 会把 rendered 打回 translated 强制重渲染。**四个阶段全部逐页 try + 每页落盘**，单页失败不致整阶段丢失。另有 `<工作目录>/job.json`：本次任务的完整 `Config` 快照 + 各模型实际解析到的绝对路径 + 各阶段耗时，供事后诊断（`pages.json` 只记 `input` 一项，不够定位"当时用的是什么配置"）；`<工作目录>/run.log` 是叶子模块（`ocr.py`/`translate.py`/`render.py`）通过 `infra.logging` 记的带时间戳警告/提示。
2. **工作目录规则**：文件夹输入 → 输出目录即工作目录；压缩包/PDF 输入 → `<输出文件名去扩展>/` 为工作目录，`_extract/src` 为解包区（**每次 run 先清空再解包**，防止不同输入污染）。`Runner._check_output_safe` 阻止输出覆盖原图（同路径或输出在输入目录内）。
3. **支持格式**：文件夹（**递归**子目录）、ZIP/CBZ、RAR/CBR（解压需系统 UnRAR，输出转 CBZ）、EPUB（按 OPF spine 排序，重打包保 mimetype 规范）、PDF（PyMuPDF 每页 @150dpi 渲染为图，输出合并回 PDF；可通过 Config.pdf_dpi 调整）。
3. **模型懒加载 + UI 常驻**：Runner._get_* 懒加载；UI 用模块级单例 Runner，模型跨请求复用；翻译引擎按 `cfg.translator` name 切换重建，其余模型不重载。
4. **全部本地翻译引擎**（`Config.translator`：`sakura`/`llm`/`mock`），不联网、不需要 API Key。Sakura 为日文专用（GGUF，轻小说风格），`source_lang != "ja"` 时 `pipeline.translate.engine_lang_conflict` 会拦截并提示改用 `llm`（通用多语言 GGUF）。
5. **多语言分工**：`Config.source_lang`（ja/en/zh/other）决定 OCR 后端——日文 manga-ocr，英/中/其它 EasyOCR；翻译引擎推荐——日文 sakura，其它 llm（`pipeline.translate.ENGINE_RECOMMENDATION`，UI 源语言变化时联动改引擎下拉）。
5a. **每个模型独立路径绑定**：`AppSettings.model_files`（manifest 键 → 绝对路径）经 `to_config()` 传进 `Config.model_paths`，是每次任务的不可变快照（不是进程级全局表——运行中改设置不会影响正在跑的任务）。解析顺序：显式绑定 → 本地缓存搜索（`core.model_locator`）→ 联网下载兜底。绑定失效（文件被移走）或大小不对，直接报错，绝不静默回落去联网下载新的顶替。
5b. **翻译/OCR 分发走注册表**：`pipeline/registry.py` 是翻译引擎（sakura/llm/mock）和 OCR 后端（ocr_ja/ocr_other）唯一的分发数据源；`runner._needed_model_keys`、`ocr.OcrEngine.__init__` 都从这里读，不再各自硬编码 if/elif。`pipeline.translate` 里的 `get_translator`/`engine_lang_conflict`/`ENGINE_LANG_LOCK`/`ENGINE_RECOMMENDATION` 仍然可以照常导入（模块级 `__getattr__` 惰性转发到 registry.py，避免两个模块互相需要对方顶层定义造成循环导入——这是实测复现过的真崩溃，不是预防性设计）。加一个新引擎/后端只需要在 registry.py 里加一条 Entry；`ui/workers.py` 的下载面板、`ui/views/translate_view.py` 的引擎下拉框仍是各自硬编码，留到真的要加第三个引擎/后端时再一起改。
6. **检测模型**用 `ogkalu/comic-text-and-bubble-detector`（Apache-2.0，规避 GPL 系工具的许可证风险）；类别 0=bubble 1=text_bubble 2=text_free，bubble 类丢弃。
6. **取色**：未膨胀 mask + 腐蚀核心 + 中位数（膨胀 mask 会混入背景色导致译文发灰）。
7. **描边策略**：仅浅色文字加黑描边（1px）；深色文字不描边（气泡内描边会糊）。
8. **渲染字体多级兜底**：指定字体 → 常见系统 CJK 字体 → `ImageFont.load_default()`，绝不抛。
9. **模型目录不再有冻结态特殊默认值**：冻结/开发态都用 `core.paths.detect_default_model_dir()` 探测到的 HF 缓存根，由首次运行弹窗（`ui.views.first_run_dialog`）让用户确认或改到别处。**不要**再往 `_apply_default_model_dir_for_frozen` 这类"冻结时默认用 exe 同级目录"的逻辑上加代码——那是上一版备份/还原 hack 的根因，已经删掉。重新打包用 `scripts\build_exe.ps1`（只换 exe 本体 + `_internal\`），不要直接跑裸的 PyInstaller 命令，否则 `--clean` 会清空 `dist\comicgen\` 下的 `settings.json`。
10. **UI 输出路径**：留空=输入同级 `_translated`；可手动指定避免上传 zip 落 temp。
11. PowerShell 控制台 GBK 显示中文日志乱码是**显示问题**，日志本身 UTF-8 正常，不要为此改代码。
12. **校对界面**（`ui/views/review_view.py` + `app/review_session.py`）：画布上的检测框直接可点，选中后侧栏
    编辑译文；"应用译文"只改文字+存盘（快），"重渲染本页"才真的跑一次 LaMa 抹字+渲染（慢，GPU，在
    `ReviewRerenderWorker` 的 QThread 里跑）。改译文会把该页状态从 `rendered` 打回 `translated`（和
    `Runner.rerender` 语义一致），但**只影响这一页**，不会像"重渲染"整卷任务那样把其它已渲染页也标记
    为待重渲染。`render_single_page`（`pipeline/runner.py`）是 Runner 批量渲染循环和这里单页重渲染共用的
    同一份实现，不要各写一份。`ReviewSession` 构造时会用 `pages.json` 里记的原始输入路径重新解析一遍
    原图位置（文件夹输入很便宜，压缩包/PDF 输入等价于重新解包一次），配置优先读 `job.json` 里的快照
    而不是当前设置页的值（两者可能对不上——比如打开一个不是本次会话跑出来的旧工作目录）。
    **`ui/views/review_view.py` 里 `show_page()` 必须先清空 `_region_items` 引用字典再调
    `self.scene.clear()`**——反过来做会撞上一个实测复现过的真崩溃：`scene.clear()` 删除旧
    `QGraphicsRectItem` 时会同步触发 `selectionChanged`，如果这时字典还指着刚被 C++ 层删除的对象，
    信号处理器遍历它们会抛 `libshiboken` 的 "already deleted" `RuntimeError`。这个项目没有 UI 测试
    基础设施（`QGraphicsScene`/`QGraphicsView` 这类对象图在 pytest 里反复构造/析构会真实触发
    access violation 崩溃，试过），验证这类改动用一次性离屏脚本（`QT_QPA_PLATFORM=offscreen` 构造
    真实 `MainWindow` 走一遍流程），不要往 `tests/` 里加构造 `QGraphicsScene` 的 pytest 用例。

## 已知待办 / 局限

- 真实漫画批量测试较少；检测阈值/掩码膨胀参数可能需调（`Config.det_threshold`，已可在设置页调）。
- llama-cpp-python 的 GPU 轮子需要匹配的 CUDA 12.4 运行时，见 README。
- 竖排标点/数字未做体位旋转；SFX 拟声词无专项处理。
- 磁盘冗余（同机与 subgen 共享缓存产生的重复文件、失败下载残留）本轮只做了查重/软删除入口，未清理，
  详见项目 memory `offline-and-model-sharing`。
