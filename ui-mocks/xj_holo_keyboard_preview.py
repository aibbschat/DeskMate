#!/usr/bin/env python3
"""小桔 · 全息投影键盘叠加预览（look-test，不改正式素材）。

思路：生成图是纯黑底发光体 -> 用亮度当 alpha（等价 additive 合成），
这样不需要抠像，投影该透的地方自然透。

用法: unset PYTHONPATH && python3 ui-mocks/xj_holo_keyboard_preview.py
产出: ui-mocks/out/holo_preview_*.png
"""
from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_SRC = os.path.join(ROOT, "ui-mocks/assets/A_futuristic_laser_projected_h_2026-11-05T06-25-05.png")
FRAME = os.path.join(ROOT, "surfaces/gui/public/pet-clips/working/frames/working_020.webp")
OUT = os.path.join(ROOT, "ui-mocks/out")

# 角色帧 720²：手在 y≈405-448、x≈288-432（掌心朝下悬在肚子前）
HAND_TOP = 441.0
HAND_CX = 360.0


# 裁到"键位实体块"，把上下飘散的细光丝去掉——那些光丝在 200px 下会糊成一条白带
KB_CROP = (60, 232, 992, 648)


def procedural_holo(width=232, cols=10, rows=4, tint=(80, 225, 255), line=(225, 250, 255),
                    top_ratio=0.76, ss=4):
    """程序化画一块全息键盘：逐键画四边形，透视直接算进顶点坐标。

    🔴 用 `Image.transform(QUAD)` 做整图透视会把源图边界一起采样进来，
       输出里多出一块不透明底板（看着像贴了张卡片）。所以按目标尺寸直接画。

    透视规则：远处（上方）行距更密、整排更窄；近处（下方）行距更疏、更宽。
    ss = 超采样倍数，只为抗锯齿。
    """
    W, H = int(width * ss), int(round(width * 0.30 * ss))
    fills = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    edge = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    df, de = ImageDraw.Draw(fills), ImageDraw.Draw(edge)

    pad_u, gap_u = 0.030, 0.012          # 单位宽度下的边距/键距
    kw_u = (1 - 2 * pad_u - (cols - 1) * gap_u) / cols
    y0, y1 = 0.10, 0.94                  # 键面在画布里的上下留白

    def v_of(t):                          # 行边界：上密下疏
        return y0 + (y1 - y0) * t

    def px(u, v):                         # 单位坐标 -> 像素
        s = top_ratio + (1 - top_ratio) * v   # 越靠下越宽
        return (W / 2 + (u - 0.5) * W * s, H * v)

    def quad(u0, u1, v0, v1, fill=None, outline=None, wpx=2):
        pts = [px(u0, v0), px(u1, v0), px(u1, v1), px(u0, v1)]
        if fill:
            df.polygon(pts, fill=fill)
        if outline:
            de.polygon(pts, outline=outline, width=max(1, int(wpx * ss)))

    for r in range(rows):
        t0, t1 = (r / rows) ** 1.45, ((r + 1) / rows) ** 1.45   # 远处行更窄
        v0, v1 = v_of(t0), v_of(t1)
        # 远端更淡：给"面在往后躺"的深度线索，否则像一条平贴的发光带
        k = 0.55 + 0.45 * (r / max(rows - 1, 1))
        c = 0
        while c < cols:
            u0 = pad_u + c * (kw_u + gap_u)
            span = 1
            if r == rows - 1 and c == 3:      # 最下一排中间连成空格键
                span = 4
            u1 = u0 + kw_u * span + gap_u * (span - 1)
            quad(u0, u1, v0, v1, fill=tint + (int(34 * k),),
                 outline=line + (int(240 * k),), wpx=1.6)
            c += span

    # 近端亮边：投影平面的"前沿"，这条线最能让人读出"一块平躺的板"
    ev = y1 - 0.012
    quad(pad_u * 0.6, 1 - pad_u * 0.6, ev, y1, fill=line + (150,), outline=None)

    # 上采样回目标尺寸。这里只缩小、没有模糊，不会摊出底板。
    return Image.alpha_composite(fills, edge).resize((int(width), int(round(width * 0.30))),
                                                     Image.LANCZOS)


