"""OCR 结果校对：把 manga-ocr 认错的字纠回来，再送去排版。

为什么需要：manga-ocr 是日文模型，认繁体中文等于"按日文汉字字形猜"，实测（8 页、
93 个框、714 个汉字）汉字级错误率 18%；逐卷校对又发现大量系统性错法（句号被认成
「：」、疑问语气词「吗」被认成「喂/冯/妈」、日文新字体残留）。重排会把这些错字直接
印到页面上，所以必须在抹字重排之前过一遍校对。

三级规则，都是纯函数、可测：
1. `CONFUSIONS`     单字表：日文新字体/异体字 → 简体标准形，以及扫描件上稳定的形近误认。
2. `_POSITIONAL`    位置规则：同一个错字在不同位置该改成不同字（句尾「喂」→「吗」、
                    「娜边」→「哪边」、「眼你」→「跟你」）。
3. `normalize_punctuation`  标点归一：manga-ocr 把句号逗号认成「：」「；」，省略号认成
                    「．．．」，标点错法几乎固定。

另有一个可选的 LLM 校对（`proofread_lines`），命中率不高（实测只把 18.1% 降到 16.5%），
真正有效的是上面三级规则 + 人工逐页校对。
"""
from __future__ import annotations

import re

try:
    import zhconv
except ImportError:  # pragma: no cover - 可选依赖，仅中文繁转简脚本使用
    zhconv = None

# ---------------------------------------------------------------- 单字表
# 日文新字体 / 日文汉字写法 → 简体中文。manga-ocr 输出大量日文写法（実/戦/様/値/伝…），
# zhconv 只处理中文繁简，不认这些。
_JP_PAIRS = """
実实 戦战 様样 値值 遅迟 剰剩 伝传 応应 険险 竜龙 黒黑 脳脑 説说 巻卷 帰归 気气
対对 経经 験验 単单 発发 変变 続续 読读 撃击 囲围 図图 団团 圧压 拡扩 払挥 抜拔
剤剂 収收 嘆叹 増增 壊坏 売卖 姉姐 専专 島岛 帯带 広广 弾弹 従从 徳德 恵惠 悪恶
悩恼 戸户 拠据 挙举 捜搜 掲揭 敵敌 昇升 晩晚 暁晓 橋桥 歯齿 殺杀 汚污 決决 沢泽
渉涉 満满 焼烧 獣兽 産产 県县 砕碎 確确 稲稻 積积 穏稳 窓窗 競竞 筆笔 粋粹 糸丝
紀纪 約约 紅红 純纯 紙纸 級级 総总 緑绿 線线 練练 縁缘 績绩 罰罚 義义 習习 聴听
職职 臨临 舎舍 薬药 術术 衛卫 製制 複复 覚觉 親亲 観观 計计 訓训 記记 訪访 証证
評评 話话 認认 誕诞 語语 調调 談谈 請请 論论 諭谕 識识 護护 豊丰 財财 貨货 販贩
費费 資资 質质 贅赘 跡迹 践践 車车 軍军 転转 軽轻 載载 輸输 込入 連连 進进 運运
過过 達达 違违 遠远 適适 選选 遺遗 醸酿 針针 鉄铁 銀银 録录 鏡镜 開开 間间 関关
隊队 階阶 際际 隠隐 隣邻 電电 頂顶 順顺 須须 頭头 題题 顔颜 願愿 類类 風风 飛飞
飯饭 飲饮 養养 馬马 駄驮 髪发 魚鱼 鳥鸟 鳴鸣 曽曾 幇帮 弐贰
"""

# 扫描件上稳定出现的形近误认（中文里几乎不成立的误认）。
_SCAN_PAIRS = """
隈喂 廖么 狼很 肥吧 岬吧 苴直 悠你 枪抢 技找 门斗 仕什 幄喔 塙搞 昵呢
"""


