#!/usr/bin/env python3
"""书上墙 v4：六块错落木板 + 14 本真实书封直贴，去掉 AI 假书与白边"""
import os
from PIL import Image, ImageDraw, ImageFilter
import numpy as np

SCENE = "/Users/xiaocheng/workspace/openworker/workbuddy-skin/board-v1.png"
SRC = "/Users/xiaocheng/workspace/openworker/workbuddy-skin/books-cropped"
OUT = "/Users/xiaocheng/workspace/openworker/workbuddy-skin/skin-bookshelf-v4.png"

scene = Image.open(SCENE).convert("RGB")
W, H = scene.size  # 2720 x 1520
canvas = scene.copy()
arr = np.array(canvas).astype(int)

# ===== 木板位置（表面 y = 书底 y）=====
# (名称, 表面y, x起点, x终点, 书籍列表)
boards = [
    # 左侧
    ("l-top", 430, 20, 880, [("2.jpg", 280, 0), ("5.png", 280, 5)]),
    ("l-mid", 890, 20, 880, [("0.jpg", 310, 0), ("1.jpg", 310, 0), ("4.jpg", 310, -6)]),
    ("l-low", 1333, 20, 880, [("3.jpg", 230, 0), ("6.jpg", 230, 6)]),
    # 右侧
    ("r-top", 430, 1820, 2700, [("7.jpg", 280, 0), ("11.jpg", 280, -5)]),
    ("r-mid", 890, 1820, 2700, [("9.jpg", 270, 6), ("10.jpg", 270, 0), ("12.png", 270, -6)]),
    ("r-low", 1333, 1820, 2700, [("8.jpg", 230, 0), ("O1_副本.jpg", 230, -6)]),
]

def sample_color(x, y):
    """采样某点颜色，越界返回 None"""
    if 0 <= x < W and 0 <= y < H:
        return tuple(int(v) for v in arr[y, x])
    return None

# ===== 1. 抹掉每块木板上的 AI 假书 =====
for name, surf_y, x0, x1, books in boards:
    ex0 = max(0, x0 - 60)
    ex1 = min(W, x1 + 60)
    top_y = surf_y - 380
    if top_y < 0:
        top_y = 0
    # 采样墙面色（书区域上方）
    wall_col = sample_color((ex0 + ex1) // 2, max(0, top_y - 6)) or (240, 232, 215)
    # 采样木板色（表面下方）
    board_col = sample_color((ex0 + ex1) // 2, surf_y + 18) or (170, 140, 110)
    # 渐变填充：上=墙面，下=木板
    fill = Image.new("RGB", (ex1 - ex0, surf_y - top_y))
    fd = ImageDraw.Draw(fill)
    for yy in range(surf_y - top_y):
        t = yy / max(1, (surf_y - top_y))
        c = tuple(int(wall_col[i] + (board_col[i] - wall_col[i]) * t) for i in range(3))
        fd.line([(0, yy), (ex1 - ex0, yy)], fill=c)
    canvas.paste(fill, (ex0, top_y))
    # 微木纹噪
    nd = ImageDraw.Draw(canvas)
    for yy in range(top_y, surf_y, 26):
        noise = np.random.randint(-5, 6)
        shade = tuple(max(0, min(255, board_col[i] + noise)) for i in range(3))
        nd.line([(ex0, yy), (ex1, yy)], fill=shade)

# ===== 2. 贴真实书封 =====
def load_book(f, target_h):
    im = Image.open(os.path.join(SRC, f)).convert("RGB")
    w, h = im.size
    bw = int(target_h * w / h)
    im = im.resize((bw, target_h), Image.LANCZOS)
    return im

def place_book(img, x, y_bottom, tilt):
    w, h = img.size
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    # 底部阴影
    sh = Image.new("RGBA", (w + 12, 18), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sh)
    sd.rectangle([2, 6, w + 9, 16], fill=(0, 0, 0, 75))
    sh = sh.filter(ImageFilter.GaussianBlur(3))
    layer.paste(sh, (x - 5, y_bottom - h - 14), sh)
    if tilt:
        im2 = img.convert("RGBA").rotate(-tilt, expand=True, resample=Image.BICUBIC)
        layer.paste(im2, (x, y_bottom - im2.size[1]))
    else:
        layer.paste(img.convert("RGBA"), (x, y_bottom - h))
    # 书脊侧
    spine = Image.new("RGBA", (10, h), (110, 85, 65, 255))
    sd2 = ImageDraw.Draw(spine)
    for sx in range(10):
        c = int(135 + (70 - 135) * sx / 10)
        sd2.line([(sx, 0), (sx, h)], fill=(c, c - 12, c - 25, 255))
    layer.paste(spine, (x + w, y_bottom - h))
    canvas.paste(layer, (0, 0), layer)

for name, surf_y, x0, x1, books in boards:
    cx = x0 + 24
    for f, hgt, tilt in books:
        im = load_book(f, hgt)
        place_book(im, cx, surf_y, tilt)
        cx += im.size[0] + 14

canvas = canvas.filter(ImageFilter.GaussianBlur(0.3))
canvas.save(OUT)
print("saved:", OUT)
