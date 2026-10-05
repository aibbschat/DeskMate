#!/usr/bin/env python3
"""生成手机端 PWA 图标（D7 已拍：用小桔立绘重出一版干净的，别把 1024²/830KB 原图塞进窄屏）。

为什么需要脚本而不是直接缩放：主屏图标必须是**不透明**的（iOS 会自己加圆角遮罩，
透明底会在深色桌面上糊成一块），而源立绘是透明 PNG。所以这里把立绘居中贴到皮肤暖底上，
再按各尺寸导出。

用法：python3 scripts/gen_mobile_icons.py
输入：surfaces/gui/src/assets/mascot-xiaoju.png
输出：surfaces/gui/public/m-icon-{180,192,512}.png
"""
from __future__ import annotations

import pathlib
import sys

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.exit("需要 Pillow：pip install pillow")

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "surfaces/gui/src/assets/mascot-xiaoju.png"
OUT = ROOT / "surfaces/gui/public"

# 底色取自 styles.css 的 xiaoju-orange 亮色表面（--accent-soft），与小桔皮肤同色系。
BG = (255, 231, 204, 255)
# 立绘占画布的比例：留一点边，主屏上不至于顶到遮罩圆角
PAD_RATIO = 0.86


def render(size: int) -> Image.Image:
    src = Image.open(SRC).convert("RGBA")
    # 裁掉四周透明边，让脸居中而不是被原始画布的留白挤偏
    bbox = src.getbbox()
    if bbox:
        src = src.crop(bbox)

    inner = int(round(size * PAD_RATIO))
    sw, sh = src.size
    scale = inner / max(sw, sh)
    resized = src.resize((max(1, int(sw * scale)), max(1, int(sh * scale))), Image.LANCZOS)

    canvas = Image.new("RGBA", (size, size), BG)
    canvas.alpha_composite(resized, ((size - resized.width) // 2, (size - resized.height) // 2))
    return canvas.convert("RGB")  # 主屏图标要不透明底


def main() -> None:
    if not SRC.exists():
        sys.exit(f"找不到源立绘：{SRC}")
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (180, 192, 512):
        img = render(size)
        dest = OUT / f"m-icon-{size}.png"
        img.save(dest, optimize=True)
        print(f"wrote {dest.relative_to(ROOT)}  {size}x{size}  {dest.stat().st_size // 1024}KB")


if __name__ == "__main__":
    main()
