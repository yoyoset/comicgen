# 稳定性审计轮：无限成长 + 幻觉/占位/硬编码

- **版本**：0.2.0（上一阶段 0.1.0）
- **日期**：2026-08-09
- **范围**：两轮只读审计 + 定向修复，覆盖全代码库；产物全部回归通过

本阶段是「全局工程原则」框架（系统构成 / 工作方式）在这台机器、这个代码库上的一次完整应用。
所有「已实测」声明都在下方留有验证方法，不写空话。

---

## 一、轮 1：无限成长问题审计与修复

### 发现（按严重度）

| 级别 | 问题 | 位置 | 依据 |
|---|---|---|---|
| 高 | 冻结 EXE 的 `comicgen.log` 以 `"a"` 追加、永不截断，模型加载的 tqdm 进度条等噪音每 run 累积 | `ui/app.py:47` | 代码审读；dev 运行里 tqdm「Loading weights」每次模型加载刷几百行 |
| 高 | `run.log` 同一工作目录跨运行追加（FileHandler 默认 mode="a"），文件夹输入默认复用工作目录，长年校对/重渲染无上限增长 | `infra/logging.py:59` + `pipeline/runner.py:262` | **实测复现**：同目录 attach 两次各写一条警告 → 文件 2 行叠着两轮 |
| 中 | `BatchController._retired_threads` 只进不出，长跑会话无上限 | `ui/batch_controller.py:51,153` | 代码审读（wait 超时窗口才触发，正常近空） |
| 已知 | `_trash/` 软删除累积（查重去重只是改名，磁盘不释放，单份 GGUF 几 GB） | `core/model_locator.py:364` + `ui/main_window.py:559` | 沿用 AGENTS.md「已知待办」 |
| 已知 | HF 下载失败残留 `.incomplete` | hub 缓存 | 沿用 AGENTS.md「已知待办」，当前 0 个 |

### 修复

1. `ui/app.py:47`：冻结 EXE 的 `comicgen.log` 改 `mode="w"`，每次启动截断、只保留最近一次会话。excepthook 仍 `"a"`（会话内崩溃堆栈累积，启动已重置，语义不变）。
2. `infra/logging.py:59`：`attach_job_file_handler` 改 `mode="w"`，每次 run 从干净开始。恢复/排错以 `pages.json`/`job.json` 为准，`run.log` 只负责当次现场。
3. `ui/batch_controller.py:147`：`_reap_thread` 先剔除 `isFinished()` 的 retired 线程，列表只留仍在 running 的。

### 验证

- **实测复现**（修复前）：同 work_dir 两轮 attach → `run.log` 两轮叠加。
- **回归测试**：`tests/test_infra_logging.py::test_job_file_handler_truncates_between_runs`（第二轮不含第一轮内容）。
- **离屏脚本**：`_reap_thread` 只保留 running 线程（`QT_QPA_PLATFORM=offscreen`）。
- 全量 367 测试通过；ruff 通过；mypy 仅既有无关 notes。

---

## 二、轮 2：幻觉 / 占位 / 硬编码审计与修复

### 幻觉（注释/文档声称 vs 实际）——抽查验证结果

| 声称 | 核对方法 | 结论 |
|---|---|---|
| manifest `size_bytes`「已实测核实」 | 与本机实际权重文件逐字节比对 | sakura 4,250,298,208 ✓ / llm 2,497,281,120 ✓ / lama 205,803,670 ✓ |
| render.py「msyh.ttc size 10~48 ascent+descent 恒为 1.33~1.40 倍字号」 | PIL 实测 6 个字号 | 1.333~1.400 ✓ |
| `_ECHO_MARKERS` 取自提示词字面量 + 有防漂移断言 | 读 `tests/test_translate_batch.py:148` | 断言存在且通过 ✓ |
| 大量「实测复现」（0xC0000409、SSL UNEXPECTED_EOF、493 页样本数字） | 抽查风格与复现路径 | 具体、可追溯，未见捏造 |

