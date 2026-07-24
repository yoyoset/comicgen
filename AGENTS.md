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

# 生成自测素材（合成漫画页 + 最小 EPUB）
.\.venv\Scripts\python.exe scripts\make_sample.py
.\.venv\Scripts\python.exe scripts\make_sample_epub.py

# 打包 EXE（产物 dist\comicgen\comicgen-ui.exe，约 4GB，约 5 分钟）
# 注意：COLLECT 会清空 dist/comicgen，需重新把模型复制进 dist\comicgen\models\hf\hub
.\.venv\Scripts\python.exe -m PyInstaller build_exe.spec --noconfirm --clean
```

## 结构

```
comic_gen/
├── core/        models.py(Page/Region/状态机) config.py(Config) utils.py(自然排序等)
├── pipeline/    loader(解包/打包) detect(RT-DETRv2) ocr(manga-ocr)
│                translate(deepseek/sakura/mock) inpaint(LaMa+取色) render(横竖排自适应)
│                runner(编排+pages.json断点续跑)
└── ui/app.py    PySide6 原生界面（main() 内有 sys.frozen 分支：EXE 运行时模型缓存重定向到 exe 同级）
entry_ui.py      EXE 入口（绝对导入；不要直接用包内 app.py 当入口）
build_exe.spec   PyInstaller 配置
scripts/         自测素材生成
tests/sample*    自测产物（不入库）
dist/comicgen/   EXE 产物（不入库）
docs/            PROPOSAL.md(方案+决策) HANDOFF.md(阶段交接)
```

## 关键设计（改动前必读）

1. **中间产物驱动**：每页所有阶段结果存 `<工作目录>/pages.json`，含 `input` 指纹（绝对路径），指纹不符则丢弃缓存。状态机：pending→detected→ocred→translated→rendered；`rerender=True` 会把 rendered 打回 translated 强制重渲染。**检测/抹字渲染逐页 try + 每页落盘**，单页失败不致整阶段丢失。
2. **工作目录规则**：文件夹输入 → 输出目录即工作目录；压缩包/PDF 输入 → `<输出文件名去扩展>/` 为工作目录，`_extract/src` 为解包区（**每次 run 先清空再解包**，防止不同输入污染）。`Runner._check_output_safe` 阻止输出覆盖原图（同路径或输出在输入目录内）。
3. **支持格式**：文件夹（**递归**子目录）、ZIP/CBZ、RAR/CBR（解压需系统 UnRAR，输出转 CBZ）、EPUB（按 OPF spine 排序，重打包保 mimetype 规范）、PDF（PyMuPDF 每页 @150dpi 渲染为图，输出合并回 PDF；可通过 Config.pdf_dpi 调整）。
3. **模型懒加载 + UI 常驻**：Runner._get_* 懒加载；UI 用模块级单例 Runner，模型跨请求复用；翻译引擎按 `cfg.translator` name 切换重建，其余模型不重载。
4. **默认引擎 = 本地 Sakura**（`Config.translator="sakura"`），开箱即用无需 API Key。DeepSeek 为可选，UI 选 deepseek 但缺 Key 会给出友好提示而非崩溃。Sakura 为日文专用，`source_lang != "ja"` 时引擎校验会拦截并提示改用 deepseek。
5. **多语言分工**：`Config.source_lang`（ja/en/zh/other）决定 OCR 后端——日文 manga-ocr，英/中/其它 EasyOCR；翻译引擎推荐——日文 sakura，其它 deepseek（UI 源语言变化时联动改引擎下拉）。
6. **检测模型**用 `ogkalu/comic-text-and-bubble-detector`（Apache-2.0，规避 GPL 系工具的许可证风险）；类别 0=bubble 1=text_bubble 2=text_free，bubble 类丢弃。
6. **取色**：未膨胀 mask + 腐蚀核心 + 中位数（膨胀 mask 会混入背景色导致译文发灰）。
7. **描边策略**：仅浅色文字加黑描边（1px）；深色文字不描边（气泡内描边会糊）。
8. **渲染字体多级兜底**：指定字体 → 常见系统 CJK 字体 → `ImageFont.load_default()`，绝不抛。
9. **EXE 便携模型**：冻结时仅当 `exe同级/models/hf/hub` 非空才强制用它，否则沿用本机全局 HF_HOME。**当前 `dist/comicgen/models/` 已被 COLLECT 清空，离线能力已丧失**；重打包后需手动复制模型到 `dist\comicgen\models\hf\hub\`。
10. **UI 输出路径**：留空=输入同级 `_translated`；可手动指定避免上传 zip 落 temp。
11. PowerShell 控制台 GBK 显示中文日志乱码是**显示问题**，日志本身 UTF-8 正常，不要为此改代码。

## 已知待办 / 局限

- 真实漫画 + DeepSeek 未实测（需素材和 key）；检测阈值/掩码膨胀参数可能需调（Config.det_threshold）。
- Sakura（llama-cpp-python）当前为 CPU 轮子；GPU 加速需 CUDA 12.4 运行时 + abetlen cu124 轮子（见 README）。
- 竖排标点/数字未做体位旋转；SFX 拟声词无专项处理。
- HuggingFace 大文件下载慢（~1MB/s），Sakura GGUF 已缓存；EXEs 分发时模型仍需首次下载。
