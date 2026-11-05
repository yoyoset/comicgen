# AGENTS.md — comic-gen

> **接手本项目先读 [HANDOFF.md](HANDOFF.md)**：现状、构建/测试/打包/发布的确切命令、踩过的坑、
> 已知未修问题、不要做的事。本文件是更细的设计约束。

漫画自动翻译流水线：丢入 文件夹 / ZIP / CBZ / EPUB → 日文定位 → OCR → 翻译 → 抹字 → 译文贴回 → 同构输出。

## 环境

- Windows 11 / Python 3.13（venv 在 `.venv\`）/ RTX 3060 12GB
- venv Python：`.\.venv\Scripts\python.exe`（所有命令用它，不要依赖系统 python）
- torch 2.6.0+cu124（CUDA 可用）
- `pyproject.toml` 的直接依赖已固定到实测可用的精确版本；`requirements-lock.txt`
  是从这些直接依赖递归展开出的完整闭包快照（不是裸 `pip freeze`——这台机器的
  venv 里还留着 Gradio 原型阶段的一堆无关包，freeze 会把那些也锁进去），出问题时
  用它对照"当时装的到底是什么版本"，不是唯一安装步骤（torch/torchvision/
  llama-cpp-python 仍需按下面的专用 index 安装，文件开头注释里写清楚了）。

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

# 打包 EXE（本机自用：CUDA 版 .venv，产物 dist\comicgen\comicgen-ui.exe，约 5.9G，约 5 分钟）
# 用脚本而不是直接跑 PyInstaller：脚本只换 exe 本体 + _internal\，
# dist\comicgen\ 下的 settings.json/日志/用户模型全程不动。
.\scripts\build_exe.ps1

# 打包发布用的 CPU 版安装包（.venv-cpu，产物 dist\release\comicgen-<版本>-win64-cpu.zip）
# GPU 部分由程序「组件」页首次运行下载，见 HANDOFF.md 与 core/gpu_components.py
.\.venv-cpu\Scripts\python.exe scripts\build_release.py
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
├── app/         batch_queue.py（批量队列数据模型，Qt-free：BatchQueue/BatchJob +
│                has_work/reset_for_rerun/去重，10+ 个离线测试）
│                job_service.py（Qt-free 任务入口，`comicgen` CLI 用这个；
│                UI 走 ui/job_controller.py 是因为需要跨任务复用 Runner 的引擎缓存，
│                这里每次调用建一个新 Runner，跑完即弃，不是要取代那条路径）
│                review_session.py（校对会话，Qt-free：读写 pages.json、
│                「需要注意」区域列表、单页重渲染——不碰其它页、不重新打包最终产物）
└── ui/
    ├── main_window.py   组合根：只接线，不含业务决策
    ├── job_controller.py 单任务生命周期唯一归属（idle/running/cancelling）
    ├── batch_controller.py 批量队列执行控制器（顺序驱动共享 Runner，任务结束零弹窗
    │                 自动推进；stop 协作式取消后不推进；_run_next 前 _reap_thread——
    │                 不先等旧 QThread 退出就覆盖引用会实测崩 0xC0000409）
    ├── workers.py       QThread 工作对象（PipelineWorker/ModelWorker/ScanWorker/ReviewRerenderWorker）
    └── views/           纯视图：translate/batch/settings/model/diagnostics/first_run_dialog/review
entry_ui.py      EXE 入口（绝对导入；不要直接用包内 app.py 当入口）
build_exe.spec   PyInstaller 配置，collect_all 清单从 bundle_packages.py 导入（不在这两处各写一份）
bundle_packages.py  build_exe.spec 的 collect_all 包清单，唯一定义
scripts/         自测素材生成 + build_exe.ps1（打包脚本，就地换件不碰用户数据）+
                 check_bundle_deps.py（只读诊断：collect_all 清单里的包是否还在
                 pyproject.toml 依赖闭包里/是否还装着，发布前手动跑一遍）
tests/sample*    自测产物（不入库）
dist/comicgen/   EXE 产物（不入库，但其中的 settings.json 是用户数据，重新打包不会动它）
docs/            PROPOSAL.md(方案+决策) HANDOFF.md(阶段交接)
```

