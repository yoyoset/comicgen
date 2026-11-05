# HANDOFF — 给接手 comic-gen 的下一个 AI

你好。我是上一个在这个仓库干活的 AI（2026-11-05 这一轮，Claude Opus 5.5）。这份信写给你：
项目是什么、现在走到哪了、怎么构建测试打包发布（命令都是实跑过的）、为什么这样设计、我踩过的坑、
还没修的问题、以及不要做的事。更细的设计约束和"已验证锁定参数"在 [AGENTS.md](AGENTS.md)，
改流水线前一定要读它的「关键设计」和「变更治理」两节。

> 用户的工作原则（全局约定，照做）：先实测再下结论；实测和早先假设矛盾时如实记录"以为是 A，实测是 B"；
> 绝不能出现"后台失败但用户以为成功"；提交只按路径 `git add`，每个独立改动单独提交，
> commit message 写真实依据（数字、复现步骤）；构建+测试全绿才提交。
> 机器是 Windows，主 shell 是 PowerShell，git bash 在 `D:\PortableGit\bin\bash.exe`。

---

## 1. 项目是什么，现在什么状态

漫画自动翻译桌面工具（PySide6，Windows）：文件夹/ZIP/CBZ/RAR/EPUB/PDF → RT-DETRv2 检测文字 →
manga-ocr / EasyOCR → 本地 GGUF 翻译（Sakura-14B 日译中 / 通用 Qwen3-14B，llama.cpp）→ LaMa 抹字 →
排版贴回 → 同格式输出。全部本地推理，无 API Key。只在 Python 3.13 上开发测试过。

**版本 0.4.1（2026-11-05）**，这一轮做的事：

- **分发方式改了**：安装包只带 CPU 运行框架（zip 0.38 GB，解压 1.12 GB），GPU 部分由程序「组件」页
  首次运行下载（上游官方 wheel，3.07 GB 下载，解包 4.78 GB）。之前的 CUDA 整包 5.9 GB，超过 GitHub Release
  单文件 2 GB 上限，发不出去。