**真不一致（本轮修复）**：`registry.py` 顶部注释与 `AGENTS.md` 第 8 条都声称硬编码分发只剩
`ui/workers.py` 下载面板 + `translate_view.py` 引擎下拉两处，**漏了 `app/job_service.py` 的 CLI
`choices`**。加第三个引擎/后端实际要改 5 处而非注释说的 2 处，会让人漏改 CLI。

### 占位——基本干净

- `BaseTranslator.translate` 的 `NotImplementedError` 是抽象方法，正确。
- `MockTranslator` 是正式自测引擎（`registry` 中 `needs_model=False`），不是占位。
- 无 TODO/FIXME/XXX；全部 `except: pass` 为防御性兜底（环境变量/线程清理）。
- `target_lang`「当前仅简体中文，预留」——字段贯通 Config/prompt，UI 不暴露属设计决定。

### 硬编码——分类处理

- **注册表外重复（真 DRY 问题，已修）**：
  - `app/job_service.py` 的 `--translator`/`--source-lang` choices 改从注册表推导。
  - `pipeline/registry.py` 新增 `SUPPORTED_SOURCE_LANGS`（源语言合法值单一来源）+ 防漂移断言（与 OCR 注册表语言并集一致，漂移在 import 时即炸）。
  - `pipeline/translate.py`：`n_ctx=4096` 补理由注释（与分块预算 `_MAX_CHUNK_LINES`/`_OUTPUT_TOKEN_CEILING=1024` 的联动关系及调整条件）。
- **标注「未校准」的经验阈值**（有意、有注释、符合纪律，**不修**）：`detect.py` score≥0.4 / 竖排 1.15 倍、`ocr.py` conf≥0.3、`inpaint.py` 0.005~0.6/0.15、`render.py` 列距 1.35/字距 1.05、`review_session.py` 0.5——均已登记 AGENTS.md 已知待办。
- **字体兜底列表 / manifest size_bytes/magic**：固定版本校验的刻意设计，不算问题。

### 验证

- 新增 `tests/test_app_job_service.py::test_cli_choices_derive_from_registry`（choices 与注册表一致、顺序不变，防未来改回硬编码）。
- 368 测试通过；ruff 通过；mypy 仅既有无关 notes；`--help` 实测 choices 正常。

---

## 三、行为变化（交接必读）

1. **`comicgen.log`（冻结 EXE）每次启动从头开始**——只保留最近一次会话。跨会话找旧崩溃记录不再可行，以当次会话日志为准。
2. **`run.log` 每次 run 从干净开始**——同一工作目录多次运行的旧记录被清。断点恢复仍靠 `pages.json`（每页原子写、崩溃自愈），排错配置快照靠 `job.json`，不受影响。
3. **CLI choices 自动跟随注册表**——加新引擎/新语言只改 `registry.py`，CLI 自动同步；`SUPPORTED_SOURCE_LANGS` 与 OCR 注册表语言并集的漂移在 import 时即崩溃（fail-fast）。

## 四、明确不做的（范围控制）

- `_trash/` 软删除清理策略（真删/回收站/保留期限）：需用户决定，GGUF 单份几 GB，不能替用户擅自删。
- HF `.incomplete` 残留清理：沿用已知待办。
- `translate_view.py` 引擎下拉、`workers.py` 下载面板仍硬编码：项目既定范围控制，加第三个引擎/后端时一起改（届时 registry 注释已如实列出全部待改点）。
- B 类「未校准」经验阈值：等有真实漫画素材能跑 `scripts/diagnose_page.py` 再校准。

## 五、下一步建议

1. 有真实漫画素材后校准 B 类阈值（detect score / 竖排方向 / OCR conf / inpaint 启发式）。
2. 决定 `_trash` 与 HF 残留的清理策略并落地。
3. 加第三个翻译引擎/OCR 后端时，按 registry 注释的新清单把 `translate_view`/`workers` 一并接线。
