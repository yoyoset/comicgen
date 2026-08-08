# comic-gen 漫画自动翻译

丢入一个漫画文件夹（或 ZIP / CBZ / EPUB），自动完成：**日文定位 → OCR 识别 → 翻译 → 抹字修复 → 译文贴回**，输出与输入同格式的熟肉。

## 快速开始

### 方式一：EXE（免装环境）

双击 `dist\comicgen\comicgen-ui.exe`（首次启动稍慢），原生窗口打开。**首次运行会弹出一个向导**，
确认模型存放目录——如果你已经用别的工具（或本机的 subgen 项目）下载过 Sakura/Qwen 之类的模型，
把目录指向那个位置即可直接复用，不用重新下载；弹窗会列出哪些模型已经就位、还差多少。

每个模型也可以单独指定路径，去「模型管理」页逐项「定位…」。

自己打包：

```powershell
.\scripts\build_exe.ps1
```

这个脚本只替换 `comicgen-ui.exe` 和 `_internal\`，不碰 `dist\comicgen\` 下的 `settings.json` /
日志 / 用户模型——重新打包不会丢配置也不会动模型（模型现在不放在这个目录里）。
直接跑 `python -m PyInstaller build_exe.spec` 也可以，但 `dist\comicgen` 会被 `--clean` 整个清空，
下次要靠这个向导重新走一遍（无害，只是麻烦），所以推荐用脚本。

### 方式二：源码运行

```powershell
# 1. 建环境
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
.\.venv\Scripts\python.exe -m pip install -e .

# 2. 启动 UI
.\.venv\Scripts\python.exe -m comic_gen.ui.app
```

首次运行会自动下载模型（检测 ~170MB、OCR ~450MB、抹字 ~200MB，Sakura 引擎另需 ~4.3GB；大文件下载慢请耐心等待）。

## 翻译引擎

全部本地推理，不联网、不需要 API Key。

| 引擎 | 说明 |
|---|---|
| `sakura`（日文默认） | 本地 Sakura-7B GGUF，专精日译中轻小说/漫画风格，**仅支持日文源** |
| `llm`（非日文默认） | 本地通用 LLM（Qwen3-4B GGUF），支持日/英/中/其它多语言 |
| `mock` | 假翻译，用于离线自测 |

源语言选好后界面会自动推荐对应引擎；两个引擎的具体权重文件都可以在「模型管理」页单独换成
别的本地文件，不必用默认下载的那份。

GPU 加速需 CUDA 12.4 运行时（安装 CUDA Toolkit 12.4 或确保 `cudart64_12.dll` 等可被找到）：

```powershell
.\.venv\Scripts\python.exe -m pip uninstall llama-cpp-python -y
.\.venv\Scripts\python.exe -m pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124
```

## 译后编辑（校对）

跑完的任务会在工作目录里留下 `pages.json`（每页原文/译文/坐标 + 各模型实际路径 + 阶段耗时，还有
`run.log` 完整日志）。**校对在应用内完成**，不需要碰任何文件：

1. 翻译页点「校对本次结果…」直接打开最近一次任务，或在「校对」页点「打开工作目录…」选择任意任务目录。
2. 画布上每个检测框都可点：选中后右侧显示原文，改译文后点「应用译文」（快捷键 **Ctrl+Enter**）保存。
3. 「重渲染本页」只重新生成这一页的图（GPU，较慢），适合快速预览改动效果。
4. 全部改完点「导出全部…」——只有这一步会把校对修改真正写进最终交付物（文件夹/ZIP/PDF），
   单页重渲染不会更新最终包。

「需要注意」列表会列出检测置信度低 / OCR 没识别出文字 / 缺译文 / 整页失败的区域，不必通读全书。

## 命令行

不需要 UI 也能跑一次任务（安装后可直接用 `comicgen` 命令）：

```powershell
.\.venv\Scripts\python.exe -m comic_gen.app.job_service <输入> -o <输出> --translator sakura
```

参数：`--translator sakura|llm|mock`、`--source-lang ja|en|zh|other`、`--device cuda|cpu`。
输出路径留空按默认规则推导（见下表）。

## 输出规则

| 输入 | 输出 |
|---|---|
| 文件夹（含子目录） | 同目录 `<名>_translated\` |
| ZIP / CBZ | 同目录 `<名>_translated.zip / .cbz` |
| RAR / CBR | 同目录 `<名>_translated.cbz`（RAR 不可写，转 CBZ；解压需系统装 WinRAR/UnRAR） |
| EPUB | 同目录 `<名>_translated.epub`（结构保留，图片原位替换） |
| PDF | 同目录 `<名>_translated.pdf`（每页 @150dpi 渲染为图，输出合并回 PDF；可在 UI 调整） |

## 模型复用

模型（检测/OCR/翻译/抹字）默认走 HuggingFace 缓存（`HF_HOME`/`HF_HUB_CACHE`），可以和本机其它
项目共用同一份，不必各自下载一遍。首次运行的向导、以及「模型管理」页的「扫描本机已有模型」，
都会自动探测已有缓存并直接复用。每个模型也可以单独指定路径。

## 常见问题

- **任务卡在「加载模型」很久**：首次运行要下载模型（检测 ~170MB、OCR ~450MB、抹字 ~200MB，Sakura 引擎另需 ~4.3GB），大文件下载慢请耐心等待；模型都就位后会自动离线运行，不联网。
- **报「输入路径不存在」**：路径打错了或文件被移走，重新选择正确的输入。
- **报磁盘空间不够**：输出位置可用空间不足以存放译文图和最终产物，清理磁盘或把「输出路径」改到空间更大的位置。
- **报「模型未就绪」**：去「模型管理」页下载或「定位…」缺失的模型；已定位的文件被移走或大小不对时会明确报错，需重新定位。
- **报「引擎不匹配」**：Sakura 只支持日文源，非日文请改用「通用本地 LLM」。
- **任务中断/失败后恢复**：每页结果实时落盘，用相同的输入/输出路径重新运行即可从断点继续，不会重跑已完成页；失败页会回退用原图打包，保证交付物页数完整。
- **「校对」改完了但最终包没变化**：单页「重渲染本页」只更新工作目录里的图，必须点「导出全部…」才会重新打包最终交付物。
- **控制台中文乱码**：是 Windows 控制台 GBK 显示问题，日志文件本身是 UTF-8，不影响使用。

## 技术栈

检测 RT-DETRv2（ogkalu/comic-text-and-bubble-detector，Apache-2.0）· OCR manga-ocr/EasyOCR ·
抹字 LaMa · 翻译本地 Sakura/通用 LLM（均为 GGUF，走 llama.cpp）· 界面 PySide6 / PyInstaller EXE

方案文档见 [docs/PROPOSAL.md](docs/PROPOSAL.md)。