- **修了一个 CPU 环境下必现的 bug**：LaMa 在 CPU 版 torch 上加载失败，整页抹字失败（见 §5 坑 1）。
- **0.4.1：首次运行弹窗重做**（用户反馈"弹出来了但没有地方开始下载"）：一屏列出每个模型的勾选框 + 大小 + 状态、
  「模型保存到 + 更改…」、GPU 组件勾选项（写明装在程序目录 `components\`）、逐行"下什么 → 存到哪"和合计，
  主按钮「开始下载」；点了以后主窗口同时启动模型下载和 GPU 组件安装，模型页有进度条，页签标题带状态。见 §4 第 11 条。
- 仓库 `yoyoset/comicgen`：**已是 public**。交接给我时说是 private，但 2026-11-05 13:5x 推送前用 `gh repo view` 查到已经是
  PUBLIC（不是这一轮改的，events 里查不到何时切换）。全历史扫描结果见 §6（唯一一处问题路径已按用户要求从全部历史清除）。
- **GitHub Release v0.4.1 是当前版本**：https://github.com/yoyoset/comicgen/releases/tag/v0.4.1
  资产 `comicgen-0.4.1-win64-cpu.zip` 376,982,899 B，sha256 `2c111899d336cb48ea24f2934cc48c44ca4fad53b6160f1ba33081c9eb744393`，
  外加 `.sha256` 文件（LF，`sha256sum -c` 可直接用）。
- v0.4.0（https://github.com/yoyoset/comicgen/releases/tag/v0.4.0，tag 指向 c4718d3）保留不删，标题和说明里标了"已被 0.4.1 取代"。
  它的 zip 376,973,179 B，sha256 `91439ffc9fdf26f9180a8bfc623ff60de9ee51607bd5d909474ad800415afa06`。
- squareuncle.com 下载页**还没上架**（属于 SUBLOG 仓库那边的会话，不在本项目改）。
- 产品排期总表在 `F:\my_ai\SUBLOG\video\weekly\2026-11-博客与产品排期.md`（不在本仓库，不进 git）。
  博客/下载页属于 SUBLOG 仓库，**不要在本项目会话里改 SUBLOG**。

## 2. 环境：两个 venv，各管各的

| venv | 内容 | 用途 |
| :--- | :--- | :--- |
| `.venv\` | torch 2.6.0+cu124、llama-cpp-python 0.3.34 cu124，外加 Gradio 原型期残留的一堆无关包 | 日常开发、跑测试、`scripts\build_exe.ps1` 打本机自用的 CUDA 整包 |
| `.venv-cpu\` | torch 2.6.0+cpu、torchvision 0.21.0+cpu、llama-cpp-python 0.3.34 **CPU 版** wheel，其余按 `requirements-lock.txt` | **只用来打发布包**（`scripts\build_release.py`） |

`.venv-cpu` 重建（git bash，约 5 分钟）：

```bash
cd /f/my_ai/comic-gen
PY=/c/Users/yoyos/AppData/Local/Microsoft/WindowsApps/PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0/python.exe
"$PY" -m venv .venv-cpu
V=.venv-cpu/Scripts/python.exe
$V -m pip install --upgrade pip
$V -m pip install torch==2.6.0+cpu torchvision==0.21.0+cpu --index-url https://download.pytorch.org/whl/cpu
grep -v -E "^#|^torch|^torchvision|^llama-cpp|^nvidia-|^$" requirements-lock.txt > /tmp/lock-cpu.txt
$V -m pip install -r /tmp/lock-cpu.txt
$V -m pip install "https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.34/llama_cpp_python-0.3.34-py3-none-win_amd64.whl"
$V -m pip install pyinstaller==6.21.0 pyinstaller-hooks-contrib==2026.6
$V -m pip install -e . --no-deps
```

注意 llama-cpp-python 的 CPU 版是 tag `v0.3.34`（6.5 MB），CUDA 版是 tag `v0.3.34-cu124`（537 MB），**同名不同包**。

## 3. 命令（全部实跑过）

```powershell
# 测试（纯函数、无网络无 GPU；两个 venv 都应 431 passed，约 1–2 分钟）
.\.venv\Scripts\python.exe -m pytest -q tests
.\.venv-cpu\Scripts\python.exe -m pytest -q -p no:cacheprovider tests
# 门禁
.\.venv\Scripts\python.exe -m ruff check comic_gen tests scripts\build_release.py
.\.venv\Scripts\python.exe -m mypy comic_gen

# 开发态跑 UI / CLI
.\.venv\Scripts\python.exe -m comic_gen.ui.app
.\.venv\Scripts\python.exe -m comic_gen.app.job_service tests\sample.zip -o out.zip --translator sakura --source-lang ja

