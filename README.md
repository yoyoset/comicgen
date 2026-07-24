# comic-gen 漫画自动翻译

丢入一个漫画文件夹（或 ZIP / CBZ / EPUB），自动完成：**日文定位 → OCR 识别 → 翻译 → 抹字修复 → 译文贴回**，输出与输入同格式的熟肉。

## 快速开始

### 方式一：EXE（免装环境）

双击 `dist\comicgen\comicgen-ui.exe`（首次启动稍慢），浏览器自动打开界面。模型缓存在 exe 同级 `models\` 目录，首次跑任务时自动下载。

自己打包：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller build_exe.spec --noconfirm --clean
```

### 方式二：源码运行

```powershell
# 1. 建环境
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
.\.venv\Scripts\python.exe -m pip install -e .

# 2. 启动界面（浏览器自动打开）
.\.venv\Scripts\python.exe -m comic_gen.ui.app
```

或者用 CLI：

```powershell
# 翻译文件夹（输出到 同目录\<文件夹名>_translated\）
.\.venv\Scripts\python.exe -m comic_gen.cli F:\manga\某漫画\第01卷

# 翻译 ZIP / CBZ / EPUB
.\.venv\Scripts\python.exe -m comic_gen.cli F:\manga\第01卷.epub

# 指定翻译引擎与排版
.\.venv\Scripts\python.exe -m comic_gen.cli <输入> --translator sakura --direction auto
```

首次运行会自动下载模型（检测 ~170MB、OCR ~450MB、抹字 ~200MB，Sakura 引擎另需 ~4.3GB；大文件下载慢请耐心等待）。

## 翻译引擎

| 引擎 | 说明 |
|---|---|
| `sakura`（默认） | 本地 Sakura-7B GGUF，**开箱即用、无需配置、离线**。默认 CPU 推理（较慢）；GPU 加速见下 |
| `deepseek` | 需 API Key：界面里填，或设环境变量 `DEEPSEEK_API_KEY`。便宜，一本 200 页约几毛钱 |
| `mock` | 假翻译，用于离线自测 |

**Sakura GPU 加速（可选）**：预装包为 CPU 轮子。想用 GPU 需 CUDA 12.4 运行时（安装 CUDA Toolkit 12.4 或确保 `cudart64_12.dll` 等可被找到），然后：

```powershell
.\.venv\Scripts\python.exe -m pip uninstall llama-cpp-python -y
.\.venv\Scripts\python.exe -m pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124
```

显存 12GB 可换更大的 14B：把 `Config.sakura_repo` 改为 `SakuraLLM/Sakura-14B-Qwen2.5-v1.0-GGUF`。

## 译后编辑（校对）

工作目录里的 `pages.json` 保存每页的原文/译文/坐标。手动改 `translated_text` 后：

```powershell
.\.venv\Scripts\python.exe -m comic_gen.cli <输入> --rerender
```

只重新抹字渲染，秒级出新图，不会重复调翻译 API。

## 输出规则

| 输入 | 输出 |
|---|---|
| 文件夹（含子目录） | 同目录 `<名>_translated\` |
| ZIP / CBZ | 同目录 `<名>_translated.zip / .cbz` |
| RAR / CBR | 同目录 `<名>_translated.cbz`（RAR 不可写，转 CBZ；解压需系统装 WinRAR/UnRAR） |
| EPUB | 同目录 `<名>_translated.epub`（结构保留，图片原位替换） |
| PDF | 同目录 `<名>_translated.pdf`（每页 @200dpi 渲染为图，输出合并回 PDF） |

## 技术栈

检测 RT-DETRv2（ogkalu/comic-text-and-bubble-detector，Apache-2.0）· OCR manga-ocr · 抹字 LaMa · 翻译 DeepSeek API / 本地 Sakura · 界面 Gradio

方案文档见 [docs/PROPOSAL.md](docs/PROPOSAL.md)。
