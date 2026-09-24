# -*- coding: utf-8 -*-
"""make_icon.py —— 生成应用图标 app.ico（多尺寸）"""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
SIZE = 512

# 背景渐变（深蓝）
C_TOP = (59, 130, 246)      # #3b82f6
C_BOT = (29, 78, 216)       # #1d4ed8
C_GOLD = (250, 204, 21)
C_WHITE = (255, 255, 255)


def rounded_gradient(size, radius):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        grad.putpixel((0, y), tuple(int(C_TOP[i] + (C_BOT[i] - C_TOP[i]) * t)
                                    for i in range(3)))
    grad = grad.resize((size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1],
                                           radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)
    return img


def find_font(px):
    for p in (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc",
              r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\arialbd.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, px)
            except Exception:
                continue
    return ImageFont.load_default()


def build(size=512):
    S = size
    img = rounded_gradient(S, int(S * 0.22))
    d = ImageDraw.Draw(img)

    # ---- 放大镜 ----
    cx, cy, r = int(S * 0.44), int(S * 0.42), int(S * 0.26)
    ring = max(3, int(S * 0.045))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=C_WHITE, width=ring)
    # 手柄
    hx0, hy0 = cx + int(r * 0.72), cy + int(r * 0.72)
    hx1, hy1 = cx + int(r * 1.62), cy + int(r * 1.62)
    d.line([hx0, hy0, hx1, hy1], fill=C_WHITE, width=int(ring * 1.5))
    d.ellipse([hx1 - ring * 0.8, hy1 - ring * 0.8, hx1 + ring * 0.8, hy1 + ring * 0.8],
              fill=C_WHITE)

    # ---- 镜片里的「查」 ----
    fs = int(r * 1.25)
    f = find_font(fs)
    txt = "查"
    bb = d.textbbox((0, 0), txt, font=f)
    d.text((cx - (bb[2] - bb[0]) / 2 - bb[0], cy - (bb[3] - bb[1]) / 2 - bb[1]),
           txt, font=f, fill=C_GOLD)

    # ---- 右下角等级徽章 ----
    bw, bh = int(S * 0.30), int(S * 0.17)
    bx, by = S - bw - int(S * 0.06), S - bh - int(S * 0.06)
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=int(bh * 0.35),
                        fill=(22, 163, 74))
    fs2 = int(bh * 0.72)
    f2 = find_font(fs2)
    t2 = "100"
    bb2 = d.textbbox((0, 0), t2, font=f2)
    d.text((bx + (bw - (bb2[2] - bb2[0])) / 2 - bb2[0],
            by + (bh - (bb2[3] - bb2[1])) / 2 - bb2[1]), t2, font=f2, fill=C_WHITE)
    return img


sizes = [256, 128, 64, 48, 32, 24, 16]
big = build(512)
big.save(os.path.join(HERE, "app_512.png"))
ico = os.path.join(HERE, "app.ico")
big.save(ico, format="ICO", sizes=[(s, s) for s in sizes])
print(f"已生成 {ico}  ({os.path.getsize(ico)} 字节)")
print("尺寸:", sizes)

# 预览用：拼一张对比图
prev = Image.new("RGBA", (sum(sizes) + 8 * len(sizes), 256), (20, 23, 29, 255))
x = 4
for s in sizes:
    im = big.resize((s, s), Image.LANCZOS)
    prev.paste(im, (x, (256 - s) // 2), im)
    x += s + 8
prev.save(os.path.join(HERE, "app_preview.png"))
print("预览图: app_preview.png")