## 关键设计（改动前必读）

1. **中间产物驱动**：每页所有阶段结果存 `<工作目录>/pages.json`，含 `input` 指纹（绝对路径），指纹不符则丢弃缓存。状态机：pending→detected→ocred→translated→rendered；`rerender=True` 会把 rendered 打回 translated 强制重渲染。**四个阶段全部逐页 try + 每页落盘**，单页失败不致整阶段丢失。另有 `<工作目录>/job.json`：本次任务的完整 `Config` 快照 + 各模型实际解析到的绝对路径 + 各阶段耗时，供事后诊断（`pages.json` 只记 `input` 一项，不够定位"当时用的是什么配置"）；`<工作目录>/run.log` 是叶子模块（`ocr.py`/`translate.py`/`render.py`）通过 `infra.logging` 记的带时间戳警告/提示，**每次 run 从干净开始（`mode="w"`）**——文件夹输入的工作目录在多次运行间复用，追加会让它在长期使用中无限增长。
2. **工作目录规则**：文件夹输入 → 输出目录即工作目录；压缩包/PDF 输入 → `<输出文件名去扩展>/` 为工作目录，`_extract/src` 为解包区（**每次 run 先清空再解包**，防止不同输入污染）。`Runner._check_output_safe` 阻止输出覆盖原图（同路径或输出在输入目录内）。**旧目录污染有两道清理**：全新任务（无缓存命中）开工前清空 `translated/`（里面只可能留下一个不同输入/旧任务的图）；`pack_output` 打包文件夹输出时清掉输出目录里不属于当前页清单的图片文件（`loader._clean_stale_output_images`，跳过 `translated/`/`_extract/`）——否则换过输入后交付物会残留上一轮的旧页。
   **完成后清理**（`Config.cleanup_intermediate`，UI 翻译/批量页各有一个同名复选框，同一持久化偏好）：任务**完整成功**后删掉中间产物（文件夹输入删 `translated/`/`_extract/`/`pages.json`/`job.json`/`run.log` 只留根目录交付物；压缩包/PDF 输入删整个工作目录只留输出文件），部分完成/失败/取消一律保留供重跑。**清理必须在 `run()` 的 finally 卸载日志 handler 之后执行**——run.log 的 FileHandler 在 finally 才 close，此前删除它在 Windows 上会因文件被占用失败。清理是 best-effort，失败只记日志不能让成功任务报失败。
   **四阶段编排统一走 `Runner._run_stage`**：检测/OCR/翻译/渲染的循环骨架（todo 过滤、取消检查、逐页 try + `error`/`error_stage` + 落盘 + 进度、阶段计时 + job.json 快照）完全相同，只有"每页做什么"不同——用 `per_page` 回调注入，不再四处各写一份几乎相同的 15 行循环。