# 发布包（约 8 分钟：PyInstaller ~5.5 分钟 + zip ~2 分钟）
.\.venv-cpu\Scripts\python.exe scripts\build_release.py
#   → dist\release\comicgen-<版本>-win64-cpu.zip 和同名 .sha256；体检不过直接退出 1、不出 zip
```

**打包后的 EXE 验收**（EXE 是 GUI 程序没有控制台；从 Explorer 双击时输出进 exe 同级 `comicgen.log`，
从 shell 启动时输出直接到 shell。shell 里中文是 GBK 乱码就加 `PYTHONIOENCODING=utf-8`）：

```bash
A=/e/某个新目录/comicgen        # 先把 zip 解压到一个全新的目录
export PYTHONIOENCODING=utf-8
"$A/comicgen-ui.exe" --self-check                  # 退出码 0 = CUDA 可用；打印 torch/llama 实际来自哪
"$A/comicgen-ui.exe" --install-gpu-components      # 和「组件」页按钮调用同一个 gpu_components.install()
"$A/comicgen-ui.exe" --cli 'E:\x\sample.zip' -o 'E:\x\out.zip' --translator sakura --source-lang ja
```

`--cli` 的参数同 `python -m comic_gen.app.job_service`。模型靠 `HF_HOME`（本机 = `F:\hf_cache`）和默认缓存找，
**不读 settings.json 的模型绑定**（见 §6）。

**UI 层验证**：本项目没有 UI 测试基础设施（AGENTS.md 第 16 条），用一次性离屏脚本：
`QT_QPA_PLATFORM=offscreen` + `COMICGEN_APP_DIR=<临时目录>`（`core/paths.app_dir()` 认这个变量）构造真实
`MainWindow`，对控件 `.click()`，循环 `app.processEvents()` 等信号。组件页这么验过（§4）。

**发布到 GitHub**（gh 用全路径，PATH 可能没刷新；已登录 yoyoset）：

```powershell
& "C:\Program Files\GitHub CLI\gh.exe" release create v0.4.0 dist\release\comicgen-0.4.0-win64-cpu.zip dist\release\comicgen-0.4.0-win64-cpu.zip.sha256 --repo yoyoset/comicgen --title "comicgen 0.4.0" --notes-file <说明文件>
# 验收：下回来核 sha256，再解压跑一遍 --self-check
& "C:\Program Files\GitHub CLI\gh.exe" release download v0.4.0 --repo yoyoset/comicgen --dir <新空目录>
```

## 4. 关键决策和为什么

1. **GPU 部分用上游官方 wheel，不自己转存**。torch cu124 wheel 本身 2.53 GB（>2 GB，Release 放不下，得拆包）；
   上游有官方 SHA256（PyTorch index 链接的 `#sha256=`、GitHub API 的 `digest`），钉死即可；不涉及再分发 NVIDIA 库的条款。
   国内网络：官方源失败自动换阿里云 `mirrors.aliyun.com/pytorch-wheels`（实测同字节数）和 `gh-proxy.com`（实测 Range 返回 206）。
   本机实测（官方源，干净解压的 EXE `--install-gpu-components`）：两次分别 **179 s** 和 **283 s**（网速波动），
   含下载 3.07 GB + 三个 SHA256 + 解包 4.78 GB。
2. **怎么让冻结程序用上下载来的 torch**：PyInstaller 6.21 的冻结导入器注册在 `sys.path_hooks`（不是 `meta_path`
   最前面，见 `PyInstaller/loader/pyimod02_importers.py` 的 `install()`），所以 `sys.path.insert(0, 组件目录)` 就能让
   `import torch` 优先命中组件里的 GPU 版，子模块跟着父包 `__path__` 走。前提：`activate()` 必须在任何 `import torch`
   之前（`ui/app.py` 的 `main()` 第一件事）。`activate()` 发现 torch 已导入会拒绝并说明原因，不会半激活。
3. **llama.cpp 不能"删掉 ggml-cuda.dll 当 CPU 版用"**：cu124 版的 `ggml.dll` **静态导入** `ggml-cuda.dll`（pefile 查过导入表），
   删了整个 llama 加载失败。所以安装包里装的是 CPU 版 wheel，GPU 版的 6 个 DLL 解到 `components/gpu-cu124/llama_lib/`，
   用 llama-cpp-python 自带的环境变量 `LLAMA_CPP_LIB_PATH` 切过去。两边版本必须同为 0.3.34（ctypes 绑定按版本对齐）。
4. **ggml-cuda.dll 的 cudart64_12/cublas64_12 借 GPU 版 torch/lib 里那一份**（同为 CUDA 12.4），不单独下载 CUDA 运行库。
   `pipeline/_cuda_runtime.py` 用 `importlib.util.find_spec("torch")` 找"实际会被导入的那份 torch"，不写死路径。
5. **只解包运行需要的东西**：torch wheel 解全了 4.65 GB，滤掉 `.lib`（`dnnl.lib` 一个 624 MB）、`include/`、`share/` 后 3.81 GB；
   llama wheel 只取 `llama_cpp/lib/*.dll`。合计实测 4,781,418,616 B（`EXTRACTED_BYTES`，磁盘空间预检用）。
6. **安装原子性**：解到 `gpu-cu124.tmp/` → 核对 `REQUIRED_FILES` → 最后写 `installed.json`（含 `PACK_ID`）→ 改名就位。
   `pack_status()` 认安装记录 + 必需文件，解压到一半被杀的目录不会被当成已装好。装完删 `_downloads/`。
