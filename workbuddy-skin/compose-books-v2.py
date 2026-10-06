#!/usr/bin/env python3
"""把 14 本书分成左右两组，中间留空给输入框；书名 100% 保真"""
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

SCENE = "/Users/xiaocheng/workspace/openworker/workbuddy-skin/trial1-scene.png"
SRC = "/Users/xiaocheng/Documents/文字/书封"
OUT = "/Users/xiaocheng/workspace/openworker/workbuddy-skin/skin-bookshelf-v2.png"

files = [
    "0.jpg", "1.jpg", "2.jpg", "3.jpg", "4.jpg", "5.png", "6.jpg",
    "7.jpg", "8.jpg", "9.jpg", "10.jpg", "11.jpg", "12.png", "O1_副本.jpg",
]
left_files = files[:7]    # 左侧 7 本
right_files = files[7:]   # 右侧 7 本

scene = Image.open(SCENE).convert("RGB")
W, H = scene.size  # 2730 x 1536

# ===== 1. 书架区域 =====
book_x0, book_x1 = int(W * 0.025), int(W * 0.975)
book_y0, book_y1 = int(H * 0.645), int(H * 0.93)
shelf_w = book_x1 - book_x0
shelf_h = book_y1 - book_y0

# ===== 2. 抹掉原书（木色渐变填充整个书架区域）=====
top_color = scene.crop((book_x0, max(0, book_y0 - 8), book_x1, max(0, book_y0 - 8) + 1)).resize((1, 1))
bottom_color = scene.crop((book_x0, min(H - 1, book_y1), book_x1, min(H - 1, book_y1) + 1)).resize((1, 1))
c_top = top_color.getpixel((0, 0))
c_bottom = bottom_color.getpixel((0, 0))

canvas = scene.copy()
draw = ImageDraw.Draw(canvas)
for y in range(book_y0, book_y1):
    t = (y - book_y0) / max(1, (book_y1 - book_y0))
    c = tuple(int(c_top[i] + (c_bottom[i] - c_top[i]) * t) for i in range(3))
    draw.line([(book_x0, y), (book_x1, y)], fill=c)
for yy in range(book_y0, book_y1, 24):
    noise = np.random.randint(-6, 7)
    shade = tuple(max(0, min(255, c + noise)) for c in (200, 170, 130))
    draw.line([(book_x0, yy), (book_x1, yy)], fill=shade)

# ===== 3. 布局参数 =====
# 书底对齐书架底，底部留 12% 给书柜底座
shelf_bottom = book_y1 - int(shelf_h * 0.12)
gap = 6
spine = 12

def book_ratio(f):
    im = Image.open(os.path.join(SRC, f)).convert("RGB")
    w, h = im.size
    return w / h

def place_group(files_group, x_start, x_end, y_bottom):
    """把一组书从左到右排进 [x_start, x_end]，底对齐 y_bottom；返回实际右边界"""
    ratios = [book_ratio(f) for f in files_group]
    total_ratio = sum(ratios)
    max_w = x_end - x_start - spine * len(files_group) - gap * (len(files_group) - 1)
    base_h = min(max_w / total_ratio, 320)  # 限制最大高度，保持美观
    avail_w = base_h * total_ratio
    x = x_start
    for i, f in enumerate(files_group):
        p = os.path.join(SRC, f)
        im = Image.open(p).convert("RGB")
        w, h = im.size
        bw = int(base_h * w / h)
        im = im.resize((bw, int(base_h)), Image.LANCZOS)

        # 底部阴影
        shadow = Image.new("RGBA", (bw + spine + 6, 14), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        sd.rectangle([0, 4, bw + spine + 5, 13], fill=(0, 0, 0, 80))
        shadow = shadow.filter(ImageFilter.GaussianBlur(3))
        canvas.paste(shadow, (x - 3, y_bottom - int(base_h) - 9), shadow)

        # 贴封面
        canvas.paste(im, (x, y_bottom - int(base_h)))

        # 书脊侧（右侧深色条 + 高光）
        spine_img = Image.new("RGB", (spine, int(base_h)), (90, 70, 55))
        sd2 = ImageDraw.Draw(spine_img)
        for sx in range(spine):
            t = sx / spine
            c = tuple(int(120 + (60 - 120) * t) for _ in range(3))
            sd2.line([(sx, 0), (sx, int(base_h))], fill=c)
        canvas.paste(spine_img, (x + bw, y_bottom - int(base_h)))

        x += bw + spine + gap
    return x

# ===== 4. 左侧组：靠左，7 本 =====
left_end = place_group(left_files, book_x0 + 20, book_x0 + shelf_w * 0.36, shelf_bottom)
# ===== 5. 右侧组：靠右，7 本（从右往左反向排，保证最右贴近书架右缘）=====
# 先算右组需要多宽
r_ratios = [book_ratio(f) for f in right_files]
r_total = sum(r_ratios)
r_max_w = book_x1 - (book_x0 + shelf_w * 0.64) - spine * 7 - gap * 6
r_base_h = min(r_max_w / r_total, 320)
r_avail = r_base_h * r_total
r_x_start = book_x1 - r_avail - spine * 7 - gap * 6
# 反向排：把文件倒序，从右往左贴
x = book_x1
for f in reversed(right_files):
    p = os.path.join(SRC, f)
    im = Image.open(p).convert("RGB")
    w, h = im.size
    bw = int(r_base_h * w / h)
    im = im.resize((bw, int(r_base_h)), Image.LANCZOS)
    x -= bw + spine
    shadow = Image.new("RGBA", (bw + spine + 6, 14), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rectangle([0, 4, bw + spine + 5, 13], fill=(0, 0, 0, 80))
    shadow = shadow.filter(ImageFilter.GaussianBlur(3))
    canvas.paste(shadow, (x - 3, shelf_bottom - int(r_base_h) - 9), shadow)
    canvas.paste(im, (x, shelf_bottom - int(r_base_h)))
    spine_img = Image.new("RGB", (spine, int(r_base_h)), (90, 70, 55))
    sd2 = ImageDraw.Draw(spine_img)
    for sx in range(spine):
        t = sx / spine
        c = tuple(int(120 + (60 - 120) * t) for _ in range(3))
        sd2.line([(sx, 0), (sx, int(r_base_h))], fill=c)
    canvas.paste(spine_img, (x + bw, shelf_bottom - int(r_base_h)))
    x -= gap

# ===== 6. 整体柔光 =====
canvas = canvas.filter(ImageFilter.GaussianBlur(0.4))

canvas.save(OUT)
print("saved:", OUT, canvas.size)
print("left_end:", left_end, "right_start:", x + gap + spine)
