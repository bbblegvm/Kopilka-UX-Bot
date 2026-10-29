"""Делает ВРЕМЕННЫЕ картинки настроения маскота: берёт каждую картинку из assets/ и пишет поверх «happy» / «sad».

Запуск из папки проекта:  python tools/make_mood_placeholders.py
Когда появятся настоящие рисунки, просто положите их в assets/ под теми же именами:
  mascot_happy.png, mascot_sad.png, mascot_hat_happy.png, mascot_hat_sad.png и т. д.
Имя = <файл наряда без .png>_<happy|sad>.png. Если нужного файла нет, бот покажет обычную картинку.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ASSETS = Path(__file__).resolve().parent.parent / "assets"
BASES = ["mascot", "mascot_bow", "mascot_hat", "mascot_glasses"]
MOODS = {"happy": (34, 150, 70), "sad": (60, 100, 190)}  # цвет подписи
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def font(size: int):
    for path in (FONT, "DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


for base in BASES:
    src = ASSETS / f"{base}.png"
    if not src.exists():
        continue
    for mood, color in MOODS.items():
        im = Image.open(src).convert("RGB")
        d = ImageDraw.Draw(im)
        f = font(max(40, im.width // 9))
        w, h = im.width, im.height
        bbox = d.textbbox((0, 0), mood, font=f)
        x, y = w - (bbox[2] - bbox[0]) - 16, h - (bbox[3] - bbox[1]) - 22
        d.text((x, y), mood, font=f, fill=color, stroke_width=3, stroke_fill=(255, 255, 255))
        out = ASSETS / f"{base}_{mood}.png"
        im.save(out)
        print("ok", out.name)