def add_hint_line(im, tint=(80, 225, 255)):
    """键面下方一条极淡的横向扫描线，暗示这是"投出来的"而不是实体。"""
    w, h = im.size
    band = Image.new("RGBA", (w, max(2, h // 14)), tint + (40,))
    out = im.copy()
    out.alpha_composite(band, (0, int(h * 0.55)))
    return out


def load_glow(tint=(80, 225, 255), mix=0.55, lo=18.0, hi=170.0, gamma=1.0):
    """把黑底发光图转成 RGBA 发光层。alpha 由亮度决定，色相统一到 tint。"""
    im = Image.open(KB_SRC).convert("RGB")
    a = np.asarray(im, dtype=np.float32)
    lum = a.max(axis=2)  # 发光是叠加的，取最亮通道比 L 稳
    alpha = np.clip((lum - lo) / (hi - lo), 0, 1) ** gamma

    # 统一色相：保留亮度结构，色相拉到 tint
    tint_arr = np.array(tint, dtype=np.float32)
    shape = (lum / max(lum.max(), 1.0))[..., None] ** 0.9
    tinted = tint_arr * shape
    rgb = a * (1 - mix) + tinted * mix
    # 提亮到发光该有的量级（原图 A 通道被当亮度用了，只在 alpha 里起作用）
    rgb = np.clip(rgb * 1.35 + tint_arr * (alpha[..., None] * 0.55), 0, 255)

    rgba = np.dstack([rgb, alpha * 255.0]).astype(np.uint8)
    out = Image.fromarray(rgba, "RGBA")
    return out.crop(KB_CROP)


def perspective(im, top_ratio=0.86):
    """顶端收窄，做出"往远处退"的透视；否则看起来像贴了张平的贴纸。

    ⚠ PIL 的 QUAD 顶点顺序是 左上→左下→右下→右上（不是顺时针绕一圈），
    顺序错了不会报错，只会把图挤成竖条。
    """
    w, h = im.size
    dx = (w * (1 - top_ratio)) / 2
    return im.transform(
        (w, h), Image.QUAD,
        (dx, 0, 0, h, w, h, w - dx, 0),
        Image.BICUBIC,
    )


def _edge_fade(im, frac=0.10):
    """四周按比例羽化。硬切口会让它看起来像贴纸，不像光。"""
    w, h = im.size
    al = np.asarray(im.split()[-1], dtype=np.float32) / 255.0
    ramp_x = np.clip(np.minimum(np.arange(w), w - 1 - np.arange(w)) / (w * frac), 0, 1)
    ramp_y = np.clip(np.minimum(np.arange(h), h - 1 - np.arange(h)) / (h * frac), 0, 1)
    al *= ramp_y[:, None] * ramp_x[None, :]
    out = im.copy()
    out.putalpha(Image.fromarray((al * 255).astype(np.uint8), "L"))
    return out


def place(frame, glow, width=340, squash=0.55, top=HAND_TOP, cx=HAND_CX, opacity=1.0,
          spill=0.0, bloom=0.0):
    """把发光层贴到角色帧上。bloom/spill 半径都按目标尺寸算（见 procedural_holo 注释）。"""
    w = int(width)
    h = max(1, int(round(glow.height * (w / glow.width) * squash)))
    g = glow.resize((w, h), Image.LANCZOS)
    if opacity < 1.0:
        g.putalpha(g.split()[-1].point(lambda v: int(v * opacity)))

    base = frame
    pos = (int(cx - w / 2), int(top))

    if spill > 0:
        # 投影洒在她身上的光。🔴 必须乘角色自身的 alpha 裁掉：光只落在"有东西的地方"，
        # 落在空气里就会变成一块浅灰矩形底板（看着像贴了张卡片，不像投影）。
        halo = g.filter(ImageFilter.GaussianBlur(max(4, w // 10)))
        char_a = np.asarray(frame.split()[-1], dtype=np.float32) / 255.0
        patch = np.zeros(frame.size[::-1], dtype=np.float32)
        patch[pos[1]:pos[1] + h, pos[0]:pos[0] + w] = 1.0
        mask = (char_a * patch) ** 1.5
        a = np.asarray(halo.split()[-1], dtype=np.float32) * spill
        a = a * mask[pos[1]:pos[1] + h, pos[0]:pos[0] + w]
        halo.putalpha(Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "L"))
        layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        layer.alpha_composite(halo, pos)
        base = Image.alpha_composite(base, layer)

    if bloom > 0:
        bl = g.filter(ImageFilter.GaussianBlur(max(1.0, w / 90)))
        bl.putalpha(bl.split()[-1].point(lambda v: int(v * bloom)))
        layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        layer.alpha_composite(bl, pos)
        base = Image.alpha_composite(base, layer)

    layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    layer.alpha_composite(g, pos)
    return Image.alpha_composite(base, layer)


def on_bg(sprite, size=200, bg=(238, 246, 235)):
    """按真实桌宠尺寸铺在浅色背景上，检验小尺寸下是否还成立。"""
    s = sprite.resize((size, size), Image.LANCZOS)
    pad = int(size * 0.18)
    canvas = Image.new("RGBA", (size + pad * 2, size + pad * 2), bg + (255,))
    canvas.alpha_composite(s, (pad, pad))
    return canvas


def bake_clip(clip="working", size=200, glow_kw=None, place_kw=None, out_name=None):
    """把键盘压进整段 working 帧，做成动图。投影是钉在空间里的，帧在动它不动——
    这正好也是真接入时的行为，所以动图能当验收用。"""
    src = os.path.join(ROOT, "surfaces/gui/public/pet-clips", clip, "frames")
    files = sorted(f for f in os.listdir(src) if f.endswith(".webp"))
    glow = procedural_holo(**(glow_kw or {}))
    frames = []
    for f in files:
        sp = place(Image.open(os.path.join(src, f)).convert("RGBA"), glow,
                   **(place_kw or {}))
        # GIF 只有 1bit 透明，压到浅底上更稳（真接入时仍是透明帧）
        bg = Image.new("RGBA", sp.size, (238, 246, 235, 255))
        shot = Image.alpha_composite(bg, sp).convert("RGB")
        if size != shot.width:
            shot = shot.resize((size, size), Image.LANCZOS)
        frames.append(shot.convert("P", palette=Image.ADAPTIVE, colors=128))
    out = os.path.join(OUT, out_name or f"baked_{clip}_{size}.gif")
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=42, loop=0,
                   optimize=True, disposal=2)
    print("wrote", out, len(frames), "帧")
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    frame = Image.open(FRAME).convert("RGBA")

    # 素材版（AI 生成光网）vs 程序化矢量版，各给青/暖两色
    jobs = []
    # 素材版（AI 生成光网）：只在原料还在时出。这条路最终没选——AI 生成的键位太密，
    # 缩到 200px 就糊成一条光网，所以成品用的是下面的矢量版。
    if os.path.exists(KB_SRC):
        jobs.append(
            ("A_素材青", lambda: _edge_fade(perspective(load_glow(tint=(80, 225, 255), mix=0.5), 0.9)),
             dict(width=230, squash=0.62, top=432, opacity=0.88, spill=0.45, bloom=0.6))
        )
    jobs += [
        ("D_矢量青", lambda: procedural_holo(width=232, tint=(70, 215, 255), line=(230, 250, 255)),
         dict(width=232, squash=1.0, top=428, opacity=0.95, spill=0.5, bloom=0.9)),
        ("E_矢量暖", lambda: procedural_holo(width=232, tint=(255, 190, 95), line=(255, 240, 210)),
         dict(width=232, squash=1.0, top=428, opacity=0.95, spill=0.45, bloom=0.85)),
        ("F_矢量青小", lambda: procedural_holo(width=204, tint=(70, 215, 255), line=(230, 250, 255)),
         dict(width=204, squash=1.0, top=430, opacity=0.95, spill=0.45, bloom=0.85)),
    ]

    sheets, labels = [], []
    for name, make, place_kw in jobs:
        glow = make()
        sprite = place(frame, glow, **place_kw)
        sprite.save(os.path.join(OUT, f"holo_preview_{name}.png"))
        sheets.append(on_bg(sprite, 200))
        labels.append(name)
        print("wrote", name, "glow", glow.size)

    # 小尺寸对比条
    w = sum(s.width for s in sheets)
    bar = Image.new("RGBA", (w, sheets[0].height + 26), (255, 255, 255, 255))
    d = ImageDraw.Draw(bar)
    x = 0
    for s, lb in zip(sheets, labels):
        bar.alpha_composite(s, (x, 0))
        d.text((x + 6, s.height + 5), lb, fill=(60, 60, 60))
        x += s.width
    bar.save(os.path.join(OUT, "holo_preview_at_200px.png"))
    print("wrote ui-mocks/out/holo_preview_at_200px.png")

    # 动图验收：投影固定在空间里，帧在动它不动
    bake_clip("working", 240,
              dict(width=232, tint=(70, 215, 255), line=(230, 250, 255)),
              dict(width=232, squash=1.0, top=428, opacity=0.95, spill=0.5, bloom=0.9),
              "working_holo_cyan.gif")
    bake_clip("working", 240,
              dict(width=232, tint=(255, 190, 95), line=(255, 240, 210)),
              dict(width=232, squash=1.0, top=428, opacity=0.95, spill=0.45, bloom=0.85),
              "working_holo_warm.gif")


if __name__ == "__main__":
    main()
