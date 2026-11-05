# 给 AI 的安装清单（comic-gen）

> 本文是**从源码安装**的清单：仓库不含模型和二进制，需要的东西由使用者或它的 AI 按下面的清单去下载。
> 只想用、不改代码的话，直接下 [Releases](https://github.com/yoyoset/comicgen/releases) 里的 Windows 安装包（CPU 框架，
> GPU 组件在程序「组件」页下载），不用走本文。
> 这份清单的目的是省掉你摸索架构与依赖的时间。数字取自 2026-11-02 的实测；URL 当天 HTTP 200。

## 0. 先决条件

- Windows、**Python 3.13**（只在 3.13 上测过，`pyproject.toml` 限定 `>=3.13,<3.14`）、git
- NVIDIA 显卡 + 支持 CUDA 12.4 的驱动。默认配置显存峰值约 12.0GB（含桌面约 2GB），**12GB 卡余量很小**；
  显存更小就把翻译引擎换成更小的 GGUF（见 §2 末尾），或设置里选 CPU（慢很多）
- 磁盘：模型合计约 18GB（检测 0.17 + OCR 0.45 + 抹字 0.2 + Sakura 8.2 + 通用 LLM 9.0）

## 1. 环境（顺序不能反）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu124
.\.venv\Scripts\python.exe -m pip install -e .
# 本地翻译引擎：用 abetlen 预编译的 cu124 轮子，不要从源码编译
.\.venv\Scripts\python.exe -m pip uninstall llama-cpp-python -y
.\.venv\Scripts\python.exe -m pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124
```

先装 torch 再装其余依赖：反过来会被默认 PyPI 的 CPU 版 torch 静默覆盖。

## 2. 需要下载的外部资产

首次启动 UI 的向导可以自动下载；AI 手动下载时按下表，放到**同一个模型目录**（例如 `D:\models`），然后在设置里把
「模型位置」指向它（或设环境变量 `HF_HOME` 指向）。GGUF 是平铺文件，放在该目录根下即可被按文件名找到。

| 键 | 内容 | 来源 | 大小 | 校验 |
| :--- | :--- | :--- | :--- | :--- |
| detect | 文字/气泡检测（Apache-2.0） | HF `ogkalu/comic-text-and-bubble-detector`，需要 `config.json`、`preprocessor_config.json`、`model.safetensors` | ~170MB | 目录齐全即可 |
| ocr_ja | 日文 OCR（Apache-2.0） | HF `kha-white/manga-ocr-base`，需要 `config.json`、`preprocessor_config.json`、`vocab.txt`、`model.safetensors` 或 `pytorch_model.bin` | ~450MB | 目录齐全即可 |
| ocr_other（可选） | 非日文 OCR | EasyOCR 首次使用时自动下 `craft_mlt_25k.pth`、`english_g2.pth` 到 `~/.EasyOCR/model` | ~100MB | — |
| lama | 抹字 | https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt → 放 `~/.cache/torch/hub/checkpoints/big-lama.pt`（首次运行也会自动下） | 205,803,670 B | sha256 `7ba7aa7ac37a4d41fdbbeba3a2af7ead18058552997e3a3cd1a3b2210c9e6b4c`（本机文件实测，上游未公布） |
| sakura | 日译中（**CC BY-NC-SA 4.0，非商用**） | https://huggingface.co/SakuraLLM/Sakura-14B-Qwen3-v1.5-GGUF/resolve/main/sakura-14b-qwen3-v1.5-iq4xs.gguf | 8,180,361,440 B（文件名与字节数被严格校验） | sha256 `cd1a129b19c711fb67f232e7feadc3cf0b2183e9408d47ac222f14123dae5436`（= HF LFS oid） |
| llm | 非日文翻译（Apache-2.0） | https://huggingface.co/unsloth/Qwen3-14B-GGUF/resolve/main/Qwen3-14B-Q4_K_M.gguf | 9,001,753,984 B（不卡字节数，可换成任意通用指令 GGUF） | sha256 `5eaa0870bd81ed3b58a630a271234cfa604e43ffb3a19cd68e54a80dd9d52a66` |

大文件用支持断点续传的方式下（`curl -L -C - -O <url>` 或 `huggingface-cli download`）。HF 连不上时可用 `hf-mirror.com`
（只换域名，路径不变），**下完必须核对 sha256**。

## 3. 验证（做完再宣布成功）

```powershell
# 离线单测：纯函数，不碰显卡/网络，约 1–2 分钟，应为 410 passed
.\.venv\Scripts\python.exe -m pytest -q tests

# 模型是否都被识别（把路径换成你的模型目录）；应全部 ready
.\.venv\Scripts\python.exe -c "from comic_gen.core.model_locator import scan_all_status as s; [print(k, 'ready' if v.is_ready else 'NOT-READY') for k,v in s(None, r'D:\models').items()]"

# 真机冒烟：用仓库自带的合成日文测试包 tests\sample.zip（1 页），应输出 out.zip（译文页）
.\.venv\Scripts\python.exe -m comic_gen.app.job_service tests\sample.zip -o out.zip --translator sakura --source-lang ja
```

## 4. 已知陷阱

- 用**项目的 `.venv\Scripts\python.exe`**，PATH 上的 `python` 在这台机器可能是应用商店占位程序。
- `settings.json` 的 `model_files` 里如果绑着旧模型文件，严格校验字节数的键（`sakura`、`lama`）会报"文件大小不对"，删掉那一项即可重新搜索。
- 处理 CBR/RAR 需要另装 WinRAR/UnRAR（pip 管不了）。
- 想换更小的日译中模型：「模型管理」页只对 `llm` 键开放任意 GGUF；`sakura` 键的提示词是照 Sakura 官方格式写死的，换成别的权重不兼容。
- 许可：见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
