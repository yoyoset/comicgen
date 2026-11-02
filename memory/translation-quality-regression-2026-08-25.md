# 事故复盘：Sakura 重复句回归（2026-08-25）

## 症状

用户报告当前译文「有大量的重复句」，之前版本没有。逐页分析 493 页杂志的
`pages.json` 后确认三类现象：

1. 同一段竖排文字被翻译/渲染 N 遍（167.JPEG 一条「それでも、」渲染 7 遍）。
2. 一条译文内部循环重复（328.JPEG 一条垃圾 OCR 输入被循环出 1500+ 字的
   「因为是已经知道的所以是已经知道的…」）。
3. 偶发思考块漏进译文（llm 引擎）。

## 根因（三个独立成因）

1. **Sakura `repeat_penalty` 1.1→1.0 回归**。上一轮「切官方 v1.0 调用格式」时，把
   prompt 格式和采样参数放在同一个改动里：官方 README 推荐 repeat_penalty=1.0，
   顺手就改了。但官方参数的前提是参考精度模型；本项目用的 IQ4XS 量化在垃圾 OCR
   输入下 1.0 会进入重复循环。12 页干净漫画验证没触发，上线 493 页杂志就爆了。
2. **RT-DETR 对窄高竖排文字行的扫射式重复框**。同一行竖排文字沿长边生成多个
   y 范围重合的窄条框，OCR 后文本相同，`suppress_overlaps` 的 IoU 判据对这种
   窄框（IoU 只有 ~0.09）无效，于是同一句台词被翻译渲染 N 遍。
3. **`_strip_think_block` 正则写错了 Qwen3 的标签**。真实输出是 ` think... response`
   （5 字母 think + 斜杠闭合），正则写的是 ``（8 字母 thinking）且漏了斜杠，
   从未匹配过真实输出，/no_think 兜底形同虚设。

## 为什么没被拦住

- **变量混在一起**：prompt 格式和 repeat_penalty 在同一个 commit 里改，验证时无法
  归因；12 页对比「官方格式更优」的结论里，repeat_penalty 的影响被混进去了。
- **验证素材不含退化输入**：干净对话页不会触发量化模型的重复循环；垃圾 OCR / 密集页
  才是高发场景。
- **锁定值没有测试守住**：repeat_penalty 是文档里的约定，没有测试兜底，改回去不会
  有任何测试失败。

## 修复（2026-08-25）

- `SakuraTranslator.repeat_penalty` 恢复 `1.1`（基类默认，也是量化模型既有缓解）。
  实测同一条垃圾输入：1.0 → 1248~1540 字循环；1.1 → ~130 字无循环。
- 新增 `detect.suppress_duplicate_text_runs`：OCR 后按「文本相同 + y 重叠 >0.8」
  去重，`runner._ocr_page` 调用。全卷 13 页 / 25 个重复区域被清理。
- `_strip_think_block` 正则修正为 ` ?< ?think(?:ing)?>.*? ?< ?/think>`，实测能剥掉
  Qwen3 真实输出。
- `_is_failed_translation` 增加重复退化检测（同短句 ≥3 字重复 ≥3 次、或输出长度
  > 源文 4 倍 + 100），命中落空串保留原文。
- 配套锁定测试：`test_sakura_repeat_penalty_restored_to_1_1`、
  `test_degenerate_repetition_is_rejected`、`test_strip_think_block_*`、
  `test_duplicate_text_run_*`。

## 教训固化（见 AGENTS.md「变更治理」一节）

1. 控制变量，一次只改一个；改 A 时 B 保持不动。
2. 验证素材必须含退化输入（垃圾 OCR / 短碎片句 / 长上下文）。
3. 别「照官方文档」改已实测校准过的值——官方参数前提是参考精度模型。
4. 锁定值用测试守住，改时测试失败 = 你在改已验证的行为。
5. CHANGELOG 必须记录每个这类改动的实测依据，不写空泛的「优化/修复」。