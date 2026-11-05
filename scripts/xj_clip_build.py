#!/usr/bin/env python3
"""
xj_clip_build.py — 小桔桌宠序列帧素材流水线。

输入：一段纯色幕 mp4（AI 图生视频产出），绿幕 #00FF00 或蓝幕 #0000FF
输出：public/pet-clips/<clip>/frames/<clip>_NNN.webp（透明 720²）+ 更新 manifest.json

用法：
    python3 scripts/xj_clip_build.py <video.mp4> <clipName> [--fps 24] [--size 720]
                                     [--max-frames N] [--screen green|blue]
                                     [--frames-dir DIR] [--ref-bbox x0,y0,x1,y1] [--dry-run]

抠像算法（绿幕，不依赖 numpy）：
    绿幕信号 gdom = G - max(R,B)。背景 gdom ≈ +235，角色（含深青绿叶 gdom ≈ -11）gdom ≤ 0。
    alpha = clamp((SPILL_HI - gdom) / (SPILL_HI - SPILL_LO), 0, 1)
    边缘 4px 羽化；非纯背景像素做绿边抑制 G = min(G, max(R,B) + 26)。

抠像算法（蓝幕，numpy）：
    蓝幕信号 bdom = B - max(R,G)。选蓝幕是因为小桔头顶有鲜绿叶子，绿幕会把叶子一起键掉。
    角色身上没有任何蓝色系像素（橙/棕/肤色/奶白袜/深青叶），所以「蓝相 + 有饱和度」可一律判背景。
    这一条同时解决实测中 AI 视频通道的两个毛病：蓝底渐晕（bdom 最低只到 82，贴着阈值）、
    脚下淡投影（bdom 落在 40~80 会变成半透明灰雾）。

设计约束（务必遵守，否则素材会废）：
    - 首帧必须是纯色幕，不能带渐变/投影；生成时明确要求 "flat uniform solid #0000FF"。
    - 绿幕模式下角色头顶绿叶必须与绿幕可区分（深青绿 hue≈175°），纯绿叶会被一起键掉 → 优先蓝幕。
    - 循环片建议用 first_last_frame_to_video 且首尾同图，否则接缝会跳。
    - 9 个 clip 必须共用同一套构图参考（--ref-bbox）；否则切换状态时小人会跳大小/跳位置。
    - 视频模型会在角落重新烧「AI生成」水印，抠像前必须按冷色判据清掉（两角各自成框，不可合框）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageFilter, ImageMath

try:  # 蓝幕路径需要 numpy；绿幕路径保持无依赖
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore[assignment]

REPO = Path(__file__).resolve().parent.parent
GUI = REPO / "surfaces" / "gui"
CLIPS_DIR = GUI / "public" / "pet-clips"
MANIFEST = CLIPS_DIR / "manifest.json"

SPILL_LO = 40.0     # gdom 低于此 → 完全不透明
SPILL_HI = 80.0     # gdom 高于此 → 完全透明
# 阈值实测（idle 旧素材是渐变绿幕 + 地面投影，是"最坏输入"）：
#   HI=130 背景几乎没键掉（33 万半透明）；HI=100 键掉 10 万；HI=80 键掉 31 万、只剩 1.6 万半透明；
#   HI=60 更干净但开始啃叶子边缘。纯色 #00FF00 新素材在 HI=80 下完全干净。
DESPILL_MARGIN = 26
FEATHER_PX = 1
WEBP_QUALITY = 82

# 蓝幕参数（与绿幕同量级：bdom 低于 LO 不透明，高于 HI 透明）
BLUE_LO = 40.0
BLUE_HI = 80.0
BLUE_ALPHA_FLOOR = 38      # 8bit alpha 低于此直接清零，压掉残留蓝边
BLUE_DESPILL_R = 2

# 构图：人物高度占画布比例、脚底落在画布高度的哪个位置。
# 9 个 clip 共用同一参考框，保证状态切换时大小与位置不跳。
FRAME_HEIGHT_FRAC = 0.86
FRAME_BOTTOM_FRAC = 0.94

# 生图模型会在右下角烧 "AI生成" 水印。该区域是纯背景，抠像后直接强制透明，
# 比让模型"去水印"稳得多（实测 masked edit 会把整张图重绘成别的角色）。
# 按源图长边比例定义，720² 下 ≈ x>608, y>662。
WM_GUARD = (0.845, 0.920)   # (x/w, y/h) 起点


def apply_wm_guard(im: Image.Image) -> Image.Image:
    w, h = im.size
    x0, y0 = int(w * WM_GUARD[0]), int(h * WM_GUARD[1])
    px = im.load()
    for y in range(y0, h):
        for x in range(x0, w):
            r, g, b, a = px[x, y]
            if a and (r < 70 and g > 140 and b < 70):   # 只清绿底上的水印，不碰角色
                px[x, y] = (0, 0, 0, 0)
    return im


def ffmpeg_exe() -> str:
    # 延迟导入：本机 ffmpeg 是 x86 版跑不起来，蓝幕路径改由外部拆好的帧目录喂入
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def extract_frames(video: Path, work: Path, fps: int) -> list[Path]:
    work.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(video), "-an", "-vf", f"fps={fps}",
           str(work / "raw_%03d.png")]
    subprocess.run(cmd, check=True)
    return sorted(work.glob("raw_*.png"))


def key_frame(img: Image.Image) -> Image.Image:
    """绿幕 → 透明 RGBA。

    ImageMath 踩坑记录（都实测过）：
      - lambda_eval 的 args 是 dict，函数要 a["max"] 取，a.max 会报 'dict' object has no attribute。
      - 命名空间只有 convert/equal/float/int/max/min/notequal，没有 clamp / maximum / minimum。
      - 标量 - Image 会走 __rsub__ → Image.new("I", size, float) 崩；只能用 Image - Image。
      - 没有 floor 除法 //（_Operand 不支持）；`a / int` 映射到 C 层 "div"（整型截断除），
        `a / float` 会因 Image.new("I", size, float) 崩 —— 除数必须是 int。
      - 整型截断除对负数不对称，先用 max() 把 gdom 钳到 ≥ SPILL_LO 再除，保证被减数非负。
    """
    r, g, b = img.split()[:3]
    hi, lo, margin = int(SPILL_HI), int(SPILL_LO), int(DESPILL_MARGIN)
    span = hi - lo
    alpha = ImageMath.lambda_eval(
        lambda a: a["convert"](
            a["min"]((hi - a["max"](a["g"] - a["max"](a["r"], a["b"]), lo)) * 255 / span, 255), "L"),
        r=r, g=g, b=b,
    )
    g2 = ImageMath.lambda_eval(
        lambda a: a["convert"](a["min"](a["g"], a["max"](a["r"], a["b"]) + margin), "L"),
        r=r, g=g, b=b,
    )
    if FEATHER_PX:
        alpha = alpha.filter(ImageFilter.MinFilter(1 + 2 * FEATHER_PX))
    return Image.merge("RGBA", (r, g2, b, alpha))


def _dilate(mask: "np.ndarray", rounds: int) -> "np.ndarray":
    out = mask.copy()
    for _ in range(rounds):
        g = out.copy()
        g[1:, :] |= out[:-1, :]
        g[:-1, :] |= out[1:, :]
        g[:, 1:] |= out[:, :-1]
        g[:, :-1] |= out[:, 1:]
        out = g
    return out


def _kill_wm_blue(arr: "np.ndarray") -> "np.ndarray":
    """清掉角落的「AI生成」水印：按冷色判据找候选，两角各自成框填纯蓝。

    两个必须守住的点（都是实测翻车换来的）：
      1. 左上与右下各自成框。把两角的候选合成一个 bbox，矩形会横跨整幅画面，
         把角色一起填成纯蓝抠掉（60 帧里后 50 帧全灭）。
      2. 候选必须先排除「角色邻域」。右下角的判定带（x>0.62w, y>0.88h）会盖住她的右脚尖，
         而脚尖与蓝底混合出的浅色像素恰好满足「浅冷色」判据 —— 于是被当成水印框住填掉，
         表现为某几帧右脚缺一角、逐帧忽有忽无地闪。强前景膨胀 5px 划为禁区，填色时也不碰禁区。
    """
    h, w, _ = arr.shape
    br = arr.mean(axis=2)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    mx, mn = arr.max(axis=2), arr.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1.0), 0.0)
    pale_cool = (br > 135) & (b > r + 10) & (sat < 0.55)
    pale_gray = (br > 185) & (sat < 0.22)
    cand_all = pale_cool | pale_gray

    # 角色禁区：明显不属于蓝底、也不是浅色的像素（橙裙/棕发/肤色/深色鞋底）
    bdom = b - np.maximum(r, g)
    strong_fg = bdom < 30
    guard = _dilate(strong_fg, 5)
    cand_all = cand_all & ~guard

    out = arr.copy()
    cap = 0.05 * h * w
    for ys, xs in (
        (slice(0, int(h * 0.11)), slice(0, int(w * 0.26))),
        (slice(0, int(h * 0.09)), slice(int(w * 0.76), w)),   # 右上（实测换幕布色那版水印跑到这儿）
        (slice(int(h * 0.88), h), slice(int(w * 0.62), w)),
    ):
        cand = np.zeros((h, w), bool)
        cand[ys, xs] = cand_all[ys, xs]
        if int(cand.sum()) < 60:
            continue
        yy, xx = np.where(cand)
        y0, y1 = max(0, yy.min() - 4), min(h, yy.max() + 5)
        x0, x1 = max(0, xx.min() - 4), min(w, xx.max() + 5)
        if (y1 - y0) * (x1 - x0) > cap:
            print(f"  跳过过大的水印候选框 {(x0, y0, x1, y1)}（面积超限）", file=sys.stderr)
            continue
        fill = np.zeros((h, w), bool)
        fill[y0:y1, x0:x1] = True
        out[fill & ~guard] = (0.0, 0.0, 255.0)
    return out


def key_frame_blue(img: Image.Image) -> Image.Image:
    """蓝幕 → 透明 RGBA。

    判据不是单纯 bdom 阈值，而是「蓝相 + 有饱和度」：角色身上没有任何蓝色系像素，
    所以渐变底的边角、脚下的淡投影（都是蓝相）会被一起判成背景，不留灰雾。
    """
    if np is None:
        sys.exit("蓝幕模式需要 numpy：pip install numpy")
    arr = np.asarray(img.convert("RGB")).astype(np.float32)
    arr = _kill_wm_blue(arr)

    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    mx, mn = arr.max(axis=2), arr.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1.0), 0.0)
    blue = (b > r + 12) & (b > g + 6) & (sat > 0.22)

    bdom = b - np.maximum(r, g)
    a = np.clip((BLUE_HI - bdom) / (BLUE_HI - BLUE_LO), 0.0, 1.0)
    alpha = np.where(blue, 0.0, a)
    alpha[alpha * 255 < BLUE_ALPHA_FLOOR] = 0.0

    # 去污：半透明边缘取邻域不透明像素的均值颜色，避免蓝边
    opaque = alpha >= 0.9
    if opaque.any():
        h, w, _ = arr.shape
        acc = np.zeros_like(arr)
        cnt = np.zeros((h, w), np.float32)
        rad = BLUE_DESPILL_R
        for dy in range(-rad, rad + 1):
            for dx in range(-rad, rad + 1):
                if dx == 0 and dy == 0:
                    continue
                sy = slice(max(0, dy), h + min(0, dy))
                sx = slice(max(0, dx), w + min(0, dx))
                ty = slice(max(0, -dy), h + min(0, -dy))
                tx = slice(max(0, -dx), w + min(0, -dx))
                m = opaque[ty, tx]
                acc[sy, sx] += arr[ty, tx] * m[..., None]
                cnt[sy, sx] += m
        has = cnt > 0
        mean = np.zeros_like(arr)
        mean[has] = acc[has] / cnt[has][:, None]
        edge = (~opaque) & (alpha > 0.02) & has
        arr = arr.copy()
        arr[edge] = mean[edge]

    rgba = np.dstack([arr, alpha * 255.0]).astype("uint8")
    return Image.fromarray(rgba, "RGBA")


def alpha_bbox(im: Image.Image) -> tuple[int, int, int, int]:
    box = im.getchannel("A").getbbox()
    if box is None:
        raise ValueError("整帧透明，检查抠像阈值")
    return box


def place_square(im: Image.Image, size: int, ref_bbox: tuple[int, int, int, int]) -> Image.Image:
    """按固定参考框把人物放进 size² 画布：高度占 FRAME_HEIGHT_FRAC，脚底落在 FRAME_BOTTOM_FRAC。

    9 个 clip 传同一个 ref_bbox（一般取 idle 首帧的人物框），保证切换状态时不跳大小、不跳位置。
    """
    x0, y0, x1, y1 = ref_bbox
    bh = max(1, y1 - y0)
    scale = size * FRAME_HEIGHT_FRAC / bh
    sw, sh = max(1, round(im.width * scale)), max(1, round(im.height * scale))
    scaled = im.resize((sw, sh), Image.LANCZOS)
    ox = size / 2 - (x0 + x1) / 2 * scale
    oy = size * FRAME_BOTTOM_FRAC - y1 * scale
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(scaled, (int(round(ox)), int(round(oy))))
    return canvas


def trim_square(im: Image.Image, size: int) -> Image.Image:
    """居中裁方 → 缩放到 size²（绿幕老路径用；竖构图会被裁头裁脚，蓝幕路径请用 place_square）。"""
    w, h = im.size
    s = min(w, h)
    left, top = (w - s) // 2, (h - s) // 2
    return im.crop((left, top, left + s, top + s)).resize((size, size), Image.LANCZOS)


def check_screen(frames: list[Path], expect: str) -> None:
    """预检幕布色：AI 视频通道会中途把蓝幕改成绿幕（实测 thinking 那段 124 帧里只有首帧是蓝的，
    后面全变成黄绿 #9DBC5D），这种素材抠像后会留下一整块绿底，必须当场拦住而不是悄悄写进 pet-clips。
    """
    if expect == "any":
        return
    kinds = []
    for f in frames[:: max(1, len(frames) // 8)]:
        a = np.asarray(Image.open(f).convert("RGB")).astype(np.float32)
        h, w, _ = a.shape
        c = np.median(np.stack([a[2, 2], a[2, w - 3], a[h - 3, 2], a[h - 3, w - 3]]), axis=0)
        r, g, b = c
        if b > max(r, g) + 30:
            kinds.append("blue")
        elif g > max(r, b) + 30:
            kinds.append("green")
        else:
            kinds.append("other")
    uniq = sorted(set(kinds))
    if uniq != [expect]:
        sys.exit(
            f"幕布色预检失败：期望全段 {expect}，实际抽到 {uniq}（{len(frames)} 帧）。\n"
            f"这多半是模型中途换幕布色，建议重出这段素材，不要硬抠。"
        )
    print(f"幕布色预检通过：全段 {expect}")


def best_loop_end(frames: list[Path], start_frac: float = 0.4) -> tuple[int, float]:
    """找动作回到起始姿态的那一帧，作为循环收尾点。

    比整帧差更可靠：先键控再只比人物 alpha 掩膜的 IoU。背景色在整段里会漂
    （首帧纯蓝、后面变浅），整帧差会被背景主导，量出来的"最像"其实没意义。
    """
    def mask_of(p: Path, w: int = 192):
        thumb = Image.open(p).convert("RGB").resize((w, int(w * 4 / 3)), Image.BILINEAR)
        return np.asarray(key_frame_blue(thumb))[..., 3] > 128

    m0 = mask_of(frames[0])
    start = max(8, int(len(frames) * start_frac))
    best_i, best_iou = len(frames) - 1, 0.0
    for i in range(start, len(frames)):
        mi = mask_of(frames[i])
        inter = int((m0 & mi).sum())
        union = int((m0 | mi).sum()) or 1
        iou = inter / union
        if iou > best_iou:
            best_i, best_iou = i, iou
    return best_i, best_iou


def build(
    video: str | None,
    clip: str,
    fps: int,
    size: int,
    max_frames: int | None,
    dry_run: bool,
    screen: str = "green",
    frames_dir: str | None = None,
    ref_bbox: tuple[int, int, int, int] | None = None,
    loop_end: int | None = None,
    loop_blend: int = 0,
    auto_loop: bool = False,
    expect_screen: str | None = None,
    start_at: int = 0,
    palindrome: bool = False,
) -> None:
    out_dir = CLIPS_DIR / clip / "frames"
    work = REPO / ".scratch" / "xjclip" / clip
    work.mkdir(parents=True, exist_ok=True)

    if frames_dir:
        frames = sorted(Path(frames_dir).glob("*.png"))
        if not frames:
            sys.exit(f"帧目录里没有 png：{frames_dir}")
        print(f"用现成帧 {len(frames)} 张 ← {frames_dir}（跳过拆帧）")
    else:
        if not video:
            sys.exit("要么给 video，要么给 --frames-dir")
        src = Path(video).resolve()
        if not src.exists():
            sys.exit(f"找不到视频：{src}")
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True, exist_ok=True)
        frames = extract_frames(src, work, fps)
        print(f"拆帧 {len(frames)} 张 @ {fps}fps ← {src.name}")
    expect = expect_screen or ("blue" if screen == "blue" else "any")
    check_screen(frames, expect)

    if start_at > 0:
        # 跳掉开头的"入戏"过渡（比如抬手/抬头），让整段都处在动作进行中，
        # 否则结尾的姿态永远对不上开头，循环必然跳。
        frames = frames[start_at:]
        print(f"从第 {start_at} 帧起用（{len(frames)} 帧）")

    # 注意顺序：max_frames 是"要用多长"，必须作用在 start_at 之后，
    # 否则 --start-at 32 --max-frames 50 只会剩 18 帧（踩过一次）
    if max_frames:
        frames = frames[:max_frames]

    if auto_loop:
        j, iou = best_loop_end(frames)
        print(f"自动收尾点：第 {j} 帧（与首帧姿态 IoU={iou:.3f}）")
        if iou < 0.9:
            print(f"  ⚠ 相似度偏低，循环处可能仍有轻微跳动", file=sys.stderr)
        loop_end = j
        loop_blend = loop_blend or 4

    if loop_end is not None:
        # 在动作回到起点姿态的那一帧收尾，否则循环回头会像"顿一下"
        frames = frames[: loop_end + 1]
        print(f"收尾于第 {loop_end} 帧（{len(frames)} 帧 ≈ {len(frames) / fps:.2f}s）")

    def make(f: Path) -> Image.Image:
        raw = Image.open(f).convert("RGB")
        if screen == "blue":
            keyed = key_frame_blue(raw)
            box = ref_bbox or alpha_bbox(keyed)
            return place_square(keyed, size, box)
        return apply_wm_guard(trim_square(key_frame(raw), size))

    if dry_run:
        for f in frames[:3] + frames[-2:]:
            im = make(f)
            probe = work / (f.stem + "_keyed.png")
            im.save(probe)
            print(f"[dry-run] {f.name} → {probe}")
        print("dry-run 完成，未写入 pet-clips")
        return

    if out_dir.exists():
        shutil.rmtree(out_dir)   # 只动本次 clip 的派生帧目录，源视频在 assets/ 不受影响
    out_dir.mkdir(parents=True, exist_ok=True)
    # 流式写盘：只留首帧与尾帧做尾部过渡，避免把整段帧都攒在内存里（实测 86 帧会 OOM 被杀）
    head = tail = None
    n = 0
    for f in frames:
        im = make(f)
        if head is None:
            head = im
        tail = im
        im.save(out_dir / f"{clip}_{n:03d}.webp", "WEBP", quality=WEBP_QUALITY, method=6)
        n += 1
    if palindrome and len(frames) > 2:
        # 正放 + 倒放（去掉末尾那一帧与开头那一帧，避免重复定格）：
        # AI 生成的动作都是「从常态进入某状态」的单向过程，末帧停在动作里，
        # 怎么切都接不回常态；倒放一遍天然回到首帧，接缝为 0。
        # 代价是动作会「回放」，对手势类（抬手→放下、瞪眼→恢复）反而更自然。
        for f in reversed(frames[:-1]):
            make(f).save(out_dir / f"{clip}_{n:03d}.webp", "WEBP", quality=WEBP_QUALITY, method=6)
            n += 1
        print(f"正放+倒放（palindrome）→ 总 {n} 帧")
        loop_blend = 0
    if loop_blend > 0 and head is not None and tail is not None and n > 1:
        # 尾帧向首帧做几帧过渡，把剩余的姿态/亮度差摊掉，循环接缝就看不出来
        for k in range(1, loop_blend + 1):
            Image.blend(tail, head, k / (loop_blend + 1)).save(
                out_dir / f"{clip}_{n + k - 1:03d}.webp", "WEBP", quality=WEBP_QUALITY, method=6
            )
        n += loop_blend
        print(f"尾帧过渡 {loop_blend} 帧 → 总 {n} 帧")
    total = sum(p.stat().st_size for p in out_dir.glob("*.webp"))
    print(f"写出 {n} 帧 → {out_dir.relative_to(REPO)} 共 {total / 1024 / 1024:.2f} MB")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest.setdefault("clips", {}).setdefault(clip, {})
    manifest["clips"][clip].update({"frameCount": n, "loop": True})
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"manifest 更新：{clip}.frameCount = {n}")


def main() -> None:
    ap = argparse.ArgumentParser(description="小桔纯色幕视频 → 透明序列帧")
    ap.add_argument("video", nargs="?", default=None)
    ap.add_argument("clip")
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--size", type=int, default=720)
    ap.add_argument("--max-frames", type=int, default=None)
    ap.add_argument("--screen", choices=["green", "blue"], default="green")
    ap.add_argument("--frames-dir", default=None, help="已经拆好的 PNG 帧目录（跳过 ffmpeg）")
    ap.add_argument("--ref-bbox", default=None,
                    help="构图参考框 x0,y0,x1,y1（源帧坐标）；9 个 clip 传同一个值，缺省按本 clip 首帧自动取")
    ap.add_argument("--loop-end", type=int, default=None,
                    help="在该帧收尾（0 基）。用于切在动作回到起始姿态处，避免循环回头顿一下")
    ap.add_argument("--loop-blend", type=int, default=0,
                    help="末尾追加几帧尾→首的过渡，摊平剩余的姿态/亮度差")
    ap.add_argument("--auto-loop", action="store_true",
                    help="自动找动作回到起始姿态的帧作为收尾点，并默认追加 4 帧过渡")
    ap.add_argument("--expect-screen", choices=["blue", "green", "any"], default=None,
                    help="幕布色预检；缺省在 --screen blue 时要求全段纯蓝")
    ap.add_argument("--start-at", type=int, default=0,
                    help="从第 N 帧起用，跳掉开头抬手/入戏的过渡段")
    ap.add_argument("--palindrome", action="store_true",
                    help="正放+倒放，让单向动作（抬手/瞪眼/抹泪）天然无缝循环")
    ap.add_argument("--dry-run", action="store_true", help="只抠几张看效果，不写 pet-clips")
    a = ap.parse_args()
    bbox = None
    if a.ref_bbox:
        parts = [int(v) for v in a.ref_bbox.split(",")]
        if len(parts) != 4:
            sys.exit("--ref-bbox 需要 x0,y0,x1,y1 四个整数")
        bbox = tuple(parts)
    build(a.video, a.clip, a.fps, a.size, a.max_frames, a.dry_run, a.screen,
          a.frames_dir, bbox, a.loop_end, a.loop_blend, a.auto_loop, a.expect_screen,
          a.start_at, a.palindrome)


if __name__ == "__main__":
    main()
