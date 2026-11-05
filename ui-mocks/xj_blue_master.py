#!/usr/bin/env python3
"""把蓝幕母图规整成可用资产。

输入：AI 生成/视频截帧的蓝幕图（背景可能带渐变、水印、色偏）
输出：
  1) master.png   ：背景被压成纯 #0000FF 的不透明母图（喂视频生成用）
  2) keyed.png    ：抠好的 RGBA 版本（喂拆帧/前端用）
  3) preview-*.png：浅底/深底实尺寸观感图

用法：python3 xj_blue_master.py <src.png> <out_prefix> [--size 1024x1536]
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

# 键控参数：bdom = B - max(R, G)
LO = 40.0   # 完全保留（不透明）
HI = 80.0   # 完全抠掉（透明）
DESPILL_R = 3
ALPHA_FLOOR = 40  # 8bit alpha 低于此值直接清零，避免蓝边


def load(src: Path) -> np.ndarray:
    return np.asarray(Image.open(src).convert("RGB")).astype(np.float32)


def bdom_map(rgb: np.ndarray) -> np.ndarray:
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    return b - np.maximum(r, g)


def estimate_bg(rgb: np.ndarray) -> tuple[int, int, int]:
    """取四角与四边中点的中位数当背景色。"""
    h, w, _ = rgb.shape
    pts = [
        rgb[2, 2], rgb[2, w - 3], rgb[h - 3, 2], rgb[h - 3, w - 3],
        rgb[2, w // 2], rgb[h - 3, w // 2], rgb[h // 2, 2], rgb[h // 2, w - 3],
    ]
    return tuple(int(v) for v in np.median(np.stack(pts), axis=0))


def strip_watermark(rgb: np.ndarray, bg: tuple[int, int, int]) -> tuple[np.ndarray, tuple | None]:
    """清掉右下角的 AI 水印。

    水印是贴在蓝底上的淡蓝/白字（冷色），而角色身上的橙鞋是暖色（R>B）。
    只按"非蓝"筛会把鞋一起圈进去吃掉 → 必须用冷暖色相区分：
      · 水印：B 明显大于 R（偏蓝），且够亮、bdom 未到纯底色
      · 鞋/皮肤/裙子：R ≥ B，一律不碰
    只填水印自身的像素（向外膨胀几像素盖住抗锯齿边），不做整块矩形填充。
    """
    h, w, _ = rgb.shape
    y0, x0 = int(h * 0.88), int(w * 0.70)  # 水印只在最右下；x 门槛要高于脚(实测脚尖 x≈0.64w)
    patch = rgb[y0:, x0:]
    r, g, b = patch[..., 0], patch[..., 1], patch[..., 2]
    bd = b - np.maximum(r, g)
    sat = patch.max(axis=2) - patch.min(axis=2)
    bright = patch.mean(axis=2)

    cool = (b - r > 25) & (bright > 140) & (bd < (HI + 45))
    gray = (sat < 35) & (bright > 160)
    mask = cool | gray
    # 排除纯底色（纯蓝 sat 极高，不会被 gray 命中，但 cool 可能命中亮蓝边）
    mask &= bright > 130

    if mask.sum() < 30:  # 没有水印或只是噪点
        return rgb, None

    # 膨胀 3 像素，盖住字边抗锯齿残留
    grown = mask.copy()
    for _ in range(3):
        g2 = grown.copy()
        g2[1:, :] |= grown[:-1, :]
        g2[:-1, :] |= grown[1:, :]
        g2[:, 1:] |= grown[:, :-1]
        g2[:, :-1] |= grown[:, 1:]
        grown = g2
    # 膨胀时只允许往冷色/低饱和方向扩，避免蹭到暖色鞋边
    warm = (b - r) < -10
    grown &= ~warm

    out = rgb.copy()
    sub = out[y0:, x0:]
    sub[grown] = bg

    ys, xs = np.where(mask)
    return out, (int(xs.min() + x0), int(ys.min() + y0), int(xs.max() + x0), int(ys.max() + y0))


def despill(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """半透明像素的颜色被蓝背景污染 → 用半径内不透明邻居的均值替换。"""
    h, w, _ = rgb.shape
    opaque = alpha >= 0.95
    if not opaque.any():
        return rgb
    # 用逐次平移累积不透明邻居的和
    acc = np.zeros_like(rgb)
    cnt = np.zeros((h, w), dtype=np.float32)
    for dy in range(-DESPILL_R, DESPILL_R + 1):
        for dx in range(-DESPILL_R, DESPILL_R + 1):
            if dx == 0 and dy == 0:
                continue
            sy = slice(max(0, dy), h + min(0, dy))
            sx = slice(max(0, dx), w + min(0, dx))
            ty = slice(max(0, -dy), h + min(0, -dy))
            tx = slice(max(0, -dx), w + min(0, -dx))
            m = opaque[ty, tx]
            acc[sy, sx] += rgb[ty, tx] * m[..., None]
            cnt[sy, sx] += m
    has = cnt > 0
    mean = np.zeros_like(rgb)
    mean[has] = acc[has] / cnt[has][:, None]
    edge = (~opaque) & (alpha > 0.02) & has
    out = rgb.copy()
    out[edge] = mean[edge]
    return out


def build(src: Path, out_prefix: Path) -> dict:
    rgb = load(src)

    # 1) 去水印
    bg = estimate_bg(rgb)
    rgb, wm_box = strip_watermark(rgb, bg)

    # 2) 背景压成纯蓝（去掉渐变与噪点），保留边缘过渡带
    d = bdom_map(rgb)
    solid_bg = d > HI
    rgb[solid_bg] = (0.0, 0.0, 255.0)

    # 3) 母图
    master = out_prefix.with_name(out_prefix.name + "-master.png")
    Image.fromarray(rgb.astype(np.uint8), "RGB").save(master)

    # 4) 抠像
    alpha = np.clip((HI - bdom_map(rgb)) / (HI - LO), 0.0, 1.0)
    alpha[alpha * 255 < ALPHA_FLOOR] = 0.0
    rgb = despill(rgb, alpha)
    rgba = np.dstack([rgb, alpha * 255.0]).astype(np.uint8)
    keyed = out_prefix.with_name(out_prefix.name + "-keyed.png")
    Image.fromarray(rgba, "RGBA").save(keyed)

    return {
        "size": f"{rgb.shape[1]}x{rgb.shape[0]}",
        "bg": bg,
        "watermark": wm_box,
        "master": str(master),
        "keyed": str(keyed),
        "coverage": float((alpha > 0).mean()),
    }


def preview(keyed: Path, out_prefix: Path, box_h: int = 200) -> list[str]:
    im = Image.open(keyed).convert("RGBA")
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    ratio = box_h / im.height
    small = im.resize((max(1, int(im.width * ratio)), box_h), Image.LANCZOS)
    out = []
    for name, color in (("light", (245, 246, 248, 255)), ("dark", (28, 30, 34, 255))):
        canvas = Image.new("RGBA", (small.width + 40, small.height + 40), color)
        canvas.alpha_composite(small, (20, 20))
        p = out_prefix.with_name(f"{out_prefix.name}-{box_h}-{name}.png")
        canvas.convert("RGB").save(p)
        out.append(str(p))
    return out


if __name__ == "__main__":
    src = Path(sys.argv[1])
    prefix = Path(sys.argv[2])
    info = build(src, prefix)
    pv = preview(Path(info["keyed"]), prefix)
    for k, v in info.items():
        print(f"{k}: {v}")
    print("previews:", *pv, sep="\n  ")
