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

## ⚠️ 已知的许可张力（未解决，需要维护者决定）

**PyMuPDF 是 AGPL-3.0。** 本仓库源码是 MIT，仅发布源码时没有冲突；但**把 PyMuPDF 一起打进 EXE 分发**，
这个 EXE 整体就要满足 AGPL（提供对应源码、保留许可声明）。可选做法：
1. 接受并在发布 EXE 时遵守 AGPL（附源码链接与许可文本）；
2. 换掉 PyMuPDF（PDF 渲染改用别的库）；
3. 向 Artifex 购买商业许可。

在做出选择前，请不要把含 PyMuPDF 的 EXE 当作"MIT 软件"对外宣传。
