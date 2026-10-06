# -*- coding: utf-8 -*-
"""生成应用图标 webui/app.ico（和界面左上角的标志一致：蓝紫渐变的圆角方块 + 白色 P）

    python scripts/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "webui" / "app.ico"
SIZE = 256


def main() -> None:
    top, bottom = (0, 145, 255), (107, 104, 255)          # 强调色 → 偏紫
    grad = Image.new("RGB", (SIZE, SIZE))
    px = grad.load()
    for y in range(SIZE):
        for x in range(SIZE):
            t = (x + y) / (2 * SIZE - 2)
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))
    mask = Image.new("L", (SIZE * 4, SIZE * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, SIZE * 4 - 1, SIZE * 4 - 1], radius=SIZE * 4 * 0.24, fill=255)
    icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    icon.paste(grad, (0, 0), mask.resize((SIZE, SIZE), Image.LANCZOS))
    font = None
    for name in ("arialbd.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"):
        try:
            font = ImageFont.truetype(name, int(SIZE * 0.6))
            break
        except OSError:
            continue
    draw = ImageDraw.Draw(icon)
    box = draw.textbbox((0, 0), "P", font=font)
    draw.text(((SIZE - (box[2] - box[0])) / 2 - box[0] + SIZE * 0.02, (SIZE - (box[3] - box[1])) / 2 - box[1]), "P",
              font=font, fill=(255, 255, 255, 255))
    icon.save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("已生成", OUT)


if __name__ == "__main__":
    main()
