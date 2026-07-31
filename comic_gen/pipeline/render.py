"""译文渲染：横排/竖排，字号自适应收缩防溢出，居中，自动描边。"""
from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from ..core.config import Config
from ..core.models import DIRECTION_HORIZONTAL, DIRECTION_VERTICAL, Region
from ..infra.logging import logger

_COL_GAP = 1.35   # 竖排列距倍数


_FONT_FALLBACKS = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf",
    "/System/Library/Fonts/PingFang.ttc",
)


@lru_cache(maxsize=None)
def _load_font(path: str, size: int):
    """多级兜底：指定字体 → 常见系统 CJK 字体 → PIL 默认字体（绝不抛异常）。

    加缓存：_pick_size 的字号搜索循环里每个候选字号都会调一次，不缓存等于每页每个
    区域都要重新打开一遍字体文件。
    """
    for p in (path, *_FONT_FALLBACKS):
        if not p:
            continue
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _line_advance(font) -> float:
    """一行的实际竖直进给：ascent+descent。

    实测 msyh.ttc 在 size 10~48 下 ascent+descent 恒为 1.33~1.40 倍字号，旧代码假设的
    size*1.3 系统性偏小，导致相邻行互相压叠。非 FreeType 字体理论上不会走到这条流水线，
    但仍加 getattr 兜底避免异常打断整页渲染。
    """
    metrics = getattr(font, "getmetrics", None)
    if metrics is not None:
        try:
            ascent, descent = metrics()
            return ascent + descent
        except Exception:
            pass
    return font.size * 1.35


def wrap_by_width(text: str, measure, max_w: float) -> list[str]:
    """按宽度贪心换行。measure(s) 返回字符串 s 的度量宽度——依赖注入使其不依赖 PIL，
    测试可以直接传 len 验证逻辑，不需要加载字体文件。
    """
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if cur and measure(cur + ch) > max_w:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def _wrap_horizontal(draw, text: str, font, max_w: int) -> list[str]:
    return wrap_by_width(text, lambda s: draw.textlength(s, font=font), max_w)


# 行首禁则：这些字符不能出现在一行的开头（收尾类标点，前面必须有内容跟着）
NOT_LINE_START = "，。、；：？！）］｝〉》」』】’”·…—～%"
# 行尾禁则：这些字符不能出现在一行的结尾（起始类标点，后面必须有内容跟着）
NOT_LINE_END = "（［｛〈《「『【‘“"


def apply_kinsoku(lines: list[str], measure, max_w: float) -> list[str]:
    """禁则处理：不产生"行首是句号/顿号"或"行尾是左括号"这类断行。

    只用"下移末字"（把上一行最后一个字挪到下一行开头，把违规标点顶到下一行的第二个
    字符位置）不用"上提首字"（把违规标点拉回上一行末尾）——前者只会让"送出方"（上一行）
    变短，绝不会让它超宽；后者会让上一行变长，可能反而超宽。

    但"接收方"（下一行）会因为多了一个字符而变长——这一侧必须显式检查 measure(...) <=
    max_w，否则会把"违规标点"问题换成"文字溢出边框"问题，视觉上更差（曾实测复现：
    "很" 被下移后使接收行超出气泡框，画面上文字直接越界）。测不出还超宽就放弃这一步，
    宁可保留原始的禁则违规，不产生溢出。

    最多迭代 3 轮防止相邻两行来回振荡；任何一步会把某一行掏空（只剩 1 字还要继续挪）
    就整体放弃，返回原始 lines，因为这说明这几行本来就挤得没有调整空间。
    """
    if len(lines) < 2:
        return list(lines)
    working = list(lines)
    for _ in range(3):
        changed = False
        for i in range(1, len(working)):
            while (
                working[i]
                and working[i][0] in NOT_LINE_START
                and len(working[i - 1]) > 1
                and measure(working[i - 1][-1] + working[i]) <= max_w
            ):
                moved, working[i - 1] = working[i - 1][-1], working[i - 1][:-1]
                working[i] = moved + working[i]
                changed = True
            while (
                working[i - 1]
                and working[i - 1][-1] in NOT_LINE_END
                and len(working[i - 1]) > 1
                and measure(working[i - 1][-1] + working[i]) <= max_w
            ):
                moved, working[i - 1] = working[i - 1][-1], working[i - 1][:-1]
                working[i] = moved + working[i]
                changed = True
        if not changed:
            break
    if any(not ln for ln in working):
        return list(lines)
    return working


def balance_lines(text: str, measure, max_w: float) -> list[str]:
    """行平衡：贪心换行常在最后一行留下 1~2 个字的孤字行；用比 max_w 更窄的宽度重排，
    只要仍能排成同样的行数，前面的行就会主动少装一点，把孤字分摊回去。

    二分查找仍能保持行数不变的最小宽度。行数随宽度减小单调不减，可以二分。
    """
    baseline = wrap_by_width(text, measure, max_w)
    n = len(baseline)
    if n <= 1:
        return baseline
    lo, hi = 1.0, float(max_w)
    best = baseline
    for _ in range(16):
        mid = (lo + hi) / 2
        candidate = wrap_by_width(text, measure, mid)
        if len(candidate) == n:
            best = candidate
            hi = mid
        else:
            lo = mid
    return best


