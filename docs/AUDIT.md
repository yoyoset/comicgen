# comic-gen 项目审计报告

> 审计方：GLM · 日期：2026-07-22 · 范围：全量源码 + 打包配置 + 已知运行记录
> 方法：静态代码审查 + 历史运行日志分析（不改代码）

## 一、结论摘要

MVP 主链路在**开发环境（源码运行）**下自测通过：检测/OCR/抹字/渲染/ZIP/EPUB/断点续跑/Sakura CPU 均有实测记录。但 **EXE 分发形态未达到「可用」**：端到端仅在冻结环境跑到「翻译前」就因配置/缺数据连续报错三次，且存在若干健壮性与数据安全缺陷。当前不建议直接交付终端用户使用 EXE，需按下文 P0/P1 修复后回归。

用户最近一次报错根因：在 UI 选了默认 `deepseek` 引擎但未填 API Key → `DeepSeekTranslator.__init__` 抛 `ValueError`，经 Gradio 上传为不含细节的 `AppError`，表现为「又报错了」。属配置问题，但暴露了错误处理缺陷。

## 二、按严重度的问题清单

### P0 — 数据风险 / 必然崩溃

**P0-1 渲染字体兜底会二次崩溃** `pipeline/render.py:13-17`
```python
def _load_font(path, size):
    try: return ImageFont.truetype(path, size)
    except OSError: return ImageFont.truetype("msyh.ttc", size)  # 再失败则未捕获 → 崩溃
```
`cfg.font_path` 默认硬编码 `C:\Windows\Fonts\msyh.ttc`（`core/config.py:23`）。若目标机无该字体（精简版 Windows / 他人分发），两次 `truetype` 都抛 `OSError` 且第二次未捕获 → 整个渲染阶段崩溃。EXE 分发到无该字体的机器必然失败。

**P0-2 阶段中途失败会丢失整阶段进度** `pipeline/runner.py:121-130, 161-177`
检测/渲染阶段在 for 循环**结束后**才 `self._save_pages(...)`。若第 3 页抛异常，前 2 页已完成的结果不会落盘，全部丢失。OCR/翻译阶段有逐页 try（`runner.py:137,150`），但检测和抹字渲染没有。坏图、越界、显存不足都会触发。

**P0-3 输出目录可能覆盖原图源** `pipeline/runner.py:189-193` + `pipeline/loader.py:133-140`
文件夹输入时 `work_dir` 即输出目录，`pack_output` 把译文 `copy2` 回该目录。若用户 `-o` 指向输入文件夹本身（或默认输出与输入同名边缘情况），译文会覆盖原图。无任何护栏。

### P1 — 影响可用性 / EXE 分发

**P1-1 UI 无顶层异常捕获，错误对用户不可见** `ui/app.py:33-49`
`run_translate` 直接调 `runner.run(...)`，任何异常（缺 key、模型下载失败、缺数据文件）都变成 Gradio 的 `AppError`，前端只显示「应用抛了异常但未开启详细错误」。这是用户反复「又报错了」且不知原因的直接原因。应 try/except 并 `raise gr.Error(友好信息)`，或预校验。

**P1-2 EXE 模型缓存重定向被环境变量击穿** `ui/app.py:78-81`
```python
os.environ.setdefault("HF_HOME", os.path.join(base, "models", "hf"))
```
`setdefault` 在 `HF_HOME` 已存在时不覆盖。本机用户级 `HF_HOME=F:\hf_cache`，导致 EXE 忽略 exe 同级预置的 `models\hf`，改用 `F:\hf_cache`。在本机「碰巧能跑」，但分发给无 `HF_HOME` 的干净机器才走 exe-local 逻辑——行为不一致，便携性设计失效。冻结环境下应**强制覆盖**（`os.environ["HF_HOME"]=...`）。

**P1-3 EXE 端到端从未跑通** 历史记录
EXE 实测仅到「翻译前」：`unidic_lite` 词典缺失（已修）→ `manga_ocr/assets/example.jpg` 缺失（已修）→ 缺 DeepSeek Key。**抹字(LaMa 下载/推理)、翻译、Sakura 在冻结环境下均未验证**。`build_exe.spec` 每次新增 `collect_all` 都是打地鼠，未做一次完整回归。

**P1-4 每次 UI 点击都重新加载全部模型** `ui/app.py:41` + `runner.py:_get_*`
每次点「开始翻译」都 `new Runner()`，检测/OCR/抹字/Sakura 模型全部重新实例化。Sakura 4.3GB、检测/OCR 各数百 MB，每次跑哪怕只改一页译文也要重新加载 30s+。无模型常驻/缓存。重跑与 `--rerender` 体验差。

**P1-5 EXE 首次运行强制联网下载模型，未内置** `build_exe.spec`
检测 ~170MB + OCR ~450MB + LaMa ~200MB（GitHub release）+ 可选 Sakura 4.3GB（HF 慢 ~1MB/s）。均未打包，依赖联网。中国网络下 HF 大文件极慢，LaMa 走 GitHub 无镜像兜底。无离线分发方案。

### P2 — 健壮性 / 一致性

**P2-1 文件夹输入不递归** `core/utils.py:list_images` 用 `Path.iterdir()`（仅顶层）。含分卷子文件夹的漫画库会漏掉所有子目录图片。而 ZIP/EPUB 用 `rglob` 递归——行为不一致。

**P2-2 DeepSeek 默认选中 + 缺 Key 即崩** `ui/app.py:59` 默认值 `deepseek`。配合 P1-1，新手首次点击必崩。应：缺 Key 时自动回退提示或禁用该选项。

