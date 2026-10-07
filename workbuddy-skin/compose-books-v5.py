#!/usr/bin/env python3
"""书上墙 v5.1：flood-fill 四角连通去白 + 立体书渲染"""
import os
from PIL import Image, ImageDraw, ImageFilter
import numpy as np

SCENE = "/Users/xiaocheng/workspace/openworker/workbuddy-skin/board-v1.png"
SRC = "/Users/xiaocheng/Documents/文字/书封"
OUT = "/Users/xiaocheng/workspace/openworker/workbuddy-skin/skin-bookshelf-v5.png"

scene = Image.open(SCENE).convert("RGB")
W, H = scene.size
canvas = scene.copy()
arr = np.array(canvas).astype(int)

boards = [
    ("l-top", 430, 20, 880, [("2.jpg", 280, 0), ("5.png", 280, 5)]),
    ("l-mid", 890, 20, 880, [("0.jpg", 310, 0), ("1.jpg", 310, 0), ("4.jpg", 310, -6)]),
    ("l-low", 1333, 20, 880, [("3.jpg", 230, 0), ("6.jpg", 230, 6)]),
    ("r-top", 430, 1820, 2700, [("7.jpg", 280, 0), ("11.jpg", 280, -5)]),
    ("r-mid", 890, 1820, 2700, [("9.jpg", 270, 6), ("10.jpg", 270, 0), ("12.png", 270, -6)]),
    ("r-low", 1333, 1820, 2700, [("8.jpg", 230, 0), ("O1_副本.jpg", 230, -6)]),
]

def sample_color(x, y):
    if 0 <= x < W and 0 <= y < H:
        return tuple(int(v) for v in arr[y, x])
    return None

