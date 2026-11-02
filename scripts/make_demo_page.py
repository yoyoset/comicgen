"""绘制一张原创的合成漫画页（4 格、网点、气泡含横排/竖排台词），用作 README 演示图。

角色与台词均为本仓库原创，没有任何第三方版权素材；画得粗糙，目的是让检测/OCR/抹字/嵌字
在"像漫画的版面"上跑一遍，而不是展示画技。需要 Windows 自带的 YuGothM 字体。

    python scripts/make_demo_page.py [输出路径]
"""
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = r"C:\Windows\Fonts\YuGothM.ttc"
S = 2  # 超采样倍数，最后缩回去抗锯齿
W, H = 900, 1300

INK = (20, 20, 20)
WHITE = (255, 255, 255)
TONE = (60, 60, 60)


def sc(v):
    return int(round(v * S))


def font(px):
    return ImageFont.truetype(FONT, sc(px))


def tone(img, box, spacing=7, radius=1.6, shape=None):
    """网点：在 box 内铺点阵；shape 为 PIL 'L' 蒙版时只在蒙版内铺。"""
    layer = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(layer)
    x0, y0, x1, y1 = [sc(v) for v in box]
    sp, r = sc(spacing), sc(radius)
    row = 0
    y = y0
    while y < y1:
        x = x0 + (sp // 2 if row % 2 else 0)
        while x < x1:
            d.ellipse((x - r, y - r, x + r, y + r), fill=255)
            x += sp
        y += sp // 2 + 1
        row += 1
    if shape is not None:
        layer = Image.composite(layer, Image.new("L", img.size, 0), shape)
    img.paste(Image.new("RGB", img.size, TONE), (0, 0), layer)


def panel(img, box):
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = [sc(v) for v in box]
    d.rectangle((x0, y0, x1, y1), fill=WHITE)
    return (x0, y0, x1, y1)


def frame(img, box):
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = [sc(v) for v in box]
    d.rectangle((x0, y0, x1, y1), outline=INK, width=sc(4))


def clip_to(img, box, painter):
    """在 box 内作画：先画到临时层，再只粘贴 box 范围，避免溢出到相邻格。"""
    layer = img.copy()
    painter(layer)
    x0, y0, x1, y1 = [sc(v) for v in box]
    img.paste(layer.crop((x0, y0, x1, y1)), (x0, y0))


def girl(img, cx, cy, k, mood="smile"):
    """原创角色"澪"：双马尾短发女孩。(cx,cy) 为脸中心，k 为缩放。"""
    d = ImageDraw.Draw(img)
    X = lambda v: sc(cx + v * k)
    Y = lambda v: sc(cy + v * k)
    # 双马尾
    for sgn in (-1, 1):
        d.ellipse((X(sgn * 95 - 28), Y(-10), X(sgn * 95 + 28), Y(170)), fill=INK)
    # 脖子与身体
    d.rectangle((X(-18), Y(75), X(18), Y(115)), fill=WHITE, outline=INK, width=sc(3))
    d.polygon([(X(-110), Y(260)), (X(-70), Y(110)), (X(70), Y(110)), (X(110), Y(260))], fill=(215, 215, 215), outline=INK)
    d.polygon([(X(-30), Y(110)), (X(0), Y(160)), (X(30), Y(110))], fill=WHITE, outline=INK)
    # 头发（后）与脸
    d.ellipse((X(-92), Y(-100), X(92), Y(90)), fill=INK)
    d.ellipse((X(-78), Y(-62), X(78), Y(88)), fill=WHITE, outline=INK, width=sc(3))
    # 刘海
    d.pieslice((X(-92), Y(-105), X(92), Y(60)), 180, 360, fill=INK)
    for bx in (-50, -15, 20, 55):
        d.polygon([(X(bx - 14), Y(-30)), (X(bx + 14), Y(-30)), (X(bx), Y(10))], fill=INK)
    # 眼睛
    for sgn in (-1, 1):
        ex = sgn * 34
        if mood == "shock":
            d.ellipse((X(ex - 15), Y(8), X(ex + 15), Y(52)), fill=WHITE, outline=INK, width=sc(3))
            d.ellipse((X(ex - 6), Y(20), X(ex + 6), Y(42)), fill=INK)
        else:
            d.ellipse((X(ex - 13), Y(14), X(ex + 13), Y(54)), fill=INK)
            d.ellipse((X(ex - 5), Y(20), X(ex + 3), Y(30)), fill=WHITE)
    # 嘴
    if mood == "shock":
        d.ellipse((X(-10), Y(62), X(10), Y(82)), fill=INK)
    else:
        d.arc((X(-20), Y(52), X(20), Y(80)), 20, 160, fill=INK, width=sc(3))
    # 腮红线
    for sgn in (-1, 1):
        for i in range(3):
            d.line((X(sgn * 52 + i * 5), Y(60), X(sgn * 52 + i * 5 + 6), Y(70)), fill=INK, width=sc(2))


def robot(img, cx, cy, k, look="front"):
    """原创角色"ピコ"：圆头小机器人。(cx,cy) 为头中心。"""
    d = ImageDraw.Draw(img)
    X = lambda v: sc(cx + v * k)
    Y = lambda v: sc(cy + v * k)
    d.line((X(0), Y(-92), X(0), Y(-130)), fill=INK, width=sc(4))
    d.ellipse((X(-10), Y(-146), X(10), Y(-126)), fill=INK)
    d.rounded_rectangle((X(-60), Y(88), X(60), Y(230)), sc(24 * k), fill=(200, 200, 200), outline=INK, width=sc(4))
    d.rectangle((X(-30), Y(150), X(30), Y(170)), fill=WHITE, outline=INK, width=sc(3))
    for sgn in (-1, 1):
        d.rounded_rectangle((X(sgn * 62 - 12), Y(110), X(sgn * 62 + 12), Y(200)), sc(10 * k), fill=(200, 200, 200), outline=INK, width=sc(3))
    d.ellipse((X(-85), Y(-90), X(85), Y(90)), fill=(235, 235, 235), outline=INK, width=sc(4))
    d.rounded_rectangle((X(-62), Y(-42), X(62), Y(48)), sc(30 * k), fill=INK)
    ox = 8 if look == "right" else (-8 if look == "left" else 0)
    for sgn in (-1, 1):
        d.ellipse((X(sgn * 26 - 14 + ox), Y(-14), X(sgn * 26 + 14 + ox), Y(20)), fill=WHITE)
    d.arc((X(-14 + ox), Y(18), X(14 + ox), Y(36)), 20, 160, fill=WHITE, width=sc(3))


def _wrap_ellipse(d, cx, cy, tw, th, tail):
    """画椭圆气泡；返回 (rx, ry)。tail=(tx,ty) 为尾巴指向的点。"""
    rx = tw / 2 * 1.38 + 14
    ry = th / 2 * 1.5 + 12
    X, Y = sc(cx), sc(cy)
    if tail:
        tx, ty = sc(tail[0]), sc(tail[1])
        ang = math.atan2(ty - Y, tx - X)
        a1, a2 = ang - 0.22, ang + 0.22
        base = [(X + sc(rx) * math.cos(a) * 0.92, Y + sc(ry) * math.sin(a) * 0.92) for a in (a1, a2)]
        d.polygon([base[0], (tx, ty), base[1]], fill=WHITE, outline=INK)
    d.ellipse((X - sc(rx), Y - sc(ry), X + sc(rx), Y + sc(ry)), fill=WHITE, outline=INK, width=sc(3))
    if tail:
        # 抹掉尾巴与椭圆相接处的轮廓线
        d.polygon([base[0], ((base[0][0] + base[1][0]) / 2 * 0.985 + X * 0.015, (base[0][1] + base[1][1]) / 2 * 0.985 + Y * 0.015), base[1]], fill=WHITE)
    return rx, ry


def bubble_h(img, cx, cy, lines, px=27, tail=None):
    d = ImageDraw.Draw(img)
    f = font(px)
    lh = px * 1.3
    tw = max(d.textlength(t, font=f) / S for t in lines)
    th = lh * len(lines)
    _wrap_ellipse(d, cx, cy, tw, th, tail)
    y = cy - th / 2
    for t in lines:
        w = d.textlength(t, font=f) / S
        d.text((sc(cx - w / 2), sc(y)), t, font=f, fill=INK)
        y += lh


def bubble_v(img, cx, cy, cols, px=27, tail=None):
    """竖排气泡：cols 从右到左，每列一个字符串；。、 放在字格右上。"""
    d = ImageDraw.Draw(img)
    f = font(px)
    step = px * 1.12
    col_w = px * 1.35
    th = max(len(c) for c in cols) * step
    tw = len(cols) * col_w
    rx = tw / 2 + 24
    ry = th / 2 + 22
    X, Y = sc(cx), sc(cy)
    if tail:
        tx, ty = sc(tail[0]), sc(tail[1])
        ang = math.atan2(ty - Y, tx - X)
        base = [(X + sc(rx) * math.cos(a) * 0.92, Y + sc(ry) * math.sin(a) * 0.92) for a in (ang - 0.25, ang + 0.25)]
        d.polygon([base[0], (tx, ty), base[1]], fill=WHITE, outline=INK)
    d.rounded_rectangle((X - sc(rx), Y - sc(ry), X + sc(rx), Y + sc(ry)), sc(34), fill=WHITE, outline=INK, width=sc(3))
    if tail:
        d.polygon([base[0], ((base[0][0] + base[1][0]) / 2 * 0.985 + X * 0.015, (base[0][1] + base[1][1]) / 2 * 0.985 + Y * 0.015), base[1]], fill=WHITE)
    x = cx + tw / 2 - col_w / 2
    for col in cols:
        y = cy - th / 2 + (th - len(col) * step) / 2
        for ch in col:
            w = d.textlength(ch, font=f) / S
            ox = (px * 0.55 if ch in "、。" else 0)
            oy = (-px * 0.45 if ch in "、。" else 0)
            d.text((sc(x - w / 2 + ox), sc(y + oy)), ch, font=f, fill=INK)
            y += step
        x -= col_w


def narration(img, x, y, lines, px=24):
    d = ImageDraw.Draw(img)
    f = font(px)
    tw = max(d.textlength(t, font=f) / S for t in lines)
    th = px * 1.3 * len(lines)
    d.rectangle((sc(x), sc(y), sc(x + tw + 28), sc(y + th + 20)), fill=WHITE, outline=INK, width=sc(3))
    yy = y + 10
    for t in lines:
        d.text((sc(x + 14), sc(yy)), t, font=f, fill=INK)
        yy += px * 1.3


def sky(img, box):
    """黄昏天空：从上到下渐疏的网点 + 几道云。"""
    x0, y0, x1, y1 = box
    shape = Image.new("L", img.size, 0)
    sd = ImageDraw.Draw(shape)
    h = y1 - y0
    for i in range(6):
        sd.rectangle((sc(x0), sc(y0 + h * i / 6), sc(x1), sc(y0 + h * (i + 1) / 6)), fill=255)
        tone(img, (x0, y0 + h * i / 6, x1, y0 + h * (i + 1) / 6), spacing=5 + i * 3, radius=2.0 - i * 0.22, shape=Image.composite(shape, Image.new("L", img.size, 0), shape))
        sd.rectangle((0, 0, img.size[0], img.size[1]), fill=0)


def make(out):
    img = Image.new("RGB", (sc(W), sc(H)), WHITE)

    # 第 1 格：横幅，屋顶黄昏
    p1 = (30, 30, 870, 430)
    panel(img, p1)

    def draw1(im):
        sky(im, (30, 30, 870, 300))
        d = ImageDraw.Draw(im)
        d.rectangle((sc(30), sc(330), sc(870), sc(430)), fill=(205, 205, 205))
        d.line((sc(30), sc(330), sc(870), sc(330)), fill=INK, width=sc(4))
        d.rectangle((sc(30), sc(215), sc(870), sc(330)), fill=WHITE)
        for i in range(8):
            bx = 50 + i * 105
            h = 40 + (i * 37) % 70
            d.rectangle((sc(bx), sc(330 - h), sc(bx + 70), sc(330)), fill=(150, 150, 150), outline=INK, width=sc(3))
        girl(im, 215, 235, 0.92)
        robot(im, 640, 250, 0.9, look="left")

    clip_to(img, p1, draw1)
    frame(img, p1)
    bubble_v(img, 102, 168, ["やっと完成した", "…！"], px=27)
    bubble_h(img, 745, 120, ["起動シーケンス、", "開始します。"], tail=(660, 160))

    # 第 2 格：机器人特写
    p2 = (30, 460, 400, 860)
    panel(img, p2)

    def draw2(im):
        tone(im, (30, 460, 400, 860), spacing=9, radius=1.6)
        robot(im, 215, 700, 1.7, look="right")

    clip_to(img, p2, draw2)
    frame(img, p2)
    bubble_h(img, 215, 520, ["あなたが、私の", "マスターですか？"], px=26, tail=(235, 600))

    # 第 3 格：少女惊讶
    p3 = (430, 460, 870, 860)
    panel(img, p3)

    def draw3(im):
        d = ImageDraw.Draw(im)
        cx, cy = 650, 665
        for i in range(36):  # 集中线
            a = 2 * math.pi * i / 36
            d.line((sc(cx + 120 * math.cos(a)), sc(cy + 120 * math.sin(a)), sc(cx + 520 * math.cos(a)), sc(cy + 520 * math.sin(a))), fill=INK, width=sc(2))
        d.ellipse((sc(cx - 120), sc(cy - 120), sc(cx + 120), sc(cy + 120)), fill=WHITE)
        girl(im, cx, cy + 20, 1.05, mood="shock")

    clip_to(img, p3, draw3)
    frame(img, p3)
    bubble_h(img, 722, 560, ["えっ、", "しゃべった！？"], px=25, tail=(700, 640))
    narration(img, 442, 472, ["その夜、小さな", "奇跡が起きた。"], px=22)

    # 第 4 格：二人
    p4 = (30, 890, 870, 1270)
    panel(img, p4)

    def draw4(im):
        d = ImageDraw.Draw(im)
        d.rectangle((sc(30), sc(1170), sc(870), sc(1270)), fill=(215, 215, 215))
        d.line((sc(30), sc(1170), sc(870), sc(1170)), fill=INK, width=sc(4))
        tone(im, (30, 890, 870, 1170), spacing=14, radius=1.3)
        girl(im, 250, 1060, 0.85)
        robot(im, 650, 1080, 0.8, look="left")

    clip_to(img, p4, draw4)
    frame(img, p4)
    bubble_h(img, 450, 950, ["よろしくね、ピコ。"], px=27, tail=(280, 1010))
    bubble_v(img, 810, 1010, ["まず、お茶を", "淹れましょうか？"], px=24, tail=(710, 1060))

    img = img.resize((W, H), Image.LANCZOS)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


if __name__ == "__main__":
    print(make(sys.argv[1] if len(sys.argv) > 1 else r"docs\demo\original.png"))