**P2-3 文件夹输出目录结构脏** `runner.py` + `loader.py:133-140`
文件夹输出里同时存在：`pages.json`、`translated/` 子树、复制出的散图、`_extract/`（压缩包输入）。读者打开看到双份图片与中间文件。应清理或隔离。

**P2-4 LaMa 下载无镜像/无兜底** `pipeline/inpaint.py:13`
`simple-lama-inpainting` 从 GitHub release 拉 196MB，无失败重试/镜像。GitHub 不通则抹字阶段整体失败。

**P2-5 Gallery 预览返回类型可能不符** `ui/app.py:22-30` 返回 `list[str]`，Gradio 6 `gr.Gallery` 期望 `list[dict(image,caption)]`。可能静默无预览。建议返回 `[(path, None), ...]`。

**P2-6 Sakura 提示词格式未必匹配** `translate.py:112-122`
用通用 chat 模板调 Sakura。Sakura 官方文档有特定提示格式（system + 用户「将以下日文翻译成中文：…」）。当前格式可能未对齐，质量存疑（实测 3 句对齐成功，但长文本未知）。

**P2-7 zip 收集清单靠 `collect_all` 逐包试错** `build_exe.spec` 已加 gradio/gradio_client/safehttpx/groovy/llama_cpp/unidic_lite/fugashi/manga_ocr。仍可能有其他「importlib.resources 读 version.txt」类包未覆盖，回归时可能再现同型崩溃。

### P3 — 轻微 / 已知局限

- **P3-1** 进度条每阶段重置到 0→满（`app.py:43`），cosmetic。
- **P3-2** `Image.open` 不 close，长时间 UI 会话可能文件句柄泄漏。
- **P3-3** 无跨页翻译上下文 / 术语表 / 角色名一致性（仅逐页批量）。
- **P3-4** 竖排数字/标点未做体位旋转；无 SFX 拟声词专项。
- **P3-5** 检测阈值 `det_threshold=0.3` 未在真实漫画上调过。
- **P3-6** `_parse_numbered` 对长列表对齐脆弱，错位会静默回退逐条调用（多耗 token）。
- **P3-7** EPUB 仅解析 `<img src>` / `<image href>`，CSS 背景图、带查询串的 src 不会被捕获。
- **P3-8** 仓库非 git 仓库，无 `.gitignore`：`tests/`、`dist/`、`models/`、`.venv/` 未隔离。
- **P3-9** `core/config.py:23` 字体路径硬编码 Windows，跨平台不可用。

## 三、EXE 打包专项审计

| 项 | 状态 | 说明 |
|---|---|---|
| 构建可完成 | ✅ | dist\comicgen\comicgen-ui.exe ~3.96GB，UI 启动 ~10s |
| 入口相对导入 | ✅ 修复 | `entry_ui.py` 绝对导入（曾因 `comic_gen/ui/app.py` 作入口报相对导入错） |
| gradio 数据文件 | ✅ 修复 | collect_all 补 safehttpx/groovy 的 version.txt |
| OCR 词典 | ✅ 修复 | collect_all 补 unidic_lite/dicdir |
| manga_ocr 资源 | ✅ 修复 | collect_all 补 manga_ocr/assets |
| detect 阶段（冻结） | ✅ 实测通过 | 用户运行已跑到 OCR 之后 |
| ocr 阶段（冻结） | ✅ 实测通过 | 同上 |
| inpaint 阶段（冻结） | ❓ 未实测 | LaMa 196MB 下载 + 推理未验证 |
| translate（冻结） | ❌ 实测失败 | 缺 Key（配置）；mock/sakura 路径未实测 |
| render（冻结） | ❓ 未实测 | 含 P0-1 字体风险 |
| 模型便携缓存 | ⚠️ 失效 | P1-2 setdefault 被 HF_HOME 击穿 |
| 模型随包内置 | ❌ 未做 | 需手动复制且 rebuild 会清空（C5 工作流坑） |
| rebuild 清空 models | ⚠️ | PyInstaller COLLECT 清 dist/comicgen，手复制的 models 被删，需每次重拷 |

## 四、正确性确认（已验证 OK 的部分，回归时勿破坏）

- 中间产物指纹缓存（`runner.py:_load_pages` 比对 `input` 字段）解决了不同输入污染同一工作目录问题。
- EPUB 打包：mimetype 首项 `ZIP_STORED`、图片原位替换，符合规范（实测）。
- 取色「未膨胀 mask + 腐蚀核心 + 中位数」修复了发灰问题（实测）。
- 深色字不描边 / 浅色字 1px 黑描边策略在样图上表现正确。
- Sakura CPU 链路（GGUF 自动选 IQ4_XS、加载、chat completion、编号对齐解析）dev 下实测通过。
- `--rerender` 译后重渲染机制实测通过。

## 五、建议修复优先级

1. **P1-1** UI 顶层 try/except + 友好错误（立即缓解用户「不知为何报错」）
2. **P0-1** 渲染字体兜底改 `ImageFont.load_default()` 兜底，避免二次崩
3. **P0-2** 检测/抹字阶段逐页 try + 落盘，防整阶段丢失
4. **P0-3** 输出目录覆盖原图的护栏校验
5. **P1-2** EXE 冻结时强制 `os.environ["HF_HOME"]=...`（修便携性）
6. **P1-3/P1-5** EXE 完整端到端回归 + 模型内置/离线方案
7. **P1-4** 模型常驻（UI 进程级缓存，避免每次重载）
8. **P2-1~P2-4** 一致性与兜底（递归扫描、DeepSeek 缺 Key 处理、LaMa 镜像）
9. P3 项按需

> 修复后应重跑：合成样图 mock 全链路（dev）+ EXE 内 mock 全链路（冻结）+ 一卷真实漫画 + DeepSeek Key，方可标记 EXE 可用。