def _fits(draw, text: str, font, bw: int, bh: int, direction: str, sw: int = 0) -> bool:
    """sw（描边宽度）会让实际墨迹比字号本身宽/高各多出 sw 一圈，排版判定要预留出来。"""
    eff_bw, eff_bh = max(1, bw - 2 * sw), max(1, bh - 2 * sw)
    if direction == DIRECTION_VERTICAL:
        per_col = max(1, int(eff_bh // (font.size * 1.05)))
        n_cols = -(-len(text.replace("\n", "")) // per_col)
        return n_cols * font.size * _COL_GAP <= eff_bw
    return len(_wrap_horizontal(draw, text, font, eff_bw)) * _line_advance(font) <= eff_bh


def _stroke_for(color, size: int) -> tuple[tuple, int]:
    """浅色文字（气泡外白字）配黑描边，宽度随字号缩放；深色文字不描边（气泡内描边会糊）。

    旧实现描边固定 1px，不随字号变化——小字描边显得过粗、大字描边显得过细。
    """
    if sum(color) >= 384:
        return (0, 0, 0), max(1, round(size * 0.06))
    return (0, 0, 0), 0


def _pick_size(draw, text: str, bw: int, bh: int, direction: str, cfg: Config, color):
    """返回 (font, stroke_width, fits)。fits=False 表示缩到 min_font_size 仍然放不下。"""
    # 竖排原先的上界只看 bh，完全没用到 bw——单列文字所需宽度 size*_COL_GAP 也可能超框宽，
    # 从 max_font_size 起试没有意义。横排本来就用 bw，这里统一处理不分方向。
    hi = min(cfg.max_font_size, bh, bw)
    for size in range(hi, cfg.min_font_size - 1, -2):
        font = _load_font(cfg.font_path, size)
        sw = _stroke_for(color, size)[1]
        if _fits(draw, text, font, bw, bh, direction, sw):
            return font, sw, True
    size = cfg.min_font_size
    font = _load_font(cfg.font_path, size)
    sw = _stroke_for(color, size)[1]
    return font, sw, _fits(draw, text, font, bw, bh, direction, sw)


def render_page(image: Image.Image, regions: list[Region], cfg: Config) -> Image.Image:
    out = image.copy()
    draw = ImageDraw.Draw(out)
    for r in regions:
        text = (r.translated_text or "").strip()
        if not text:
            continue
        x, y, w, h = r.bbox
        pad = 3
        bx, by = x + pad, y + pad
        bw, bh = max(1, w - 2 * pad), max(1, h - 2 * pad)
        direction = r.direction if cfg.direction == "auto" else cfg.direction
        if direction not in (DIRECTION_VERTICAL, DIRECTION_HORIZONTAL):
            direction = DIRECTION_HORIZONTAL
        color = tuple(r.color) if r.color else (0, 0, 0)
        font, sw, fits = _pick_size(draw, text, bw, bh, direction, cfg, color)
        if not fits:
            logger.warning(
                "区域 bbox=%s 文字在最小字号 %s 下仍超出边框，可能显示不全",
                r.bbox, cfg.min_font_size,
            )
        stroke = (0, 0, 0)
        if direction == DIRECTION_VERTICAL:
            _draw_vertical(draw, text, font, bx, by, bw, bh, color, stroke, sw)
        else:
            _draw_horizontal(draw, text, font, bx, by, bw, bh, color, stroke, sw)
    return out


def _draw_horizontal(draw, text, font, bx, by, bw, bh, color, stroke, sw):
    eff_bw = max(1, bw - 2 * sw)
    measure = lambda s: draw.textlength(s, font=font)
    lines = balance_lines(text, measure, eff_bw)
    lines = apply_kinsoku(lines, measure, eff_bw)
    lh = _line_advance(font)
    cy = by + max(0.0, (bh - lh * len(lines)) / 2)
    cx = bx + bw / 2
    for line in lines:
        draw.text(
            (cx, cy + lh / 2), line,
            font=font, fill=color, stroke_width=sw, stroke_fill=stroke, anchor="mm",
        )
        cy += lh


def _draw_vertical(draw, text, font, bx, by, bw, bh, color, stroke, sw):
    chars = [c for c in text if c != "\n"]
    if not chars:
        return
    eff_bh = max(1, bh - 2 * sw)
    adv = font.size * 1.05
    per_col = max(1, int(eff_bh // adv))
    col_w = font.size * _COL_GAP
    n_cols = -(-len(chars) // per_col)
    x_right = bx + (bw + (n_cols - 1) * col_w) / 2  # 最右列中心 x
    for col in range(n_cols):
        col_chars = chars[col * per_col : (col + 1) * per_col]
        cy = by + max(0.0, (bh - adv * len(col_chars)) / 2)
        cx = x_right - col * col_w
        for ch in col_chars:
            draw.text(
                (cx, cy + adv / 2), ch,
                font=font, fill=color, stroke_width=sw, stroke_fill=stroke, anchor="mm",
            )
            cy += adv
