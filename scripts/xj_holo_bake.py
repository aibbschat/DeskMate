#!/usr/bin/env python3
"""桌宠 working 段烘焙全息投影键盘。

背景：working 是"低头双手悬空点按"的动作，手指底下没有东西，观感出戏。
方案：在她双手下方叠一块激光投影键盘（半透明发光光垫，7x3 键）。
投影键盘的用法本来就是"手指在投影上方的空中点按"，不需要接触实体，
所以现有帧的动作不用重做，键盘固定在空间里、帧动它不动。

实现要点（都是从预览阶段踩出来的，别"优化"掉）：
1. 键盘必须按目标尺寸逐键画多边形。用 Image.transform(QUAD) 做整图透视
   会把源图边界一起采样进来，输出里凭空多出一块不透明底板。
2. 发光(bloom)和洒光(spill)半径必须按目标像素算；在超大源图上模糊再缩小，
   整片键面会被摊平成一块灰板。
3. 洒光必须乘角色自身的 alpha 裁掉——光只落在"有东西的地方"，
   落在空气里就会变成一块浅灰矩形底板。

用法: unset PYTHONPATH && python3 scripts/xj_holo_bake.py [--check]
恢复烘焙前帧: git checkout HEAD -- surfaces/gui/public/pet-clips/working/
"""
from __future__ import annotations

import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIP_DIR = os.path.join(ROOT, "surfaces/gui/public/pet-clips/working/frames")
WEBP_QUALITY = 82  # 与 xj_clip_build.py 保持一致

# ── C 方案定稿参数（2026-11-05 萧哥确认）──────────────────────────
WIDTH = 190          # 光垫宽（画布 720）
COLS, ROWS = 7, 3
TOP = 430            # 光垫远端上沿（她指尖下方）
TINT = (0, 185, 255)
LINE = (215, 245, 255)
TOP_RATIO = 0.72     # 远端收窄比例（透视）
OPACITY, SPILL, BLOOM = 1.0, 0.45, 1.0


def procedural_holo(width=WIDTH, cols=COLS, rows=ROWS, tint=TINT, line=LINE,
                    top_ratio=TOP_RATIO, ss=4):
    """逐键画四边形的光键盘。ss = 超采样倍数，只为抗锯齿。"""
    W, H = int(width * ss), int(round(width * 0.30 * ss))
    fills = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    edge = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    df, de = ImageDraw.Draw(fills), ImageDraw.Draw(edge)

    pad_u, gap_u = 0.030, 0.012
    kw_u = (1 - 2 * pad_u - (cols - 1) * gap_u) / cols
    y0, y1 = 0.10, 0.94

    def px(u, v):
        s = top_ratio + (1 - top_ratio) * v   # 越靠下越宽
        return (W / 2 + (u - 0.5) * W * s, H * v)

    def quad(u0, u1, v0, v1, fill=None, outline=None, wpx=1.6):
        pts = [px(u0, v0), px(u1, v0), px(u1, v1), px(u0, v1)]
        if fill:
            df.polygon(pts, fill=fill)
        if outline:
            de.polygon(pts, outline=outline, width=max(1, int(wpx * ss)))

    for r in range(rows):
        t0, t1 = (r / rows) ** 1.45, ((r + 1) / rows) ** 1.45   # 远处行更窄更密
        v0, v1 = y0 + (y1 - y0) * t0, y0 + (y1 - y0) * t1
        k = 0.55 + 0.45 * (r / max(rows - 1, 1))                 # 远端更淡
        for c in range(cols):
            u0 = pad_u + c * (kw_u + gap_u)
            quad(u0, u0 + kw_u, v0, v1,
                 fill=tint + (int(34 * k),), outline=line + (int(240 * k),))

    ev = y1 - 0.012                                            # 近端亮边（"前沿"）
    quad(pad_u * 0.6, 1 - pad_u * 0.6, ev, y1, fill=line + (150,))

    out = Image.alpha_composite(fills, edge)
    return out.resize((int(width), int(round(width * 0.30))), Image.LANCZOS)


def bake_frame(frame: Image.Image, glow: Image.Image) -> Image.Image:
    """把发光层贴到一帧上。投影钉在空间里：帧在动，它不动。"""
    w, h = glow.size
    g = glow.copy()
    if OPACITY < 1.0:
        g.putalpha(g.split()[-1].point(lambda v: int(v * OPACITY)))
    pos = (720 // 2 - w // 2, TOP)
    base = frame

    if SPILL > 0:
        halo = g.filter(ImageFilter.GaussianBlur(max(4, w // 10)))
        char_a = np.asarray(frame.split()[-1], dtype=np.float32) / 255.0
        patch = np.zeros(frame.size[::-1], dtype=np.float32)
        patch[pos[1]:pos[1] + h, pos[0]:pos[0] + w] = 1.0
        mask = (char_a * patch) ** 1.5
        a = np.asarray(halo.split()[-1], dtype=np.float32) * SPILL
        a = a * mask[pos[1]:pos[1] + h, pos[0]:pos[0] + w]
        halo.putalpha(Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "L"))
        layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        layer.alpha_composite(halo, pos)
        base = Image.alpha_composite(base, layer)

    if BLOOM > 0:
        bl = g.filter(ImageFilter.GaussianBlur(max(1.0, w / 90)))
        bl.putalpha(bl.split()[-1].point(lambda v: int(v * BLOOM)))
        layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        layer.alpha_composite(bl, pos)
        base = Image.alpha_composite(base, layer)

    layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    layer.alpha_composite(g, pos)
    return Image.alpha_composite(base, layer)


def frame_has_holo_kb(im: Image.Image, band=(420, 500, 240, 480), need=120) -> bool:
    """这帧在双手下方有没有青色发光块（=投影键盘）。

    判据刻意用"蓝远高于红"：她身上是橙/暖色（R≥B），叶子是绿（B 低），
    原素材里不存在真正青蓝的像素，所以不是"凡亮都算"。
    """
    im = im.convert("RGBA").crop((band[2], band[0], band[3], band[1]))
    a = np.asarray(im, dtype=np.int16)
    r, b, alpha = a[..., 0], a[..., 2], a[..., 3]
    return int(((alpha > 60) & (b > 150) & (b - r > 40)).sum()) >= need


def main() -> int:
    files = sorted(f for f in os.listdir(CLIP_DIR) if f.endswith(".webp"))
    if "--check" in sys.argv:
        missing = [f for f in files
                   if not frame_has_holo_kb(Image.open(os.path.join(CLIP_DIR, f)))]
        print("缺键盘的帧:", missing or "无")
        return 1 if missing else 0

    glow = procedural_holo()
    total = 0
    for f in files:
        im = Image.open(os.path.join(CLIP_DIR, f)).convert("RGBA")
        if im.size != (720, 720):
            raise SystemExit(f"{f} 画布不是 720²: {im.size}")
        out = bake_frame(im, glow)
        p = os.path.join(CLIP_DIR, f)
        out.save(p, "WEBP", quality=WEBP_QUALITY, method=6)
        total += os.path.getsize(p)
    print(f"烘焙 {len(files)} 帧，合计 {total / 1e6:.1f} MB（原 2.98s 段）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
