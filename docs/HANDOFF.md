# 阶段交接文档（HANDOFF）— 2026-07-21

> 阶段：MVP 完成 + EXE 打包初步成功。本文档用于跨会话/跨人交接，读完应能直接继续开发。

## 1. 项目一句话

漫画自动翻译工具：丢入 文件夹 / ZIP / CBZ / EPUB → RT-DETRv2 定位日文 → manga-ocr 识别 → DeepSeek/本地 Sakura 翻译 → LaMa 抹字 → 译文自适应渲染贴回 → 同构输出。

## 2. 环境快照

- Windows 11 / Python 3.13.14（Microsoft Store 版）/ RTX 3060 12GB
- venv：`.venv\`，Python 入口 `.\.venv\Scripts\python.exe`
- torch 2.6.0+cu124（CUDA 可用）/ transformers 5.14.1 / gradio 6.20.0 / PyInstaller 6.21.0
- 网络注意：**HuggingFace 大文件直连极慢（~1MB/s）**，hf-mirror 同样慢；小模型（检测 170MB / OCR 450MB / LaMa 200MB）已入 HF 缓存（`%USERPROFILE%\.cache\huggingface`）；Sakura 7B GGUF（4.25GB，IQ4_XS）已下载完成。

## 3. 已完成并验证（全部有实测记录）

| 项目 | 状态 | 备注 |
|---|---|---|
| 流水线 6 模块（loader/detect/ocr/translate/inpaint/render） | ✅ | `comic_gen/pipeline/` |
| 合成样图端到端 | ✅ | `scripts\make_sample.py`，检测 3 区域全中、OCR 全对、抹字干净 |
| 文件夹 / ZIP / EPUB 输入输出 | ✅ | EPUB 输出符合规范（mimetype 首项不压缩、图片原位替换） |
| pages.json 断点续跑 / `--rerender` 译后重渲染 | ✅ | 改译文只重渲染，不重复调翻译 |
| Sakura 本地引擎（CPU） | ✅ | 加载 ~10s，3 句翻译 ~10s |
| Gradio 界面 | ✅ | `python -m comic_gen.ui.app` → http://127.0.0.1:7860 |
| CLI | ✅ | `python -m comic_gen.cli <输入> [--translator X] [--rerender]` |
| EXE 打包（PyInstaller） | ✅ 启动验证 | `dist\comicgen\comicgen-ui.exe`，约 3.96GB，双击 10s 出界面 |

## 4. 未验证 / 待办（按优先级）

1. **真实漫画 + DeepSeek 实测**（最高优先）：需要真实素材路径 + API key。真实页面检测阈值/掩码膨胀参数大概率要调（`Config.det_threshold`、inpaint 的 dilate 参数）。
2. **EXE 内完整流水线未测**：只验证了界面启动；exe 里首次跑会往 `exe同级/models/hf` 下载模型（冻结时 HF_HOME/TORCH_HOME 已重定向），检测/OCR/抹字/Sakura 在冻结环境的实际推理未测。
3. Sakura GPU 化：当前 CPU 轮子；GPU 需 CUDA 12.4 运行时 + abetlen cu124 轮子（README 有步骤），或等 llama-cpp 官方新轮子。
4. 竖排数字/标点体位旋转、SFX 拟声词专项、术语表/角色名一致性（翻译上下文）。
5. 翻译结果人工校对界面（当前只能手改 pages.json 再 --rerender）。

## 5. 关键文件

```
build_exe.spec       PyInstaller 配置（collect_all: gradio/gradio_client/safehttpx/groovy/llama_cpp）
entry_ui.py          EXE 入口（绝对导入，冻结环境相对导入会炸）
comic_gen/           包源码（结构详见 AGENTS.md）
docs/PROPOSAL.md     方案文档（§10 决策记录、§11 实现状态）
AGENTS.md            关键设计约束（改代码前必读）
dist/comicgen/       EXE 产物（3.96GB，可直接分发）
```

## 6. 打包 EXE 踩过的坑（重打包前看）

1. `safehttpx`/`groovy` 用 importlib.resources 读 `version.txt` → 必须 `collect_all`，否则启动 FileNotFoundError。
2. 入口不能直接用包内 `comic_gen/ui/app.py`（相对导入失败）→ 用根目录 `entry_ui.py`。
3. 重打包命令：`.\.venv\Scripts\python.exe -m PyInstaller build_exe.spec --noconfirm --clean`（约 5 分钟）。
4. 冻结环境模型缓存重定向逻辑在 `ui/app.py main()`（`sys.frozen` 分支）。

## 7. 历史 bug 记录（已修，回归时注意）

- 取色发灰：膨胀 mask 混入背景色 → 改「未膨胀 mask + 腐蚀核心 + 中位数」。
- 气泡内文字描边发糊：深色字不描边，仅浅色字 1px 黑描边。
- 工作目录污染：不同输入（zip/epub）映射同一工作目录名 → 解包区每次 run 先 `rmtree`，pages.json 加 `input` 指纹校验。
- Sakura 量化文件自动匹配：该 GGUF 仓库只有 IQ4_XS/Q6K，无 Q4_K_M → 偏好列表兜底。
- PowerShell 控制台中文日志乱码 = GBK 显示问题，日志本身正常，勿改代码。

## 8. 下一步建议

1. 用户提供一卷真实漫画 + DeepSeek key → 跑通真实链路，调检测阈值与渲染参数。
2. 实测 EXE 完整跑一遍（小文件夹即可），确认冻结环境模型下载/推理无异常。
3. 视真实效果决定渲染优化项（竖排体位、字号策略、SFX）。

---

## 9. 第二轮修复（2026-07-22，按 docs/AUDIT.md）

针对审计报告修复，**全部回归通过**：

| 审计项 | 修复 | 验证 |
|---|---|---|
| P1-1 UI 错误不可见 | `run_translate` 顶层 try/except → `gr.Error(友好信息)` | ✅ |
| P0-1 字体二次崩 | `_load_font` 多级兜底 → `ImageFont.load_default()` | ✅ |
| P0-2 整阶段丢进度 | 检测/抹字渲染逐页 try + 每页落盘 | ✅ dev/冻结 |
| P0-3 覆盖原图 | `Runner._check_output_safe` 拦截同路径/嵌套输出 | ✅ 拦截测试 |
| 默认引擎 | 改为本地 `sakura`（config/cli/ui），无需 API Key | ✅ |
| P1-4 每次重载模型 | UI 模块级单例 Runner + 翻译引擎按 name 缓存 | ✅ |
| P1-2 EXE 便携失效 | 冻结时仅当 `exe同级/models/hf/hub` 非空才强制用它，否则用全局 | ✅ |
| P2 Gallery 类型 | 返回 `[(path, None), ...]` | ✅ |
| P2 DeepSeek 缺 Key | UI 预校验给友好提示，不崩 | ✅ |

**关键里程碑：EXE 冻结环境端到端全链路首次跑通**（含审计里从未实测的 inpaint+render）：
- mock 全链路：detect→OCR→translate(mock)→LaMa 抹字→渲染 → 2 页 rendered ✅
- **Sakura 全链路（默认引擎）**：detect→OCR→Sakura 翻译→抹字→渲染 → 2 页 rendered，译文正确（"那时，少年还一无所知"等）✅

已将检测/OCR/Sakura 三套模型内置进 `dist\comicgen\models\hf\hub`，EXE 完全离线开箱即用（不联网、不依赖全局 HF_HOME）。

### 仍待办
- 真实漫画 + DeepSeek 实测（需素材/key）
- Sakura GPU 化（CPU 翻译 ~10s/页，大批量偏慢）
- 竖排数字体位、SFX、跨页术语表
