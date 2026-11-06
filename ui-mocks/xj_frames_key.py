#!/usr/bin/env python3
"""把蓝幕视频帧切成可用的透明动画。

针对 AI 视频通道的三个固有毛病做了处理（都是实测撞出来的）：
  1. 视频模型会在画面角落重新盖一层「AI生成」水印 → 按"浅冷色"检测后填纯蓝，键控时自然消失
  2. 蓝底不是平的（渐晕/渐变，bdom 最低只有 82，贴着阈值） → 改用"蓝相"判据，蓝相像素一律 alpha=0
  3. 角色脚下一层淡投影，bdom 落在 40~80 会变成半透明灰雾 → 同上，蓝相判据一并解决
角色身上没有任何蓝色系像素（橙/棕/肤色/奶白/绿），所以蓝相判据不会误伤。

用法:
  python3 xj_frames_key.py <frames_dir> <out_prefix> [--height 360] [--fps 24] [--max-seconds 2.5]
输出:
  <out_prefix>-seq/frame-XXXX.png   逐帧 RGBA（统一裁切窗口）
  <out_prefix>.png                  APNG（WKWebView 原生支持，带 alpha）
  <out_prefix>.webp                 动图 WebP（体积对照用）
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

import numpy as np
from PIL import Image

HI = 80.0
LO = 40.0
ALPHA_FLOOR = 38


def bdom_map(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 2] - np.maximum(rgb[..., 0], rgb[..., 1])


def blue_family(rgb: np.ndarray) -> np.ndarray:
    """蓝相判定：B 明显高于 R/G 且不是灰白（有饱和度）。角色身上不存在这类像素。"""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = rgb.max(axis=2)
    mn = rgb.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1.0), 0.0)
    return (b > r + 12) & (b > g + 6) & (sat > 0.22)


def kill_watermark(rgb: np.ndarray) -> tuple[np.ndarray, list[tuple]]:
    """检测角落的浅冷色水印块并填纯蓝（键控会把它吃掉）。

    注意：两个角必须各自求 bbox 再填。早期版本把左上和右下的候选合成一个矩形，
    结果矩形横跨整幅画面、把角色一起填成纯蓝抠掉（实测 60 帧里后 50 帧全灭）。
    另外给每个 bbox 面积设上限，防止零星噪点撑出大框。
    """
    h, w, _ = rgb.shape
    br = rgb.mean(axis=2)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = rgb.max(axis=2)
    mn = rgb.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1.0), 0.0)
    pale_cool = (br > 135) & (b > r + 10) & (sat < 0.55)
    pale_gray = (br > 185) & (sat < 0.22)
    cand_all = pale_cool | pale_gray

    bands = [
        (slice(0, int(h * 0.11)), slice(0, int(w * 0.26))),        # 左上
        (slice(int(h * 0.88), h), slice(int(w * 0.62), w)),        # 右下
    ]
    out = rgb.copy()
    hits: list[tuple] = []
    cap = 0.05 * h * w
    for ys_sl, xs_sl in bands:
        cand = np.zeros((h, w), bool)
        cand[ys_sl, xs_sl] = cand_all[ys_sl, xs_sl]
        n = int(cand.sum())
        if n < 60:
            continue
        ys, xs = np.where(cand)
        y0, y1 = max(0, ys.min() - 4), min(h, ys.max() + 5)
        x0, x1 = max(0, xs.min() - 4), min(w, xs.max() + 5)
        if (y1 - y0) * (x1 - x0) > cap:
            print(f"  跳过过大的水印候选框 {(x0,y0,x1,y1)} 面积超限", file=sys.stderr)
            continue
        out[y0:y1, x0:x1] = (0.0, 0.0, 255.0)
        hits.append((int(x0), int(y0), int(x1), int(y1)))
    return out, hits


def despill(rgb: np.ndarray, alpha: np.ndarray, radius: int = 2) -> np.ndarray:
    h, w, _ = rgb.shape
    opaque = alpha >= 0.9
    if not opaque.any():
        return rgb
    acc = np.zeros_like(rgb)
    cnt = np.zeros((h, w), np.float32)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
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


def key_frame(path: str) -> np.ndarray:
    rgb = np.asarray(Image.open(path).convert("RGB")).astype(np.float32)
    rgb, _ = kill_watermark(rgb)
    blue = blue_family(rgb)
    a_bdom = np.clip((HI - bdom_map(rgb)) / (HI - LO), 0.0, 1.0)
    alpha = np.where(blue, 0.0, a_bdom)
    alpha[alpha * 255 < ALPHA_FLOOR] = 0.0
    rgb = despill(rgb, alpha)
    return np.dstack([rgb, alpha * 255.0]).astype(np.uint8)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("frames_dir")
    ap.add_argument("out_prefix")
    ap.add_argument("--height", type=int, default=360)
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--max-seconds", type=float, default=2.5)
    ap.add_argument("--margin", type=float, default=0.03)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.frames_dir, "*.png")))
    if not files:
        print("没有帧", file=sys.stderr)
        return 1
    limit = int(args.fps * args.max_seconds)
    files = files[:limit]

    imgs = [key_frame(f) for f in files]
    print(f"键控完成 {len(imgs)} 帧")

    # 统一裁切窗口：取所有帧 alpha 的并集，保证动画期间不跳
    union = None
    for im in imgs:
        ys, xs = np.where(im[..., 3] > 0)
        if len(ys) == 0:
            continue
        box = (xs.min(), ys.min(), xs.max(), ys.max())
        union = box if union is None else (
            min(union[0], box[0]), min(union[1], box[1]),
            max(union[2], box[2]), max(union[3], box[3]),
        )
    if union is None:
        print("全程透明，检查键控阈值", file=sys.stderr)
        return 1

    h, w = imgs[0].shape[:2]
    mx = int((union[2] - union[0]) * args.margin)
    my = int((union[3] - union[1]) * args.margin)
    x0, y0 = max(0, union[0] - mx), max(0, union[1] - my)
    x1, y1 = min(w, union[2] + mx + 1), min(h, union[3] + my + 1)
    # 保持宽高比到目标高度
    bw, bh = x1 - x0, y1 - y0
    scale = args.height / bh
    tw = max(1, int(round(bw * scale)))
    print(f"裁切窗口 {bw}x{bh} → 输出 {tw}x{args.height}")

    seq_dir = f"{args.out_prefix}-seq"
    os.makedirs(seq_dir, exist_ok=True)
    frames = []
    for i, im in enumerate(imgs):
        crop = Image.fromarray(im[y0:y1, x0:x1], "RGBA").resize((tw, args.height), Image.LANCZOS)
        crop.save(os.path.join(seq_dir, f"frame-{i:04d}.png"))
        frames.append(crop)

    dur = int(round(1000 / args.fps))
    frames[0].save(
        f"{args.out_prefix}.png", save_all=True, append_images=frames[1:],
        duration=dur, loop=0, disposal=1, blend=0, optimize=True,
    )
    frames[0].save(
        f"{args.out_prefix}.webp", save_all=True, append_images=frames[1:],
        duration=dur, loop=0, quality=82, method=6,
    )
    for p in (f"{args.out_prefix}.png", f"{args.out_prefix}.webp"):
        print(f"{p}: {os.path.getsize(p)/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
