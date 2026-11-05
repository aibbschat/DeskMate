"""桌宠序列帧素材契约（surfaces/gui/public/pet-clips）。

锁两件事：
1. **manifest 不能骗人**：`frameCount` 必须等于磁盘上真实帧数，且帧名从 000 连续编号。
   XjFramePlayer 按 `frameCount` 拼 URL 逐个预加载，少一张那张就是 404 空帧；
   多几张则永远播不到。这类错不会报错，只会让动作卡一下——必须用测试兜住。
2. **桌宠一律无声**（萧哥 2026-11-05 定）：播放器与宿主组件不得出现音频播放。
   素材是透明 WebP 帧序列，本身就没有音轨；这里防的是以后有人「加个音效更活泼」。

另外顺手校验画布统一为 720² RGBA：9 个 clip 必须同尺寸，否则 XjFramePlayer 里
`object-fit: contain` 会让不同 clip 的人物看起来忽大忽小。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
CLIPS = REPO / "surfaces" / "gui" / "public" / "pet-clips"
MANIFEST = CLIPS / "manifest.json"
GUI_SRC = REPO / "surfaces" / "gui" / "src" / "components" / "deskmate"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _clips_with_assets() -> list[tuple[str, int]]:
    clips = _manifest()["clips"]
    return [(name, meta["frameCount"]) for name, meta in sorted(clips.items()) if meta.get("frameCount")]


def test_manifest_has_at_least_idle():
    """idle 是兜底 clip，缺了桌宠就完全不动。"""
    assert "idle" in _manifest()["clips"]
    assert _manifest()["clips"]["idle"]["frameCount"] > 0


@pytest.mark.parametrize("name,count", _clips_with_assets())
def test_frame_count_matches_files(name: str, count: int):
    frames = sorted((CLIPS / name / "frames").glob("*.webp"))
    assert len(frames) == count, (
        f"{name}: manifest 声明 {count} 帧，磁盘实际 {len(frames)} 张"
    )


@pytest.mark.parametrize("name,count", _clips_with_assets())
def test_frames_are_contiguously_numbered(name: str, count: int):
    frames = sorted((CLIPS / name / "frames").glob("*.webp"))
    expected = [f"{name}_{i:03d}.webp" for i in range(count)]
    assert [f.name for f in frames] == expected, f"{name} 帧编号不连续（播放器按序号拼 URL）"


def test_all_clips_share_canvas_size():
    """9 个 clip 必须同画布：XjFramePlayer 是 contain 缩放，尺寸不同就会跳大小。"""
    sizes = {}
    for name, count in _clips_with_assets():
        probes = {0, count // 2, count - 1}
        frames = sorted((CLIPS / name / "frames").glob("*.webp"))
        for i in probes:
            im = Image.open(frames[i])
            sizes.setdefault(im.size, []).append(f"{name}[{i}]")
    assert len(sizes) == 1, f"画布尺寸不一致：{ {k: v[:3] for k, v in sizes.items()} }"
    (w, h), _ = next(iter(sizes.items()))
    assert (w, h) == (720, 720), f"画布应为 720²，实际 {w}×{h}"


def test_frames_have_transparency():
    """透明通道是抠像产物；全不透明说明键控没生效（会带一整块蓝底）。"""
    for name, count in _clips_with_assets():
        frames = sorted((CLIPS / name / "frames").glob("*.webp"))
        im = Image.open(frames[count // 2])
        assert im.mode == "RGBA", f"{name} 不是 RGBA"
        alpha = im.getchannel("A")
        lo, hi = alpha.getextrema()
        assert lo == 0, f"{name} 帧里没有全透明像素，抠像可能失效"
        assert hi > 200, f"{name} 帧里没有不透明像素"


AUDIO_PATTERN = re.compile(
    r"<audio\b|new\s+Audio\s*\(|\.play\(\)|\.mp3\b|\.wav\b|\.ogg\b|Howl\b|volume\s*[:=]",
    re.IGNORECASE,
)


@pytest.mark.parametrize("filename", ["XjFramePlayer.tsx", "XiaojuMascot.tsx"])
def test_mascot_is_silent(filename: str):
    """桌宠展示一律无声：组件里不得出现任何音频播放路径。"""
    src = (GUI_SRC / filename).read_text(encoding="utf-8")
    # 去掉注释再查，避免「契约里写了不许加 audio」被自己的正则误判
    body = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    body = re.sub(r"^\s*//.*$", "", body, flags=re.M)
    hit = AUDIO_PATTERN.search(body)
    assert hit is None, f"{filename} 出现音频播放：{hit.group(0) if hit else ''}"
