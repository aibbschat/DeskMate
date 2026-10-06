#!/usr/bin/env python3
"""把真实书封精确合成到 AI 场景图的书架区域（书名 100% 保真）"""
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

SCENE = "/Users/xiaocheng/workspace/openworker/workbuddy-skin/trial1-scene.png"
SRC = "/Users/xiaocheng/Documents/文字/书封"
OUT = "/Users/xiaocheng/workspace/openworker/workbuddy-skin/skin-bookshelf-v1.png"

files = [
    "0.jpg", "1.jpg", "2.jpg", "3.jpg", "4.jpg", "5.png", "6.jpg",
    "7.jpg", "8.jpg", "9.jpg", "10.jpg", "11.jpg", "12.png", "O1_副本.jpg",
]

scene = Image.open(SCENE).convert("RGB")
W, H = scene.size  # 2730 x 1536

# ===== 1. 确定书架区域（书所在位置）=====
# 从场景观察：书区域 x: 3%~97%，y: 65%~93%
book_x0, book_x1 = int(W * 0.025), int(W * 0.975)
book_y0, book_y1 = int(H * 0.645), int(H * 0.93)
shelf_w = book_x1 - book_x0
shelf_h = book_y1 - book_y0
print("book area:", (book_x0, book_y0, book_x1, book_y1), "size:", (shelf_w, shelf_h))

# ===== 2. 抹掉原书：用书架木色渐变填充 =====
# 取书区域上下边缘的颜色做垂直渐变，模拟书架木板
top_color = scene.crop((book_x0, max(0, book_y0 - 8), book_x1, max(0, book_y0 - 8) + 1)).resize((1, 1))
bottom_color = scene.crop((book_x0, min(H - 1, book_y1), book_x1, min(H - 1, book_y1) + 1)).resize((1, 1))
c_top = top_color.getpixel((0, 0))
c_bottom = bottom_color.getpixel((0, 0))

# 生成木色渐变底
canvas = scene.copy()
draw = ImageDraw.Draw(canvas)
for y in range(book_y0, book_y1):
    t = (y - book_y0) / max(1, (book_y1 - book_y0))
    c = tuple(int(c_top[i] + (c_bottom[i] - c_top[i]) * t) for i in range(3))
    draw.line([(book_x0, y), (book_x1, y)], fill=c)

# 增加书架木板纹理感：横向木纹线
for yy in range(book_y0, book_y1, 24):
    noise = np.random.randint(-6, 7)
    shade = tuple(max(0, min(255, c + noise)) for c in (200, 170, 130))
    draw.line([(book_x0, yy), (book_x1, yy)], fill=shade)

# ===== 3. 把 14 本书排成一排 =====
# 书高占书架高度的 72%，底部露出书柜底座，顶部留书架板
book_h = int(shelf_h * 0.72)
gap = 6
spine = 14  # 书脊厚度

# 计算每本书宽度（按原比例）
ratios = []
for f in files:
    p = os.path.join(SRC, f)
    im = Image.open(p).convert("RGB")
    w, h = im.size
    ratios.append(w / h)

total_ratio = sum(ratios)
# 可用宽度 = shelf_w - spine*14 - gap*13
avail_w = shelf_w - spine * len(files) - gap * (len(files) - 1)
base_h = avail_w / total_ratio
if base_h > book_h:
    base_h = book_h
    avail_w = base_h * total_ratio

# 重算起点使整体居中；书底离书柜底留出底座高度（约 12%），避免被底部遮挡
shelf_bottom = book_y1 - int(shelf_h * 0.12)
total_books_w = avail_w + spine * len(files) + gap * (len(files) - 1)
x_start = book_x0 + (shelf_w - total_books_w) // 2
y_top = shelf_bottom - int(base_h)

x = x_start
for i, f in enumerate(files):
    p = os.path.join(SRC, f)
    im = Image.open(p).convert("RGB")
    w, h = im.size
    bw = int(base_h * w / h)
    im = im.resize((bw, int(base_h)), Image.LANCZOS)

    # 轻微透视/倾斜感：直接垂直贴（皮肤背景视觉上足够）
    # 底部阴影
    shadow = Image.new("RGBA", (bw + spine + 6, 12), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rectangle([0, 4, bw + spine + 5, 11], fill=(0, 0, 0, 70))
    shadow = shadow.filter(ImageFilter.GaussianBlur(3))
    canvas.paste(shadow, (x - 3, y_top + int(base_h) - 8), shadow)

    # 贴封面
    canvas.paste(im, (x, y_top))

    # 书脊侧（右侧深色条 + 高光）
    spine_img = Image.new("RGB", (spine, int(base_h)), (90, 70, 55))
    sd2 = ImageDraw.Draw(spine_img)
    for sx in range(spine):
        t = sx / spine
        c = tuple(int(120 + (60 - 120) * t) for _ in range(3))
        sd2.line([(sx, 0), (sx, int(base_h))], fill=c)
    canvas.paste(spine_img, (x + bw, y_top))

    x += bw + spine + gap

# ===== 4. 整体柔光融合（轻微），让书融入场景 =====
canvas = canvas.filter(ImageFilter.GaussianBlur(0.4))

canvas.save(OUT)
print("saved:", OUT, canvas.size)