3. **支持格式**：文件夹（**递归**子目录）、ZIP/CBZ、RAR/CBR（解压需系统 UnRAR，输出转 CBZ）、EPUB（按 OPF spine 排序，重打包保 mimetype 规范）、PDF（PyMuPDF 每页 @150dpi 渲染为图，输出合并回 PDF；可通过 Config.pdf_dpi 调整）。
4. **模型懒加载 + UI 常驻**：Runner._get_* 懒加载；`ui/job_controller.py` 的 `JobController` 持有一个长期存活的 Runner 实例，模型跨请求复用；翻译引擎按 `cfg.translator` name 切换重建，其余模型不重载。
5. **全部本地翻译引擎**（`Config.translator`：`sakura`/`llm`/`mock`），不联网、不需要 API Key。Sakura 为日文专用（GGUF，轻小说风格），`source_lang != "ja"` 时 `pipeline.translate.engine_lang_conflict` 会拦截并提示改用 `llm`（通用多语言 GGUF）。
6. **多语言分工**：`Config.source_lang`（ja/en/zh/other）决定 OCR 后端——日文 manga-ocr，英/中/其它 EasyOCR；翻译引擎推荐——日文 sakura，其它 llm（`pipeline.translate.ENGINE_RECOMMENDATION`，UI 源语言变化时联动改引擎下拉）。**推理设备**（`Config.device`）四个模型现在都认了（4.3）：detect/inpaint 本来就认，manga-ocr/EasyOCR/llama.cpp 通过 `registry._wants_gpu(cfg)` 接上；请求 GPU 但没有可用 CUDA 时统一退回 CPU，不崩。
7. **每个模型独立路径绑定**：`AppSettings.model_files`（manifest 键 → 绝对路径）经 `to_config()` 传进 `Config.model_paths`，是每次任务的不可变快照（不是进程级全局表——运行中改设置不会影响正在跑的任务）。解析顺序：显式绑定 → 本地缓存搜索（`core.model_locator`）→ 联网下载兜底。绑定失效（文件被移走）或大小不对，直接报错，绝不静默回落去联网下载新的顶替。
8. **翻译/OCR 分发走注册表**：`pipeline/registry.py` 是翻译引擎（sakura/llm/mock）和 OCR 后端（ocr_ja/ocr_other）唯一的分发数据源；`runner._needed_model_keys`、`ocr.OcrEngine.__init__` 都从这里读，不再各自硬编码 if/elif。`pipeline.translate` 里的 `get_translator`/`engine_lang_conflict`/`ENGINE_LANG_LOCK`/`ENGINE_RECOMMENDATION` 仍然可以照常导入（模块级 `__getattr__` 惰性转发到 registry.py，避免两个模块互相需要对方顶层定义造成循环导入——这是实测复现过的真崩溃，不是预防性设计）。加一个新引擎/后端只需要在 registry.py 里加一条 Entry；`app/job_service.py` 的 CLI choices（`--translator`/`--source-lang`）已经从这里推导，源语言合法值见 `registry.SUPPORTED_SOURCE_LANGS`（与 OCR 注册表语言并集有防漂移断言）；`ui/workers.py` 的下载面板、`ui/views/translate_view.py` 的引擎下拉框仍是各自硬编码（`batch_view.py` 复用 `translate_view` 那份），留到真的要加第三个引擎/后端时再一起改。
9. **检测模型**用 `ogkalu/comic-text-and-bubble-detector`（Apache-2.0，规避 GPL 系工具的许可证风险）；类别 0=bubble 1=text_bubble 2=text_free，bubble 类丢弃。
    **两类重复框去重**：`detect.suppress_overlaps`（IoU/包含，检测后、OCR 前，处理类别
    拍平产生的完全相同的框）；`detect.suppress_duplicate_text_runs`（**OCR 后**、文本相同 +
    y 重叠 >0.8，处理 RT-DETR 对窄高竖排文字行沿长边生成的多个 y 重合窄条框——实测
    167.JPEG 一条竖排文字被渲染 7 遍，全卷 13 页 25 个重复区域）。前者在 `detect()` 内部，
    后者由 `runner._ocr_page` 在 `recognize_regions` 后调用。改 OCR 阶段时要记得这两层去重
    各司其职：只靠 IoU 去重会漏掉窄条扫描框。
10. **取色**：未膨胀 mask + 腐蚀核心 + 中位数（膨胀 mask 会混入背景色导致译文发灰）。
11. **描边策略**：仅浅色文字加黑描边，宽度随字号缩放（`max(1, round(size*0.06))`，
    不是固定 1px——小字描边太粗、大字太细）；深色文字不描边（气泡内描边会糊）。
