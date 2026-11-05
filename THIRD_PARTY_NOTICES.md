# 第三方组件与模型许可

本项目自身代码为 MIT（见 [LICENSE](LICENSE)）。下面是运行时用到的第三方模型与主要依赖的许可——
**以各自上游为准**；表里的许可字段取自 2026-11-02 当天 HuggingFace 模型卡 / 已安装包的元数据。

## 模型（首次运行时下载，不随仓库分发）

| 模型 | 用途 | 许可 | 备注 |
| :--- | :--- | :--- | :--- |
| [Sakura-14B-Qwen3-v1.5](https://huggingface.co/SakuraLLM/Sakura-14B-Qwen3-v1.5-GGUF) | 日译中（默认） | **CC BY-NC-SA 4.0（非商用）** | **不得用于商业用途**，衍生分发需同协议共享。把本工具用于营利场景前先看清上游条款 |
| [Qwen3-14B](https://huggingface.co/Qwen/Qwen3-14B)（GGUF） | 通用多语言翻译 | Apache-2.0 | |
| [comic-text-and-bubble-detector](https://huggingface.co/ogkalu/comic-text-and-bubble-detector) | 文字/气泡检测 | Apache-2.0 | |
| [manga-ocr-base](https://huggingface.co/kha-white/manga-ocr-base) | 日文 OCR | Apache-2.0（模型卡） | |
| LaMa（经 simple-lama-inpainting） | 抹字 | 包为 Apache-2.0 | 权重来自 LaMa 官方发布，条款以其上游为准 |

## 主要 Python 依赖

| 包 | 许可 |
| :--- | :--- |
| PySide6 | LGPL-3.0 / GPL（Qt for Python 三选一） |
| **PyMuPDF** | **AGPL-3.0 或 Artifex 商业许可** |
| torch | BSD-3-Clause |
| transformers / easyocr / opencv-python | Apache-2.0 |
| llama-cpp-python | MIT |
| rarfile | ISC |

## 关于 PyMuPDF（AGPL-3.0）

从 0.4.0 起 GitHub Release 提供 Windows 安装包（`comicgen-<版本>-win64-cpu.zip`），包内含 PyMuPDF。
因此**安装包作为整体按 AGPL-3.0 分发**：对应的完整源码就是本仓库同版本 tag（本项目自身代码为 MIT，与 AGPL 兼容），
`LICENSE` 与本文件随安装包一起分发。只用源码、自己 `pip install` 依赖的使用者不受此影响。
如果你自己改了代码再打包分发，同样需要公开对应源码（或改用 Artifex 商业许可 / 换掉 PyMuPDF）。

## GPU 组件（程序内「组件」页按需下载，不随安装包分发）

| 组件 | 来源 | 许可 |
| :--- | :--- | :--- |
| torch 2.6.0+cu124（含 NVIDIA CUDA 12.4 / cuDNN 9 运行库） | download.pytorch.org 官方 wheel | BSD-3-Clause；随附的 NVIDIA 库按 NVIDIA 再分发条款 |
| torchvision 0.21.0+cu124 | download.pytorch.org 官方 wheel | BSD-3-Clause |
| llama-cpp-python 0.3.34 cu124 | abetlen/llama-cpp-python GitHub Release | MIT |

程序直接从上游下载并核对 SHA256，本项目不转存这些文件。

## 关于非商用模型

默认日译中模型 Sakura-14B 为 CC BY-NC-SA 4.0（非商用）。本仓库不分发它，由使用者自行下载并遵守其条款。