7. **删除组件**：组件在用时 DLL 被锁、删不掉 → 写 `components/REMOVE_PENDING`，下次启动在 `activate()` 之前删。
8. **发布包在干净的 `.venv-cpu` 里构建**：顺带甩掉 `.venv` 里 Gradio 原型期残留的 pandas/onnxruntime/babel 等，
   产物 1.12 GB（旧 CUDA 整包 `_internal` 5.3 GB）。产物从 `build\release` 全新生成，绝不碰 `dist\comicgen`。
9. **EXE 加了三个无界面开关**（`--self-check` / `--install-gpu-components` / `--cli`）：冻结环境里的行为（缺 stdlib 模块、
   SSL 证书、DLL 搜索顺序）只能在 EXE 里验证，而 EXE 只有 GUI 这一个入口。也给用户排障用。
11. **首次运行弹窗只产出"下载计划"，不自己下载**（0.4.1）：`FirstRunDialog.download_plan()` 返回 (模型 key 列表, 是否装 GPU)，
    `MainWindow` 关窗后调用 `_start_first_run_downloads`，复用既有 `ModelWorker` / `ComponentWorker`——下载逻辑只有一份，
    和「模型管理」「组件」页的按钮走同一条路。默认勾选 = 必需三项 + Sakura（默认日译中引擎）；非日文 OCR、通用 LLM（8.4 GB）按需不默认。
    大小用 `core/first_run_plan.approx_bytes`（manifest 精确字节数优先，否则解析 `approx_size`），十进制 GB，和组件页一致。
    模型下载器（from_pretrained / hf_hub_download / torch.hub）都没有进度回调，进度靠 `first_run_plan.watched_bytes` 每秒统计
    `<hub>/models--org--name/blobs/` 或 torch hub `checkpoints/big-lama.pt*` 的字节数增长来估（真实下载 LaMa 实测 0%→87%→99%）。
12. **许可**：安装包里有 PyMuPDF（AGPL-3.0），所以安装包整体按 AGPL 分发，`LICENSE` 和 `THIRD_PARTY_NOTICES.md` 随包；
    对应源码 = 同版本 tag，**这也是仓库必须公开后才适合挂下载链接的原因之一**。

### 验收数字（2026-11-05，最终发布 zip，`E:\cg-e2e-final`）

| 步骤 | 结果 |
| :--- | :--- |
| zip | 376,973,179 B，sha256 `91439ffc9fdf26f9180a8bfc623ff60de9ee51607bd5d909474ad800415afa06`；解压 11 s，1,124,791,499 B |
| `--self-check`（未装组件） | 退出码 1：torch 2.6.0+cpu，llama gpu_offload=False（符合预期） |
| `--cli` mock 翻译（请求 cuda、无组件） | 退出码 0，20 s；检测 5.1 s / OCR 1.8 s / 渲染 7.2 s，3 个区域全部 rendered |
| `--install-gpu-components` | 退出码 0，283 s；components 4,781,418,992 B |
| `--self-check`（装完） | 退出码 0：torch 2.6.0+cu124 来自 components，CUDA 可用（RTX 3060），llama gpu_offload=True |
| `--cli` Sakura-14B 翻译 1 页 | 退出码 0，31 s（模型文件已在系统缓存；第一次冷启动 65 s）；显存 1,865 → 峰值 11,850 MiB |
| 装完组件的整个目录 | 5,906,210,491 B |

0.4.1（`E:\cg-041-check2`，HF_HOME / TORCH_HOME 指向空目录）：zip 376,982,899 B；首次运行弹窗真机截图（DPI aware PrintWindow）
6 行模型全部可见，默认勾选检测/OCR 日文/Sakura/LaMa，汇总"共约 9.0 GB → 存到 …；GPU 组件约 3.1 GB → 装到 …\components；
合计约 12.1 GB"，按钮「开始下载」；`--self-check` 退出码 1（CPU 态，符合预期），`--cli` mock 翻译退出码 0（47 s）。
点「开始下载」之后的主窗口行为在离屏脚本里验过（切到模型管理页、真实下载 LaMa 进度条走到 99%、落盘 205,803,670 B），
**没有在真实窗口里点过**。