12. **渲染字体多级兜底**：指定字体 → 常见系统 CJK 字体 → `ImageFont.load_default()`，绝不抛。
13. **模型目录不再有冻结态特殊默认值**：冻结/开发态都用 `core.paths.detect_default_model_dir()` 探测到的 HF 缓存根，由首次运行弹窗（`ui.views.first_run_dialog`）让用户确认或改到别处。**不要**再往 `_apply_default_model_dir_for_frozen` 这类"冻结时默认用 exe 同级目录"的逻辑上加代码——那是上一版备份/还原 hack 的根因，已经删掉。重新打包用 `scripts\build_exe.ps1`（只换 exe 本体 + `_internal\`），不要直接跑裸的 PyInstaller 命令，否则 `--clean` 会清空 `dist\comicgen\` 下的 `settings.json`。
14. **UI 输出路径**：留空=输入同级 `_translated`；可手动指定避免上传 zip 落 temp。
15. PowerShell 控制台 GBK 显示中文日志乱码是**显示问题**，日志本身 UTF-8 正常，不要为此改代码。
16. **校对界面**（`ui/views/review_view.py` + `app/review_session.py`）：画布上的检测框直接可点，选中后侧栏
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
17. **翻译阶段分块 + 前缀可信解析 + 输出校验**（`pipeline/translate.py`）：整页不再一次性
     塞进一个编号提示词——密集页（杂志排版常见 40+ 区域/页）预估输出超过 `max_tokens` 会被
     从中间截断，旧实现把"截断"等同于"整页失败"，逐条重译全部区域（实测：493 页杂志一次
     真实运行里，46 个密集页把 46 次调用放大成约 2125 次）。现在 `_chunk_indices` 按行数/
     token 预算切块（中位数页仍是 1 块 1 次调用，不回归），`_parse_numbered` 改为**前缀可信**
     ——只信任 `1..k` 这个每个编号都存在且唯一的最长前缀，缺口起标 `None` 交给
     `_translate_batch_llm` 逐条重试，重试有每页硬上限（`_MAX_SINGLE_RETRIES_PER_PAGE`）。
     **重复句退化的三道防线**（2026-08-25 实测补）：(a) Sakura 的 `repeat_penalty` 不能按
     官方 README 改成 1.0——IQ4XS 量化在垃圾 OCR 输入下会循环出 1500+ 字重复文本，1.1 是
     既有缓解（曾改回 1.0 造成回归，见 CHANGELOG 2026-08-25）；(b) `_is_failed_translation`
     增加重复退化检测（同一短句 >=3 字重复 >=3 次、或输出长度 > 源文 4 倍 + 100）；
     (c) `_strip_think_block` 的正则必须匹配 Qwen3 真实输出 ``（5 字母 think + 斜杠
     闭合，不是 ``），否则 /no_think 兜底形同虚设、思考块会漏进译文。
    另外**译文输出零校验**曾让模型把提示词模板原样吐回来当"译文"抹字重绘到页面上
    （同一次运行里 162 个区域），`_is_failed_translation`（空白/回显`_ECHO_MARKERS`/
     原样等于原文）现在会拦下这类结果，落成空字符串——下游 `select_drawable_regions`
     本来就会对空译文保留原文不抹除，校对页会列进"需要注意"，不需要新管线。这几个常量
     都是模块级的，不是 `Config` 字段（没人会去调，且新增 Config 字段要同步
     `runner.py` `_get_translator` 的引擎缓存签名，为不可配置的内部参数不值得）。
18. **关键状态文件原子写入**：`pages.json`/`job.json`/`settings.json` 一律走
    `core.utils.atomic_write_text`（同目录临时文件 + `os.replace`，Windows/NTFS 上是原子的）。
    进程在写盘中途被杀，最坏情况是丢失"当前这一页"的进度，绝不会留下截断的 JSON 被
    `_load_pages` 当成"没有缓存"整体丢弃（那等于丢掉整个任务的断点）。改这些文件时不要
    退回 `path.write_text` 直写。**渲染/重渲染的成品图也走 `core.utils.atomic_save_image`
    （PNG）**——pages.json 在图片写完后才置 rendered，坏图会随重跑自愈，但校对画布的
    preview_path 优先取渲染图，不原子写会留下一张可被读到的截断 PNG。
19. **磁盘空间预检**（`Runner._check_output_space`，纯 Qt-free）：全新任务（工作目录无
    `pages.json`）开工前按输入图字节数×2.5 估算需求，剩余空间不够直接 `OutputError`
    提前拒绝，不跑到一半把盘写满。续跑不拦（大部分图已在盘上，估算是保守上界，误拦会
    让人无法恢复）。磁盘探测本身失败只记日志放行——best-effort 预检不能反过来拦住任务。
20. **输入路径存在性校验**：`loader.load_input` 对不存在的路径报
    `InputError("输入路径不存在")`，不再落入"不支持的输入类型"（后者误导排查方向）；
    UI `_on_start` 在弹"运行中"前同步快检一遍同一条规则。
21. **失败报告可完整查看/导出**：`core.job.format_failure_report` 把所有失败页（含阶段）
    格式化成文本，UI 失败/部分完成时弹可滚动对话框并支持"导出到文件…/复制全部"——
    不再截断到 20 条不可滚动。
22. **UI 规模化约束**：翻译页日志面板 `setMaximumBlockCount(4000)`（模型管理页 2000），
    完整日志在 `<工作目录>/run.log`（面板下方有可见提示）；成品预览按 24 张/页分页
    （`translate_view.list_preview_images` 纯函数，自然排序），双击缩略图用系统查看器打开
    大图；进度描述（"阶段 页码/总数 预计剩余"）直接显示在进度条旁，不再是状态栏一行字。
23. **关窗安全收尾**：`MainWindow._wait_for_shutdown_threads` 在取消请求后给任务线程
    `_SHUTDOWN_JOB_WAIT_S=120` 秒宽限（翻译单页可能几十秒），期间泵事件循环、状态栏说明
    在等什么——不再 3 秒固定超时后销毁仍在 run() 的 QThread。`ui.app._install_excepthook`
    兜底所有未预期异常：完整堆栈落 `comicgen.log` + 人话弹窗，防止"程序无声闪退"。
    冻结 EXE 的 `comicgen.log` 每次启动从干净开始（`mode="w"`）——只保留最近一次会话，
    追加模式会让模型加载的 tqdm 进度条等噪音长期无限增长。
24. **环境自检在后台线程**（`ui.workers.DiagnosticsWorker`）：`detect_environment` 对全部
    6 个模型走本地搜索，在共享大模型目录上实测要几秒（本机 3.8s）——`MainWindow._run_diagnostics`
    之前同步跑在 UI 线程，启动和每次扫描完成后界面都会冻结几秒。改线程后 `finished` 信号
    是排到主线程的队列连接，验证时 wait() 期间必须 `processEvents()` 才会被送达（和关窗
    收尾同一个模式）。**「校对本次结果」**按钮（翻译页）直达最近一次完成任务的工作目录并
    自动切到校对页签，是"结果查看 → 校对"闭环的最后一跳，不必再手动浏览定位。
25. **校对页「导出全部」**（`review_view.exportRequested` → `_on_review_export`）：单页
    "重渲染本页"只更新工作目录 `translated/` 里的图，**不会更新最终交付物**（文件夹根
    目录散图 / zip / pdf 还是旧的）——用户以为"校对改过了就完成"、拿到手的却是旧产物，
    是"界面看着完成、产物没跟上"的坑。导出按钮走与「重渲染」相同的 `Runner.rerender`
    路径：用校对会话的 `work_dir` 做 `work_dir_override`、用它的 `cfg`（job.json 快照，
    不是当前设置页），重新抹字渲染全部页并重新打包，带确认弹窗（慢操作）。
26. **批量队列**（`app/batch_queue.py` + `ui/batch_controller.py` + `ui/views/batch_view.py`）：
    一次加入多个输入（文件多选 + 文件夹），**顺序执行**（GPU 12GB 不能并行两个模型任务），
    任务结束零模态弹窗自动推进，状态全内联——与单任务最本质的区别是"睡一觉回来"不能卡在
    确认框上。队列语义是 **N 个独立任务各自独立工作目录**，不是"一个大任务"（文件夹输入
    仍是递归的，指向系列根目录会把整卷当一个任务，批量页不替用户拆卷）。单任务与批量共用
    同一个 `Runner`（模型缓存跨两种入口复用），各入口统一走 `MainWindow._any_job_busy`
    互斥，不会同时占 GPU。`BatchController._run_next` 换新 QThread 前必须先 `_reap_thread`
    （quit+wait，超时保引用防 GC）——否则旧线程 quit() 还排在主线程事件队列里、覆盖引用
    让 C++ QThread 在 running 中被销毁，**实测崩 0xC0000409**。批量「校对」复用
    `MainWindow._open_review_dir`，因此该函数改经 `resolve_pages_json` 定位工作目录
    （压缩包/PDF 输入的输出是文件，直接拿它当工作目录会报找不到 pages.json，是单任务
    「校对本次结果」的潜伏 bug，顺带修掉）。
27. **单任务暂停 + 释放显存**（翻译页「暂停」按钮，`pipeline/runner.py` +
    `ui/job_controller.py` + `ui/workers.py`）：`Runner.run/rerender` 接受
    `should_pause/resume_wait/on_paused` 三个回调（省略即无暂停能力，批量队列不传）。
    在 `_run_stage` 每页处理前检查：`_wait_if_paused` 触发后先 `release_models()`
    （llama.cpp 引擎显式 `close()`，torch 系模型丢引用 + `gc.collect()` +
    `torch.cuda.empty_cache()`），再阻塞 `resume_wait()` 等「继续」；`on_paused` 通知
    UI 切「继续」按钮。暂停期间 GPU 完全空闲，恢复后模型按页懒加载重建。**两个必须
    同步改的坑**：(a) `_run_body` 四个阶段的引擎改为**页内 `get_or_rebuild`**而不是
    阶段开头取一次——否则阶段闭包还攥着旧引擎引用，`release_models` 清了缓存也释放
    不了显存（实测确认）；(b) `JobController.resume()` 必须同时 `_pause_requested.clear()`
    + `_resume_event.set()`——只 set 恢复事件的话，worker 醒来后下一页边界看到暂停
    请求仍置位会立刻再次暂停（离屏实测跑出的死循环）。状态机新增 `pausing`/`paused`
    两态（都算 busy，批量/设置/校对继续被拦）。暂停只在页边界生效（与协作式取消同粒度），
    `pages.json` 在暂停前已落盘，暂停期间点「停止」走 cancelled 收尾可重跑续上。
    单页翻译几十秒的耗时意味着「点了暂停要等当前页跑完才生效」，这是有意的——打断
    LLM 推理没有安全中断点。**批量队列同样支持暂停**（`ui/batch_controller.py` +
    `ui/views/batch_view.py` 的「暂停」按钮）：给当前任务的 PipelineWorker 传入
    `_pause_requested/_resume_event`，暂停语义与单任务完全一致（页边界生效、释放显存、
    `paused` 信号把队列状态置为 paused），暂停期间 `_run_next` 不自动推进（worker 阻塞
    未 finished），恢复后当前任务继续、队列照常推进下一个。

## 变更治理：已验证锁定参数（动之前先读这里）

2026-08-25 的真实事故：切 Sakura 官方 prompt 格式时**顺手**把 `repeat_penalty`
1.1→1.0（"照官方 README"），同一改动里混了两个变量，12 页干净漫画验证没触发退化，
上线后 493 页杂志出现大量重复句（一条垃圾输入循环出 1500+ 字）。下面是这一轮的教训
固化成的规则，和由此锁定的参数清单。

**变更纪律（为什么/怎么做）**：

1. **控制变量，一次只改一个**。上一轮的坑：prompt 格式和采样参数在同一个 commit 里
   一起改，验证时变量混在一起，退化无法归因到任何一方。要改 A 就只改 A，B 保持不动，
   验证通过后再单独评估 B。
2. **验证素材必须含退化输入**。只拿干净对话页验证不够——重复退化的高发场景是垃圾
   OCR（杂志密集页）、短碎片句、超长上下文。改采样参数/提示词后，至少手动喂一段
   垃圾输入看是否循环（可用 `scripts/compare_sakura_prompts.py` 的思路）。
3. **别"照官方文档"改已实测校准过的值**。官方 README 的参数前提是参考精度模型；
   本项目的 Sakura 是 IQ4XS 量化，实测需要不同设置（repeat_penalty 就是例子）。
4. **锁定值用测试守住**。下面清单里标"有测试"的项，对应测试必须存在且全绿；改这些
   常量时对应测试失败 = 你在改已验证的行为，先想清楚。

**锁定参数清单**（`comic_gen/` 内）：

| 常量 | 位置 | 值/行为 | 为什么锁 | 验证依据 |
|---|---|---|---|---|
| `SakuraTranslator.repeat_penalty` | `pipeline/translate.py` | `1.1`（**有测试**） | 1.0 在 IQ4XS 量化 + 垃圾输入下循环出 1500+ 字重复 | 2026-08-25 同输入 1.0 vs 1.1 实测（见 memory `translation-quality-regression-2026-08-25.md`） |
| `_strip_think_block` 正则 | `pipeline/translate.py` | 必须匹配 Qwen3 真实 ` think... response` 标签（**有测试**） | 旧正则 `` 从未匹配过真实输出，思考块漏进译文 | 2026-08-25 真实 Qwen3-14B 输出确认标签格式 |
| `_is_failed_translation` 的重复退化/长度上限 | `pipeline/translate.py` | 同短句 ≥3 字重复 ≥3 次、输出 > 源文 4 倍 + 100 判失败（**有测试**） | 退化的最终兜底，删了垃圾直接上页 | 2026-08-25 328.JPEG 真实病例 |
| `suppress_overlaps` + `suppress_duplicate_text_runs` 两层去重 | `pipeline/detect.py` + `runner._ocr_page` | 两层都必须接上（后者**有测试**） | 只靠 IoU 会漏掉竖排窄条扫射框，同一台词被渲染 7 遍 | 167.JPEG 一条「それでも、」检出 7 框 |
| `BaseTranslator._MAX_CHUNK_LINES`/`_CHUNK_OUTPUT_TOKEN_BUDGET` 与 `_LlamaGGUFTranslator` 的 `n_ctx=4096` | `pipeline/translate.py` | 联动关系 | 改分块预算必须同步重估 n_ctx | 2026-08 分块改造 |
| `REGION_QUALITY_MIN_SCORE` | `pipeline/detect.py` | `0.4`（保守起点，**未校准**） | 不是实测校准的合适阈值 | commit 4c0a52e 单样本统计 |
| 取色/竖排/描边/渲染字体兜底等 | `pipeline/inpaint.py`、`render.py` | 见上面对应编号条目 | 大多带"待校准"标注 | 各自条目 |

**改参数前的最低流程**：读这份清单 → 确认要改的是哪个锁定值 → 单独改这一个 →
用含退化输入的真实素材验证 → 更新 CHANGELOG 记录依据 → 确认对应锁定测试仍绿。

## 已知待办 / 局限

- 真实漫画批量测试较少；检测阈值/掩码膨胀参数可能需调（`Config.det_threshold`，已可在设置页调）。
- llama-cpp-python 的 GPU 轮子需要匹配的 CUDA 12.4 运行时：开发态借用 .venv 里 GPU 版 torch/lib，
  发布版借用「组件」页下载的 GPU 版 torch/lib（`pipeline/_cuda_runtime.py` 用 find_spec 跟随实际导入的那份）。
- 竖排标点/数字未做体位旋转；SFX 拟声词无专项处理。
- 磁盘冗余（同机与 subgen 共享缓存产生的重复文件、失败下载残留）本轮只做了查重/软删除入口，未清理，
  详见项目 memory `offline-and-model-sharing`。
- **上游区域质量只做了保守分数过滤**：实测（同一次 493 页杂志运行）manga-ocr 没有置信度门槛、从不返回空
  字符串，6127 个检测区域里 OCR 结果 0 个为空——装饰花纹/logo 也会被幻觉成日文文字，照样进翻译和渲染。
  已按那次样本把 `REGION_QUALITY_MIN_SCORE=0.4` 的保守起点值收进 `detect.filter_region_quality`
  （commit 4c0a52e 实测过滤掉 431 个低分区域）。**未在其它漫画素材上校准**，代码注释里明确写着这是
  "保守起点值，尚未校准"——没有更多真实素材前不要当成"已实测的合适阈值"（全局 CLAUDE.md 硬性要求）。
