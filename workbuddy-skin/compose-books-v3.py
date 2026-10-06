#!/usr/bin/env python3
"""书上墙·错落格子版：14 本书放进左右两侧的墙上书架格，中间留空"""
import os
from PIL import Image, ImageDraw, ImageFilter

SCENE = "/Users/xiaocheng/workspace/openworker/workbuddy-skin/wall-shelf-empty.png"
SRC = "/Users/xiaocheng/Documents/文字/书封"
OUT = "/Users/xiaocheng/workspace/openworker/workbuddy-skin/skin-bookshelf-v3.png"

scene = Image.open(SCENE).convert("RGB")
W, H = scene.size  # 2730 x 1536
canvas = scene.copy()

def load_book(f, target_h):
    im = Image.open(os.path.join(SRC, f)).convert("RGB")
    w, h = im.size
    bw = int(target_h * w / h)
    im = im.resize((bw, target_h), Image.LANCZOS)
    return im

def place_standing(img, x, y_bottom, tilt=0):
    """立放一本书，底部对齐 y_bottom，可倾斜"""
    w, h = img.size
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    # 底部阴影
    sh = Image.new("RGBA", (w + 10, 16), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sh)
    sd.rectangle([2, 6, w + 8, 15], fill=(0, 0, 0, 70))
    sh = sh.filter(ImageFilter.GaussianBlur(3))
    layer.paste(sh, (x - 4, y_bottom - h - 12), sh)
    if tilt:
        # 旋转封面（逆时针正角）
        im2 = img.convert("RGBA").rotate(-tilt, expand=True, resample=Image.BICUBIC)
        # 底部对齐
        layer.paste(im2, (x, y_bottom - im2.size[1]))
    else:
        layer.paste(img.convert("RGBA"), (x, y_bottom - h))
    # 书脊侧（右侧深色条）
    spine = Image.new("RGBA", (10, h), (110, 85, 65, 255))
    sd2 = ImageDraw.Draw(spine)
    for sx in range(10):
        c = int(130 + (70 - 130) * sx / 10)
        sd2.line([(sx, 0), (sx, h)], fill=(c, c - 12, c - 25, 255))
    layer.paste(spine, (x + w, y_bottom - h))
    canvas.paste(layer, (0, 0), layer)

def place_lying(img, x, y_bottom, tilt=0):
    """横放一本书（旋转90度后平躺），用于下层格"""
    w, h = img.size
    im = img.rotate(90, expand=True, resample=Image.BICUBIC)  # 高宽互换
    lw, lh = im.size
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sh = Image.new("RGBA", (lw + 12, 16), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sh)
    sd.rectangle([2, 6, lw + 10, 15], fill=(0, 0, 0, 70))
    sh = sh.filter(ImageFilter.GaussianBlur(3))
    layer.paste(sh, (x - 5, y_bottom - lh - 12), sh)
    layer.paste(im.convert("RGBA"), (x, y_bottom - lh))
    canvas.paste(layer, (0, 0), layer)

# ============ 左侧（3 格） ============
# 上格（小）：2 本立书，高 260，底 y≈595
lx = 90
for f, tilt in [("2.jpg", 0), ("5.png", 4)]:
    place_standing(load_book(f, 260), lx, 585, tilt)
    lx += 210

# 中格（大）：3 本立书，高 300，中间一本微斜
lx = 80
for f, tilt in [("0.jpg", 0), ("1.jpg", 0), ("4.jpg", 6)]:
    im = load_book(f, 300)
    place_standing(im, lx, 1335, tilt)
    lx += im.size[0] + 12

# 下格：2 本横放（平躺）在底层，底 y≈1536
lx = 120
for f in ["3.jpg", "6.jpg"]:
    im = load_book(f, 170)
    place_lying(im, lx, 1510, 0)
    lx += im.size[0] + 20

# ============ 右侧（3 格） ============
# 上格：2 本立书，高 260，底 y≈530，x 1850-2400
rx = 1860
for f, tilt in [("7.jpg", 0), ("11.jpg", -4)]:
    place_standing(load_book(f, 260), rx, 520, tilt)
    rx += 215

# 中格：3 本立书，高 300
rx = 1860
for f, tilt in [("9.jpg", 6), ("10.jpg", 0), ("12.png", -6)]:
    im = load_book(f, 300)
    place_standing(im, rx, 1040, tilt)
    rx += im.size[0] + 12

# 下格：2 本横放
rx = 1870
for f in ["8.jpg", "O1_副本.jpg"]:
    im = load_book(f, 170)
    place_lying(im, rx, 1460, 0)
    rx += im.size[0] + 20

canvas = canvas.filter(ImageFilter.GaussianBlur(0.3))
canvas.save(OUT)
print("saved:", OUT)
