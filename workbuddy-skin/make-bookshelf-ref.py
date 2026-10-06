#!/usr/bin/env python3
"""把 14 张书封拼成一排书的参考图（宽幅，适合做 16:9 皮肤背景参考）"""
import os
from PIL import Image

SRC = "/Users/xiaocheng/Documents/文字/书封"
OUT = "/Users/xiaocheng/workspace/openworker/workbuddy-skin/bookshelf-ref.png"

# 按顺序取 14 个文件
files = [
    "0.jpg", "1.jpg", "2.jpg", "3.jpg", "4.jpg", "5.png", "6.jpg",
    "7.jpg", "8.jpg", "9.jpg", "10.jpg", "11.jpg", "12.png", "O1_副本.jpg",
]

TARGET_H = 900          # 每本书统一高度（含封面）
GAP = 18                # 书与书的间距
PAD = 60                # 四周留白
SIDE = 50               # 每本书的书脊侧（右侧），模拟立体书

imgs = []
for f in files:
    p = os.path.join(SRC, f)
    im = Image.open(p).convert("RGB")
    w, h = im.size
    # 统一缩放：高 = TARGET_H
    nw = max(1, round(w * TARGET_H / h))
    im = im.resize((nw, TARGET_H), Image.LANCZOS)
    imgs.append(im)

total_w = PAD * 2 + sum(im.width for im in imgs) + GAP * (len(imgs) - 1) + SIDE * len(imgs)
total_h = TARGET_H + PAD * 2

canvas = Image.new("RGB", (total_w, total_h), (245, 240, 230))  # 暖米色底

x = PAD
for im in imgs:
    # 贴封面（左侧为主面）
    canvas.paste(im, (x, PAD))
    # 书脊侧：右侧画一条深色渐变条，模拟书脊厚度
    spine_w = SIDE
    spine = Image.new("RGB", (spine_w, TARGET_H), (120, 100, 80))
    # 简单渐变
    for i in range(spine_w):
        t = i / spine_w
        c = tuple(int(120 + (60 - 120) * t) for _ in range(3))
        for y in range(TARGET_H):
            spine.putpixel((i, y), c)
    canvas.paste(spine, (x + im.width, PAD))
    x += im.width + SIDE + GAP

canvas.save(OUT)
print(f"saved: {OUT} size={canvas.size}")
