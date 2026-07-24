"""生成一张合成英文漫画页，用于测试非日文 OCR（EasyOCR）路径。"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

EN_FONT = r"C:\Windows\Fonts\arial.ttf"


def make_sample_en(out: str = r"tests\sample_en\page_001.png") -> str:
    img = Image.new("RGB", (800, 1100), (205, 205, 215))
    d = ImageDraw.Draw(img)
    d.ellipse((60, 80, 440, 280), fill="white", outline="black", width=3)
    f1 = ImageFont.truetype(EN_FONT, 36)
    d.text((110, 150), "What is this?\nDoes it really work?", font=f1, fill="black")
    d.rounded_rectangle((520, 60, 750, 460), 30, fill="white", outline="black", width=3)
    f2 = ImageFont.truetype(EN_FONT, 34)
    d.text((545, 120), "To be, or\nnot to be.", font=f2, fill="black")
    d.rectangle((80, 880, 720, 980), fill=(30, 30, 40))
    d.text((105, 910), "Once upon a time, in a faraway land.", font=f2, fill="white")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


if __name__ == "__main__":
    print(make_sample_en())