# ===== 1. 抹掉 AI 假书 =====
for name, surf_y, x0, x1, books in boards:
    ex0 = max(0, x0 - 60)
    ex1 = min(W, x1 + 60)
    top_y = surf_y - 380
    wall_col = sample_color((ex0 + ex1) // 2, max(0, top_y - 6)) or (240, 232, 215)
    board_col = sample_color((ex0 + ex1) // 2, surf_y + 18) or (170, 140, 110)
    fill = Image.new("RGB", (ex1 - ex0, surf_y - top_y))
    fd = ImageDraw.Draw(fill)
    for yy in range(surf_y - top_y):
        t = yy / max(1, (surf_y - top_y))
        c = tuple(int(wall_col[i] + (board_col[i] - wall_col[i]) * t) for i in range(3))
        fd.line([(0, yy), (ex1 - ex0, yy)], fill=c)
    canvas.paste(fill, (ex0, top_y))

# ===== 2. flood-fill 去白边 =====
def trim_white_floodfill(im, thresh=240):
    """从四边 flood-fill 去除与边缘连通的近白区域，返回内容包围盒裁剪"""
    rgb = im.convert("RGB")
    w, h = rgb.size
    arr = np.array(rgb).astype(int)
    # 白色掩码（三通道都 > thresh）
    white = (arr[:, :, 0] > thresh) & (arr[:, :, 1] > thresh) & (arr[:, :, 2] > thresh)
    # 用 BFS 从四边扩散标记连通白色
    from collections import deque
    visited = np.zeros((h, w), dtype=bool)
    q = deque()
    for x in range(w):
        if white[0, x] and not visited[0, x]:
            visited[0, x] = True; q.append((0, x))
        if white[h-1, x] and not visited[h-1, x]:
            visited[h-1, x] = True; q.append((h-1, x))
    for y in range(h):
        if white[y, 0] and not visited[y, 0]:
            visited[y, 0] = True; q.append((y, 0))
        if white[y, w-1] and not visited[y, w-1]:
            visited[y, w-1] = True; q.append((y, w-1))
    while q:
        y, x = q.popleft()
        for dy, dx in ((1,0),(-1,0),(0,1),(0,-1)):
            ny, nx = y+dy, x+dx
            if 0 <= ny < h and 0 <= nx < w and white[ny, nx] and not visited[ny, nx]:
                visited[ny, nx] = True
                q.append((ny, nx))
    # 背景 = visited 白色区域；内容 = 非背景
    content = ~visited
    ys, xs = np.where(content)
    if len(xs) == 0:
        return rgb.copy()
    minx, maxx = xs.min(), xs.max()
    miny, maxy = ys.min(), ys.max()
    pad = 2
    box = (max(0, minx-pad), max(0, miny-pad), min(w, maxx+pad+1), min(h, maxy+pad+1))
    return rgb.crop(box)

def edge_absorb(im, n=3):
    """边缘颜色吸收：最外圈像素用相邻内部颜色替换，消除残余白框"""
    rgb = im.convert("RGB")
    arr = np.array(rgb).astype(int)
    new = arr.copy()
    for _ in range(n):
        new[0, :] = arr[1, :]
        new[-1, :] = arr[-2, :]
        new[:, 0] = arr[:, 1]
        new[:, -1] = arr[:, -2]
    return Image.fromarray(new.astype(np.uint8))

def load_book(f, target_h):
    im = Image.open(os.path.join(SRC, f))
    if im.mode == "RGBA":
        # 12.png: 透明边直接按 alpha 裁
        rgb = im.convert("RGBA")
        a = rgb.getchannel("A")
        aw, ah = a.size
        ap = np.array(a)
        rows = np.where(ap.max(axis=1) > 10)[0]
        cols = np.where(ap.max(axis=0) > 10)[0]
        if len(rows) and len(cols):
            rgb = rgb.crop((cols.min(), rows.min(), cols.max()+1, rows.max()+1))
        rgb = rgb.convert("RGB")
    else:
        rgb = trim_white_floodfill(im)
    rgb = edge_absorb(rgb)
    w, h = rgb.size
    bw = int(target_h * w / h)
    rgb = rgb.resize((bw, target_h), Image.LANCZOS)
    return rgb

# ===== 3. 立体书渲染 =====
def render_book(img, x, y_bottom, tilt):
    w, h = img.size
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    book = img.convert("RGBA")
    spine_w = max(8, int(w * 0.06))
    spine = Image.new("RGBA", (spine_w, h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(spine)
    base = book.getpixel((w - 1, h // 2))
    for sx in range(spine_w):
        t = sx / spine_w
        c = tuple(int(base[i] * (0.72 + 0.18 * (1 - t))) for i in range(3))
        sd.line([(sx, 0), (sx, h)], fill=(c[0], c[1], c[2], 255))
    hl = Image.new("RGBA", (max(2, spine_w // 3), h), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hl)
    for sx in range(max(2, spine_w // 3)):
        t = sx / max(2, spine_w // 3)
        a = int(70 * (1 - t))
        hd.line([(sx, 0), (sx, h)], fill=(255, 245, 230, a))
    spine.paste(hl, (0, 0), hl)

    page_h = max(4, int(h * 0.03))
    pages = Image.new("RGBA", (w, page_h), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pages)
    base_col = book.getpixel((w // 2, h - 1))
    for py in range(page_h):
        shade = tuple(max(0, min(255, int(base_col[i] * (0.92 + 0.03 * (py / page_h))))) for i in range(3))
        pd.line([(0, py), (w, py)], fill=(shade[0], shade[1], shade[2], 255))

    if tilt:
        rot_book = book.rotate(-tilt, expand=True, resample=Image.BICUBIC)
        rot_spine = spine.rotate(-tilt, expand=True, resample=Image.BICUBIC)
        rot_pages = pages.rotate(-tilt, expand=True, resample=Image.BICUBIC)
        combined = Image.new("RGBA", (rot_book.width + rot_spine.width, max(rot_book.height, rot_spine.height, rot_pages.height)), (0, 0, 0, 0))
        combined.paste(rot_book, (0, 0), rot_book)
        combined.paste(rot_spine, (rot_book.width, 0), rot_spine)
        combined.paste(rot_pages, (0, rot_book.height - rot_pages.height), rot_pages)
        layer.paste(combined, (x, y_bottom - combined.height))
    else:
        combined = Image.new("RGBA", (w + spine_w, h), (0, 0, 0, 0))
        combined.paste(book, (0, 0), book)
        combined.paste(spine, (w, 0), spine)
        combined.paste(pages, (0, h - page_h), pages)
        layer.paste(combined, (x, y_bottom - h))

    cw, ch = combined.size
    sh = Image.new("RGBA", (cw + 24, 22), (0, 0, 0, 0))
    sd2 = ImageDraw.Draw(sh)
    sd2.rectangle([4, 8, cw + 18, 18], fill=(0, 0, 0, 90))
    sh = sh.filter(ImageFilter.GaussianBlur(4))
    layer.paste(sh, (x - 8, y_bottom - 16), sh)

    glow = Image.new("RGBA", (cw, 6), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for gy in range(6):
        a = int(40 * (1 - gy / 6))
        gd.line([(0, gy), (cw, gy)], fill=(255, 250, 235, a))
    layer.paste(glow, (x, y_bottom - combined.height), glow)

    canvas.paste(layer, (0, 0), layer)

for name, surf_y, x0, x1, books in boards:
    cx = x0 + 26
    for f, hgt, tilt in books:
        im = load_book(f, hgt)
        render_book(im, cx, surf_y, tilt)
        cx += im.size[0] + 16

canvas = canvas.filter(ImageFilter.GaussianBlur(0.3))
canvas.save(OUT)
print("saved:", OUT)
