"""译文渲染：横排/竖排，字号自适应收缩防溢出，居中，自动描边。"""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from ..core.config import Config
from ..core.models import DIRECTION_HORIZONTAL, DIRECTION_VERTICAL, Region

_LINE_GAP = 1.3   # 横排行距倍数
_COL_GAP = 1.35   # 竖排列距倍数


_FONT_FALLBACKS = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf",
    "/System/Library/Fonts/PingFang.ttc",
)


def _load_font(path: str, size: int):
    """多级兜底：指定字体 → 常见系统 CJK 字体 → PIL 默认字体（绝不抛异常）。"""
    for p in (path, *_FONT_FALLBACKS):
        if not p:
            continue
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_horizontal(draw, text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if cur and draw.textlength(cur + ch, font=font) > max_w:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def _fits(draw, text: str, font, bw: int, bh: int, direction: str) -> bool:
    if direction == DIRECTION_VERTICAL:
        per_col = max(1, int(bh // (font.size * 1.05)))
        n_cols = -(-len(text.replace("\n", "")) // per_col)
        return n_cols * font.size * _COL_GAP <= bw
    return len(_wrap_horizontal(draw, text, font, bw)) * font.size * _LINE_GAP <= bh


def _pick_size(draw, text: str, bw: int, bh: int, direction: str, cfg: Config):
    hi = min(cfg.max_font_size, bh, bw if direction == DIRECTION_HORIZONTAL else bh)
    for size in range(hi, cfg.min_font_size - 1, -2):
        font = _load_font(cfg.font_path, size)
        if _fits(draw, text, font, bw, bh, direction):
            return font
    return _load_font(cfg.font_path, cfg.min_font_size)


def _stroke_for(color) -> tuple[tuple, int]:
    """浅色文字（气泡外白字）配黑描边；深色文字不描边（气泡内描边会糊）。"""
    if sum(color) >= 384:
        return (0, 0, 0), 1
    return (0, 0, 0), 0


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
        font = _pick_size(draw, text, bw, bh, direction, cfg)
        color = tuple(r.color) if r.color else (0, 0, 0)
        stroke, sw = _stroke_for(color)
        if direction == DIRECTION_VERTICAL:
            _draw_vertical(draw, text, font, bx, by, bw, bh, color, stroke, sw)
        else:
            _draw_horizontal(draw, text, font, bx, by, bw, bh, color, stroke, sw)
    return out


def _draw_horizontal(draw, text, font, bx, by, bw, bh, color, stroke, sw):
    lines = _wrap_horizontal(draw, text, font, bw)
    lh = font.size * _LINE_GAP
    cy = by + max(0.0, (bh - lh * len(lines)) / 2)
    for line in lines:
        lw = draw.textlength(line, font=font)
        draw.text(
            (bx + (bw - lw) / 2, cy), line,
            font=font, fill=color, stroke_width=sw, stroke_fill=stroke,
        )
        cy += lh


def _draw_vertical(draw, text, font, bx, by, bw, bh, color, stroke, sw):
    chars = [c for c in text if c != "\n"]
    if not chars:
        return
    adv = font.size * 1.05
    per_col = max(1, int(bh // adv))
    col_w = font.size * _COL_GAP
    n_cols = -(-len(chars) // per_col)
    x_right = bx + (bw + (n_cols - 1) * col_w) / 2  # 最右列中心 x
    for col in range(n_cols):
        col_chars = chars[col * per_col : (col + 1) * per_col]
        cy = by + max(0.0, (bh - adv * len(col_chars)) / 2)
        cx = x_right - col * col_w
        for ch in col_chars:
            draw.text(
                (cx - font.size / 2, cy), ch,
                font=font, fill=color, stroke_width=sw, stroke_fill=stroke,
            )
            cy += adv