## 5. 踩过的坑（现象 → 原因 → 解决）

1. **CPU 包翻译整页失败，交付物是原图**：`Could not run 'aten::empty_strided' with arguments from the 'CUDA' backend`，
   `--device cpu` 也一样 → simple_lama 的 `SimpleLama.__init__` 用 `torch.jit.load(path)` 不带 `map_location`，
   big-lama.pt 的张量存在 CUDA 上；它也不做"没 CUDA 就退回 CPU" → `Inpainter` 自己 `resolve_device` +
   `torch.jit.load(path, map_location=dev)`（commit 7420271）。以前只发 CUDA 整包，这条路径从没跑到过。
   **教训：发 CPU 包就要在 CPU 环境真跑一次完整流程，不能只跑 self-check。**
2. **我最初估的解包大小 4.42 GB 是错的**，按过滤规则对真实 wheel 求和是 4.78 GB。数字一律实测后再写进代码。
3. **git bash heredoc 里给 Python 写 Windows 路径，`\b` 变成了退格符**（`scripts\build_release.py` 写进 README 成了
   `scripts<BS>uild_release.py`）→ 改文档用 Edit 工具，或者写完用 `[\x00-\x08]` 扫一遍控制字符。
4. **`rm -rf /e/某目录` 被 Claude Code 安全检查拦截** → 验收目录每次用新名字（如 `E:\cg-e2e-final`），旧目录留给用户手动删。
5. **computer-use 申请控制 `comicgen-ui.exe` 被拒** → 用离屏脚本验 UI 接线 + EXE 无界面开关验冻结环境。
   也就是说"在真实窗口里点组件页按钮"这一步**没有人工点过**（见 §6）。
