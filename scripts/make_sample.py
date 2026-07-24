"""生成一张合成漫画页，用于流水线自测（无需真实漫画素材）。"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

JP_FONT = r"C:\Windows\Fonts\msgothic.ttc"


def make_sample(out: str = r"tests\sample\page_001.png") -> str:
    img = Image.new("RGB", (800, 1200), (205, 205, 215))
    d = ImageDraw.Draw(img)

    # 气泡 1：横排台词
    d.ellipse((60, 80, 440, 330), fill="white", outline="black", width=3)
    f1 = ImageFont.truetype(JP_FONT, 34)
    d.text((120, 150), "これはテストです。\n本当に動くの？", font=f1, fill="black")

    # 气泡 2：竖排台词
    d.rounded_rectangle((540, 60, 750, 500), 40, fill="white", outline="black", width=3)
    f2 = ImageFont.truetype(JP_FONT, 32)
    for i, ch in enumerate("縦書きのセリフです"):
        d.text((610, 100 + i * 40), ch, font=f2, fill="black")

    # 气泡外旁白（白字黑底）
    d.rectangle((80, 950, 720, 1060), fill=(30, 30, 40))
    d.text((105, 980), "その時、少年はまだ何も知らなかった。", font=f2, fill="white")

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


if __name__ == "__main__":
    print(make_sample())
