# -*- coding: utf-8 -*-
"""pick_text_linux.py —— 纯 Python 点选验证码求解器（Linux 可用）

思路（不需要"认字"，只做模板匹配）：
  1. ddddocr 检测出所有字符框
  2. 底部一条（y>=295）是**提示行** —— 要按顺序点的字
  3. 其余是**散布字符**
  4. 对提示行每个字（从左到右），在散布字符里找最像的那个
  5. 按提示顺序输出散布字符的中心坐标

为什么能这么干：提示行和图上的字是**同一套字形**，像素比对比 OCR 认字更稳。
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np
from PIL import Image

PROMPT_Y = 295          # 提示行的起始 y（实测底部 40px 是实心条）
NORM = (48, 48)         # 归一化尺寸


def _glyph(img_gray, box, verbose=False):
    """裁出字形并归一化。

    ★ 关键：散布字符的背景是**花纹理**，粗暴二值化会把信号毁掉
      （实测相似度全在 0 附近，等于没匹配上）。
      改用「减去大核模糊」做背景估计，笔画就被凸显出来了。
    """
    from PIL import ImageFilter
    x1, y1, x2, y2 = [int(v) for v in box]
    pad = 2
    x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
    x2, y2 = min(img_gray.width, x2 + pad), min(img_gray.height, y2 + pad)
    crop = img_gray.crop((x1, y1, x2, y2))

    # 背景 = 大核高斯模糊（半径取框尺寸的 1/4 左右）
    r = max(3.0, min(crop.size) / 4.0)
    bg = crop.filter(ImageFilter.GaussianBlur(radius=r))

    a = np.asarray(crop).astype(float)
    b = np.asarray(bg).astype(float)
    d = a - b                     # 笔画处明显偏离局部背景
    m = np.abs(d)
    if m.max() < 1e-6:
        return np.zeros(NORM, dtype=float)
    m = m / m.max()

    # 裁到笔画外接框（去掉留白，否则位置偏移会主导相似度）
    ys, xs = np.where(m > 0.35)
    if len(ys) > 8:
        m = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    if m.shape[0] < 3 or m.shape[1] < 3:
        return np.zeros(NORM, dtype=float)

    # 归一化到统一尺寸；保持长宽比以免字形被拉伸变形
    h, w = m.shape
    s = max(h, w)
    canvas = np.zeros((s, s), dtype=float)
    canvas[(s - h) // 2:(s - h) // 2 + h, (s - w) // 2:(s - w) // 2 + w] = m
    im = Image.fromarray((canvas * 255).astype(np.uint8))
    return np.asarray(im.resize(NORM, Image.LANCZOS)).astype(float) / 255.0


def _score(a, b):
    """相似度：归一化互相关（对亮度/尺寸差异不敏感）"""
    a = a - a.mean()
    b = b - b.mean()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-6 or nb < 1e-6:
        return -1.0
    return float((a * b).sum() / (na * nb))


def solve(img_bytes, det, verbose=False):
    """返回 'x,y|x,y|...' 形式的坐标串（与 Windows 引擎同格式）"""
    img = Image.open(io.BytesIO(img_bytes)).convert("L")
    boxes = det.detection(img_bytes)
    if not boxes:
        return "-1", []

    prompt, scattered = [], []
    for b in boxes:
        x1, y1, x2, y2 = b
        cy = (y1 + y2) / 2
        rec = {"box": (x1, y1, x2, y2), "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2,
               "w": x2 - x1, "h": y2 - y1}
        (prompt if cy >= PROMPT_Y else scattered).append(rec)

    if verbose:
        print(f"    提示行 {len(prompt)} 个字，散布 {len(scattered)} 个字")
        print(f"      提示行: " + " ".join(
            f"({r['cx']},{r['cy']})w{r['w']}" for r in
            sorted(prompt, key=lambda r: r["cx"])))
        print(f"      散布:   " + " ".join(
            f"({r['cx']},{r['cy']})w{r['w']}" for r in scattered))

    if not prompt or not scattered:
        return "-1", []

    prompt.sort(key=lambda r: r["cx"])          # 提示行从左到右 = 点击顺序
    pg = [_glyph(img, r["box"]) for r in prompt]
    sg = [_glyph(img, r["box"]) for r in scattered]

    used, out = set(), []
    for i, p in enumerate(pg):
        best, bi = -2.0, -1
        for j, s in enumerate(sg):
            if j in used:
                continue
            sc = _score(p, s)
            if sc > best:
                best, bi = sc, j
        if bi < 0:
            break
        used.add(bi)
        out.append((scattered[bi]["cx"], scattered[bi]["cy"]))
        if verbose:
            print(f"    提示#{i+1} -> 散布 ({scattered[bi]['cx']},{scattered[bi]['cy']})"
                  f"  相似度 {best:.3f}")

    return "|".join(f"{x},{y}" for x, y in out) + ("|" if out else ""), out


if __name__ == "__main__":
    import glob
    import time

    import ddddocr

    TRUTH = {
        "cap_00.jpg": "113,108|249,46|240,119|48,252|187,38|",
        "cap_01.jpg": "44,192|254,259|",
        "cap_02.jpg": "117,107|114,258|54,114|",
        "cap_03.jpg": "267,104|263,47|254,258|",
        "cap_04.jpg": "49,7|46,264|118,35|",
    }
    det = ddddocr.DdddOcr(det=True, show_ad=False)
    ok = 0
    for f in sorted(glob.glob("samples/*.jpg")):
        name = os.path.basename(f)
        data = open(f, "rb").read()
        t0 = time.time()
        got, pts = solve(data, det, verbose=True)
        dt = time.time() - t0
        truth = TRUTH[name]
        tpts = [tuple(int(v) for v in p.split(","))
                for p in truth.strip("|").split("|")]
        # 判定：点数和每个点都在 12 像素内算对
        good = len(pts) == len(tpts) and all(
            any(abs(px - tx) <= 12 and abs(py - ty) <= 12 for tx, ty in tpts)
            for px, py in pts)
        ok += good
        print(f"  {name}")
        print(f"    得到: {got}")
        print(f"    标准: {truth}")
        print(f"    {'✅ 对' if good else '❌ 不对'}   ({dt:.2f}s)")
        print()
    print("=" * 70)
    print(f"  结果: {ok}/{len(TRUTH)} 正确")