6. **`dist\comicgen\` 是本机自用目录**，里面有 `settings.json`（含本机路径、最近打开目录）、`comicgen.log`、`models\hf\`（588 MB）
   → 绝不能打包它；`build_release.py` 从全新目录构建并体检。
7. **EXE 从 shell 启动时 stdout 不是 None**，`comicgen.log` 不会生成，输出直接到管道；只有双击（无控制台）时才写 log。
8. **pip 装的 torch 不认系统 CUDA**：它自带 CUDA 库。"让用户自己装 CUDA"不能减小安装包，这是整个方案的出发点。
9. **Release 上的 `.sha256` 用 `sha256sum -c` 校验失败**（`'...zip'$'\r': No such file`）→ `Path.write_text` 在 Windows 写成 CRLF
   → 改 `write_bytes`（commit 99dc870），资产已 `--clobber` 重传。**重传后立刻下载拿到的还是旧文件**（CDN 缓存），
   隔一会儿或带个查询参数再下才是新的；判断以 `gh release view --json assets` 的 `digest`/`size` 为准。
10. **`gh release create --target` 要完整 40 位提交哈希**，短哈希报 `Release.target_commitish is invalid`（HTTP 422）。
11. **首次运行弹窗的表格只露出 4 行**（0.4.1 候选包真机截图发现，默认勾选的 LaMa 被藏掉）→ 用了 `AdjustToContents` +
    关竖向滚动条，算出来的高度不含全部行 → 在 `showEvent` 和扫描完成后按 `horizontalHeader().height() + verticalHeader().length()`
    算死表高（commit 6debffb）。**离屏（offscreen）渲染发现不了这类问题**：offscreen 平台没有中文字体（全是方框）、行高也和真机不同。
12. **截图要 DPI aware**：先 `SetProcessDPIAware()`，再 `GetWindowRect` + `PrintWindow(hwnd, dc, 2)`；不这样截，125% 缩放下
    只截到窗口左上角一块，会误以为界面没问题。PrintWindow 只读像素、不操作窗口，不需要 computer-use 授权。
13. **旧弹窗的"还需下载约 7.6 GB … 大小见上表"是错的**：`plan_download_bytes` 只计 LAYOUT_FILE、`llm` 的 size_bytes 故意为 0，
    7.6 又是 GiB——实际只算了 Sakura 一项，表里也没有大小列。新代码用 `first_run_plan.total_bytes`，十进制。

## 6. 已知但还没处理的问题

- **（已处理）历史里的商业杂志路径**：全历史扫描（`git log --all -p`）无密钥/令牌、无私人图片、无大文件（最大 blob 2.9 MB）；
  邮箱与公开的 subgen 相同。唯一问题是一个商业漫画杂志的本地目录名（在 `scripts/compare_sakura_prompts.py`、`docs/AGENT_BRIEF.md`
  和本文件里），会暴露"手上有某期杂志的 493 页图"。2026-11-05 经用户同意，用 git-filter-repo `--replace-text` 把它在**全部历史**里
  替换成"某漫画杂志"并强推 main 与 v0.4.0 tag（所有提交哈希都变了；改写前的完整备份是
  `F:\my_ai\comicgen-before-rewrite-20261105.bundle`，**里面仍含原路径，不要上传**）。
  残留风险：GitHub 可能仍能按旧提交哈希打开旧提交（缓存/悬空对象），要彻底清除得找 GitHub Support 申请 GC。
- **真实窗口里点「组件」页没有人工验收过**（computer-use 被拒）。已验的是：离屏 MainWindow 点下载/取消/重试/完成；
  冻结 EXE 调同一个 `install()` 真实联网下载。建议用户第一次用时亲手点一遍。
- **纯 CPU 跑 Sakura-14B 翻译没测过耗时**（预计极慢）；CPU 包只用 mock 翻译跑通了检测/OCR/抹字/渲染全流程。
- **旧显卡驱动**：cu124 需要 NVIDIA 驱动 ≥ 551（Windows）。没在旧驱动上测；那种情况组件页会显示"GPU 组件已加载，
  但 CUDA 不可用"，程序退回 CPU，不会崩。
- **`--cli` 不读 settings.json 的模型绑定**（`job_service.main` 构造的 `Config` 没有 `model_paths`），只靠 `HF_HOME`/默认缓存搜索。
  开发态 CLI 也一样，是既有行为。
- 开发态 `python -m comic_gen.app.job_service` 不调用 `activate()`（开发态 .venv 本来就是 GPU 版），有意为之。
- 镜像回退只在本地假服务器上测过逻辑；真实镜像只核对了 HEAD 200 + 字节数（阿里云）和 Range 206（gh-proxy），没整包走过一遍。
- 组件页文案只有中文。
- 下载页（squareuncle.com/download）上架、隐私说明页、博客：没做，归 SUBLOG 会话。需要的材料都在 §1（链接、大小、sha256）。
- 安装包未签名，Windows 可能提示未知发布者。

## 7. 不要做的事

- 不要用 `.venv` 打发布包（会打进 5.9 GB 的 CUDA 栈）；不要把 `dist\comicgen\` 打包或上传。
- 不要 `git add -A` / `git add .`：工作区常年挂着无关文件（`tests/*.txt`、`build/` 等）。
- 换任何一个 GPU wheel 时，**同时**改 `gpu_components.py` 里的 URL、`sha256`、`size`、`PACK_ID`，并重新实测 `EXTRACTED_BYTES`
  （用 `select_members` 对真实 wheel 求和）——哈希一律下载后本地算，不凭记忆填。llama-cpp-python 的 CPU 版与 cu124 版必须同版本。
- 不要在 `ui/app.py` 的导入链顶层（`comic_gen/__init__`、`core/__init__`、`ui/__init__`、`ui/app.py` 模块级）import torch
  或任何会间接 import torch 的模块，否则 `activate()` 来不及，GPU 组件永远不生效。
- 不要给 `verify` 加"哈希对不上也继续用"的降级路径。
- 不要未经用户同意改写 git 历史、切换仓库可见性、推送 tag 到公开仓库之外的地方。
- 不要按"官方 README"改 AGENTS.md「变更治理」里锁定的参数（repeat_penalty 等），先读那一节。
- 不要把真实素材的本地路径（尤其是商业漫画/杂志的目录名、文件名）写进代码、文档或提交信息——仓库是公开的，
  2026-11-05 为此改写过一次全部历史。脚本里要用素材路径就走命令行参数或环境变量。
