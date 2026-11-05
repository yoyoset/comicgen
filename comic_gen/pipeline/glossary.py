"""专有名词译名表（术语表）：让同一卷里人名/招式名只出现一种写法。

为什么需要：翻译引擎按页按块独立推理，不认识"这卷的主角叫花道"，实测（2026-11-05
灌篮高手英文版 v01）同一卷里 HANAMICHI 被译成「花火」和「花道」两种写法——更糟的是
EasyOCR 对风格化英文字体的识别本来就会把 `HANAMICHI` 认成 `HANAMiCHiW`，模型只能
按发音瞎猜。这里做两件事：

1. **源文侧替换**（`apply_glossary`）：翻译前把原文里能匹配上的专有名词直接换成既定
   译名，模型就不必猜了。匹配对大小写、撇号、以及 OCR 常见的增删字母容忍（`HANAMiCHiW`
   与 `HANAMICHI` 的相似度 0.94，按阈值算命中）——这正是靠提示词做不到的那部分。
2. **提示词侧约束**（`glossary_prompt`）：把整张表塞进 llm 引擎的 system_prompt，
   让模型在替换没命中时也知道既定译名。

全部是纯函数（不碰模型/网络），单测直接覆盖。术语表由用户提供（CLI `--glossary`）。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# 模糊匹配阈值：0.85 实测能吃掉 OCR 把 HANAMICHI 认成 HANAMiCHiW（0.94）、
# SAKURAGI 认成 SAKURAGIS（0.94）这类小错，又不会把 DAISUKI 认成 SUZUKI（0.77）。
_FUZZY_MIN_RATIO = 0.85
# 太短的名字不做模糊匹配（"Ai"、"Qi" 这种对任何 3-4 字母词都可能过线），只认精确命中。
_FUZZY_MIN_LEN = 4
# 模糊匹配额外要求长度差不超过 2，避免 "AKAGI" 吃到 "AKAGIRA"（相似度 0.8，已被阈值拦下，
# 但这条让判据更直观：OCR 的错法基本是单个字母的增删改）。
_FUZZY_MAX_LEN_DELTA = 2

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\u2019]*")
_PUNCT_RE = re.compile(r"[^A-Za-z]")

# 系统提示词里最多带多少条译名：译名表是给模型"认人"用的，不是语料库里那样能写几百行；
# 塞太多会挤占 n_ctx（translate.py 的 4096）且稀释指令本身。
_PROMPT_MAX_ENTRIES = 60
_PROMPT_MAX_CHARS = 900


def normalize_term(s: str) -> str:
    """归一化到"只留字母、全小写"，用于比较 OCR 文本和术语表原文。"""
    return _PUNCT_RE.sub("", s).lower()


def parse_glossary_text(text: str) -> dict[str, str]:
    """解析术语表文件：每行 `原文=译名`，`#` 开头与空行忽略，重复原文后者覆盖前者。

    故意只按第一个 `=`/`＝`/制表符切分：译名里出现等号不常见，但原文里出现空格很常见
    （"Hanamichi Sakuragi=樱木花道"），不能被空格切坏。原文或译名为空的行整行丢弃
    ——静默忽略比让一条坏行污染整个映射表安全。
    """
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=", "\uff1d", "\t"):
            if sep in line:
                src, _, dst = line.partition(sep)
                break
        else:
            continue
        src, dst = src.strip(), dst.strip()
        if not src or not dst:
            continue
        result[src] = dst
    return result


def glossary_prompt(glossary: dict[str, str]) -> str:
    """把术语表拼成 system_prompt 的附加段（空表返回空串，调用方可直接相加）。"""
    if not glossary:
        return ""
    pairs: list[str] = []
    used = 0
    for src, dst in glossary.items():
        piece = f"{src}={dst}"
        if len(pairs) >= _PROMPT_MAX_ENTRIES or used + len(piece) > _PROMPT_MAX_CHARS:
            break
        pairs.append(piece)
        used += len(piece) + 1
    if not pairs:
        return ""
    return "专有名词的既定译名（必须照此翻译，不要自行音译或意译）：" + "；".join(pairs) + "。"


def _token_spans(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(0)) for m in _TOKEN_RE.finditer(text)]


def _similar(a: str, b: str) -> bool:
    """单个词与术语原文是否算同一个名字。"""
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) < _FUZZY_MIN_LEN or len(b) < _FUZZY_MIN_LEN:
        return False
    if abs(len(a) - len(b)) > _FUZZY_MAX_LEN_DELTA:
        return False
    return SequenceMatcher(None, a, b).ratio() >= _FUZZY_MIN_RATIO


def apply_glossary(text: str, glossary: dict[str, str]) -> str:
    """把 text 里出现的专有名词替换成术语表里的既定译名。

    先整词表（"Hanamichi Sakuragi" 这类多词原文作为一个整体匹配，避免被拆成两个词
    各替换一次产生"樱木花道 樱木花道"），再把剩下的单词逐个匹配。匹配失败原样保留，
    绝不猜测——宁可这一处仍让模型自己翻，也不能把不相关的词替换成别人的名字。
    """
    if not glossary or not text:
        return text
    tokens = _token_spans(text)
    if not tokens:
        return text
    norms = [normalize_term(tok) for _, _, tok in tokens]

    single: list[tuple[str, str]] = []
    multi: list[tuple[list[str], str]] = []
    for src, dst in glossary.items():
        words = [normalize_term(w) for w in src.split()]
        words = [w for w in words if w]
        if not words:
            continue
        if len(words) == 1:
            single.append((words[0], dst))
        else:
            multi.append((words, dst))
    multi.sort(key=lambda item: len(item[0]), reverse=True)

    # 每个 token 的替换结果：None = 未命中（保持原样）
    replaced: list[str | None] = [None] * len(tokens)

    # 1) 多词原文：连续 token 归一化后逐词相等才算命中（短语只做精确匹配，
    #    跨词做模糊匹配的误伤面太大，收益也不高）。
    for words, dst in multi:
        n = len(words)
        for i in range(len(tokens) - n + 1):
            if any(replaced[i + k] is not None for k in range(n)):
                continue
            if norms[i : i + n] == words:
                for k in range(n):
                    replaced[i + k] = dst

    # 2) 单词：精确或模糊命中。同一次替换里多个名字都可能命中（如 "SAKURAGI!"），
    #    按术语表顺序取第一个，保证结果稳定可测。
    for i, norm in enumerate(norms):
        if replaced[i] is not None or not norm:
            continue
        for src_norm, dst in single:
            if _similar(norm, src_norm):
                replaced[i] = dst
                break

    if not any(r is not None for r in replaced):
        return text

    out: list[str] = []
    cursor = 0
    for (start, end, _tok), replacement in zip(tokens, replaced):
        out.append(text[cursor:start])
        out.append(replacement if replacement is not None else text[start:end])
        cursor = end
    out.append(text[cursor:])
    joined = "".join(out)
    return _collapse_repeated_targets(joined, set(glossary.values()))


def _collapse_repeated_targets(text: str, targets: set[str]) -> str:
    """把相邻重复的同一译名收成一个。

    场景：原文 "Sakuragi Hanamichi" 里的两半如果分别在术语表里，会各自替换成
    "樱木花道 樱木花道"；中文里这个名字说一次就够。只处理术语表自己的译名，
    不会误伤普通重复词。
    """
    for target in targets:
        if not target:
            continue
        pattern = re.escape(target) + r"(?:[\s、,，]+" + re.escape(target) + r")+"
        text = re.sub(pattern, target, text)
    return text
