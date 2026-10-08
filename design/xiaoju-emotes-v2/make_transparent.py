#!/usr/bin/env python3
"""Flood-fill from four corners to remove white background, save as transparent webp."""
import os, sys
from collections import deque
from PIL import Image, ImageFilter

SRC_DIR = "/Users/xiaocheng/workspace/openworker/design/xiaoju-emotes-v2"
DST_DIR = "/Users/xiaocheng/workspace/openworker/surfaces/gui/src/assets/emotes"
os.makedirs(DST_DIR, exist_ok=True)

# ordered mapping: source file -> output webp name
FILES = [
    ("01-wave.png",          "wave.webp"),
    ("emote-05-cheer.png",   "cheer.webp"),
    ("emote-06-shy.png",     "shy.webp"),
    ("emote-07-v.png",       "v.webp"),
    ("emote-08-sleepy.png",  "sleepy.webp"),
    ("emote-09-tea.png",     "tea.webp"),
    ("emote-10-hug.png",     "hug.webp"),
    ("emote-11-facepalm.png","facepalm.webp"),
    ("emote-12-peek.png",    "peek.webp"),
    ("emote-13-clap.png",    "clap.webp"),
    ("emote-14-lol.png",     "lol.webp"),
    ("emote-15-think.png",   "think.webp"),
    ("emote-16-cry.png",     "cry.webp"),
    ("14-shock.png",         "shock.webp"),
    ("15-heart.png",         "heart.webp"),
    ("16-rage.png",          "rage.webp"),
]

def remove_white_bg(img, thresh=242):
    """Flood-fill from all border pixels, remove near-white connected region."""
    img = img.convert("RGBA")
    w, h = img.size
    px = img.load()
    visited = [[False]*w for _ in range(h)]
    q = deque()
    # seed from all border pixels that are near-white
    for x in range(w):
        for y in (0, h-1):
            r,g,b,a = px[x,y]
            if r>=thresh and g>=thresh and b>=thresh:
                q.append((x,y)); visited[y][x]=True
    for y in range(h):
        for x in (0, w-1):
            r,g,b,a = px[x,y]
            if r>=thresh and g>=thresh and b>=thresh:
                if not visited[y][x]:
                    q.append((x,y)); visited[y][x]=True
    while q:
        x,y = q.popleft()
        px[x,y] = (255,255,255,0)
        for dx,dy in ((1,0),(-1,0),(0,1),(0,-1)):
            nx,ny = x+dx,y+dy
            if 0<=nx<w and 0<=ny<h and not visited[ny][nx]:
                r,g,b,a = px[nx,ny]
                if r>=thresh and g>=thresh and b>=thresh:
                    visited[ny][nx]=True
                    q.append((nx,ny))
    # slight alpha edge soften
    alpha = img.split()[3].filter(ImageFilter.GaussianBlur(0.6))
    img.putalpha(alpha)
    return img

for src, dst in FILES:
    sp = os.path.join(SRC_DIR, src)
    dp = os.path.join(DST_DIR, dst)
    if not os.path.exists(sp):
        print(f"MISSING: {sp}"); continue
    im = Image.open(sp)
    im = remove_white_bg(im)
    im.save(dp, "WEBP", quality=92)
    print(f"OK {dst}  {im.size}")

print("done ->", DST_DIR)