def _pairs(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for token in text.split():
        if len(token) == 2:
            out[token[0]] = token[1]
    return out


CONFUSIONS: dict[str, str] = {**_pairs(_JP_PAIRS), **_pairs(_SCAN_PAIRS)}

# ---------------------------------------------------------------- 位置规则
_POSITIONAL = [
    # 句尾的「喂/冯/媽/妈/碼/码/鴫/玛」几乎都是疑问语气词「吗」（"是克林喂？"）；
    # 句首的「喂」是真在喊人，不动。
    (re.compile(r"([\u4e00-\u9fff])[喂冯媽妈碼码鴫玛马嗎]\s*([？?])"), r"\1吗\2"),
    # 没有前字的「马？」（分区的下标偏移会把「是马？」切成一个独立气泡）
    (re.compile(r"马(?=[？?])"), "吗"),
    # 「娜边/娜里/娜个」= 哪边/哪里/哪个。娜本身是人名用字，只在跟方位/量词时替换。
    (re.compile(r"娜(?=[边里个些儿])"), "哪"),
    # 「忘廖/忘蕨」→「怎么」；「痣廖」→「怎么」
    (re.compile(r"忘(?=[廖蕨麼么])"), "怎"),
    (re.compile(r"痣(?=[廖么麽])"), "怎"),
    (re.compile(r"[蕨癩]"), "么"),
    # 「凭廖/凭坠/凭应/凭样」= 怎么（"凭廖可能" → "怎么可能"）；「凭什么」是对的。
    (re.compile(r"凭(?=[廖坠应样蕨])"), "怎"),
    # 「眼你/眼我/眼他/眼着」= 跟；「眼睛/眼泪/眼前…」不动。
    (re.compile(r"眼(?![睛泪前中看神魂镜球角色光])"), "跟"),
    # 整词误认
    (re.compile(r"像似"), "家伙"),
    (re.compile(r"恐帖"), "恐怕"),
    (re.compile(r"憧(?=了)"), "懂"),
    (re.compile(r"岩(?=有此理)"), "岂"),
    (re.compile(r"[拔批北](?=不到)"), "找"),
    (re.compile(r"到(?=子)"), "找"),
    (re.compile(r"喂(?=是)"), "但"),
    (re.compile(r"喂(?=且)"), "而"),
    (re.compile(r"喂(?=睡)"), "沉"),
    # 日文片假名混进中文
    (re.compile(r"オ"), "才"),
    (re.compile(r"カ"), "力"),
    (re.compile(r"己(?=经)"), "已"),
    (re.compile(r"°"), "。"),
    (re.compile(r"怎应"), "怎么"),
    (re.compile(r"这应(?=[早好快多少远])"), "这么"),
    (re.compile(r"那应(?=[早好快多少远])"), "那么"),
    (re.compile(r"阿(?=[？?！!])"), "啊"),
    # 句尾的「玛」是语气词「吗」；句尾的「聊」几乎都是「了/嘛」
    (re.compile(r"玛(?=[，。！？…]|$)"), "吗"),
    (re.compile(r"聊(?=[，。！？…]|$)"), "了"),
]

# ---------------------------------------------------------------- 标点
# manga-ocr 对这批扫描件的标点几乎是固定错法：句点/逗号被认成「：」「；」，
# 省略号被认成「．．．」，强调用的是「｜」，间隔号「・」当逗号。
_PUNCT_RULES = [
    (re.compile(r"(?<!\d)[：；・＇](?!\d)"), "，"),
    (re.compile(r"．{2,}"), "…"),
    (re.compile(r"[\uff5c]"), "！"),  # 全角竖线 U+FF5C，逐个替换（"｜｜" = "！！"）
    (re.compile(r"，{2,}"), "，"),
    (re.compile(r"。{2,}"), "。"),
    (re.compile(r"\s+([，。！？…])"), r"\1"),
    (re.compile(r"([，。！？…])\s+"), r"\1"),
]


def normalize_punctuation(text: str) -> str:
    if not text:
        return text
    out = text
    for pattern, repl in _PUNCT_RULES:
        out = pattern.sub(repl, out)
    return out.strip("，")


# 日文长音符 U+30FC / 半角 U+FF70：OCR 没清掉的残留，竖排下就是一根竖线，
# 连用两个看起来跟「一一」一模一样（2026-11-16 用户实测反馈：以为页面上打了「一一」）。
_DASH_RUN = re.compile(r"[ーｰ]+")
_TOO_MANY_DASH = re.compile(r"—{3,}")


def normalize_dashes(text: str) -> str:
    """把日文长音符（ー / ｰ）改成中文破折号「——」。

    只动 ー，不动全角连字符「－」——后者可能出现在数字/型号里（１２－０）。
    """
    if not text:
        return text
    out = _DASH_RUN.sub("——", text)
    return _TOO_MANY_DASH.sub("——", out)


def render_text(text: object) -> str:
    """重排到页面前该做的文本处理：繁→简 + 长音符归一 + 垃圾串丢弃。

    **刻意不调 apply_confusions**：上面那张单字表是给 OCR 原始输出纠错用的
    （門→门 / 鬥→斗 这类扫描形近误认），作用在已经校对过的译文上会把正确字改坏。
    2026-11-16 实测（死神 74 卷）：1826 格被改错——「为什么不敲门就打开啊」印成
    「为什么不敲斗就打开啊」，「悠闲」变「你闲」，「死鱼眼」变「死鱼跟」。

    空白返回 = 这一格不排字、保留原图（下游 `kept` 会跳过空串）。
    """
    if not text:
        return ""
    s = str(text)
    raw = (zhconv.convert(s, "zh-cn") if zhconv is not None else s).strip()
    if looks_like_ocr_noise(raw):
        return ""
    return normalize_dashes(raw)


# ---------------------------------------------------------------- OCR 幻觉垃圾串
# manga-ocr 面对装饰花纹、页脚小字、排名表、版权页会「幻觉」出成串的日期/法条，
# 这些不是台词。它们一直躺在 .text.jsonl 里没进成品，只因为那些页从没被重排；
# 一旦重排（哪怕是修别的错字）就会把整段乱码印到画面上。
_NOISE_TOKENS = re.compile(
    r"(?:[０-９0-9]{2,4}年|[０-９0-9]{1,2}月|[０-９0-9]{1,2}日|第[０-９0-9一二三四五六七八九十百]+[条回名项次])"
)
_FUNNEL = set("，。、;；:：（）()［］[]％%／/\\-—…“”\"'‘’・·~～!！?？　 ")
_ELLIPSIS_RUN = re.compile(r"[…‥．.]{2,}")
_JP_KANA = re.compile(r"[\u3040-\u30ff]")
_HAN = re.compile(r"[\u4e00-\u9fff]")


def looks_like_ocr_noise(text: str) -> bool:
    """判断一格是不是 OCR 幻觉出来的垃圾串（不是台词）。

    判据都留了余量，宁可漏判也不误杀台词：
    - 短句（<12 字）一律不判；
    - 先把「……」这类连续省略号压成一个字符再算比例，否则正常的「怎……怎么会这样……」
      会因为标点占比高被误判（2026-11-16 实测这条踩过）。
    """
    t = (text or "").strip()
    if len(t) < 12:
        return False
    t = _ELLIPSIS_RUN.sub("…", t)
    n = len(t)
    digits = sum(1 for c in t if c.isdigit() or "０" <= c <= "９")
    kana = len(_JP_KANA.findall(t))
    han = len(_HAN.findall(t))
    punct = sum(1 for c in t if c in _FUNNEL)
    tokens = len(_NOISE_TOKENS.findall(t))
    if han == 0 and (digits + kana + punct) / n >= 0.3:
        return True  # 一个汉字都没有，却排得满：日期/编号表之类
    if n >= 20 and kana / n >= 0.3 and han / n <= 0.5:
        return True  # 成段没翻译的日文（夹着几个汉字的也算）
    if tokens >= 3 and han / n <= 0.5:
        return True  # 成串的日期/条款编号
    return False


def apply_confusions(text: str) -> str:
    if not text:
        return text
    out = "".join(CONFUSIONS.get(ch, ch) for ch in text)
    for pattern, repl in _POSITIONAL:
        out = pattern.sub(repl, out)
    return normalize_punctuation(out)


# ---------------------------------------------------------------- 可选：LLM 校对
_PROMPT = """下面是一页漫画的 OCR 识别结果。这些文字是日文 OCR 模型按字形猜出来的繁体中文，常有形近字错误（例如「隈隈」其实是「喂喂」，「吧」被认成「肥」，「嗎」被认成「媽」）。

请逐条订正错字，规则：
1. 只订正明显认错的汉字，不要改写句子、不要增删内容、不要翻译成别的语言；
2. 不要改标点、全角/半角、阿拉伯数字、英文字母（「１００」不要改成「100」）；
3. 输出必须是简体中文，不要把简体字改成繁体字；
4. 条数和编号必须与输入完全一致，每条一行，格式为「编号. 文本」；
5. 没有把握的条目原样输出，不要臆测。
最后一行输出 END。

{numbered}"""

_LINE_RE = re.compile(r"^\s*(\d+)\s*[.、．:：]\s*(.*)$")


def parse_numbered(raw: str, n: int) -> list[str | None]:
    """解析「编号. 文本」，只信任 1..k 连续前缀。"""
    out: dict[int, str] = {}
    for line in (raw or "").splitlines():
        m = _LINE_RE.match(line.strip())
        if not m:
            continue
        idx = int(m.group(1))
        if 1 <= idx <= n and idx not in out:
            out[idx] = m.group(2).strip()
    trusted = 0
    for i in range(1, n + 1):
        if i not in out:
            break
        trusted = i
    return [out.get(i + 1) if i < trusted else None for i in range(n)]


def _too_different(original: str, fixed: str) -> bool:
    """改得面目全非就别信它——校对只该动几个字，不该改写成另一句话。"""
    a, b = len(original.strip()), len(fixed.strip())
    if a == 0 or b == 0:
        return True
    if b > a * 2 + 4 or b < a * 0.4:
        return True
    common = len(set(original) & set(fixed))
    return common / max(1, len(set(original))) < 0.5


def proofread_lines(lines: list[str], call_llm) -> tuple[list[str], int]:
    """对一页的逐条文本做 LLM 校对，返回 (校对后的列表, 改动字符数)。

    任何解析失败/离题的行都保留原文——校对只能让结果更准，不能让它更乱。
    """
    if not lines:
        return [], 0
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(lines))
    try:
        raw = call_llm(_PROMPT.format(numbered=numbered))
    except Exception:
        return list(lines), 0
    parsed = parse_numbered(raw, len(lines))
    result: list[str] = []
    changed = 0
    for orig, fixed in zip(lines, parsed):
        if not fixed or _too_different(orig, fixed):
            result.append(orig)
            continue
        result.append(fixed)
        changed += sum(1 for a, b in zip(orig, fixed) if a != b) + abs(len(orig) - len(fixed))
    return result, changed
