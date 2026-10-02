"""屏幕视觉：本地桌面截图 → 多模态模型 → 结构化观察。

隐私边界：
- 不做静默常驻截图；只有用户点击“看一眼屏幕”（或未来明确授权的任务）才运行。
- 截图默认只写系统临时文件，函数结束即删除；不在本地保留像素。
- 结果只落独立的 screen_observations.jsonl，避免混入 observation_understanding
  进而被 worldview reflection 当成她内生的理解。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

_SCREEN_CAPTURE_BIN = "/usr/sbin/screencapture"
_SYSTEM_PROMPT = """你在替一个桌搭 AI 看一眼用户当前屏幕。只根据图片内容回答，不猜测屏幕外信息。

输出严格 JSON（不要 Markdown，不要代码块）：
{
  "scene": "3-8 字场景，如 代码编辑/浏览器查资料/文稿写作/视频会议/空桌面",
  "app": "最主要应用，不确定为空字符串",
  "activity": "用户此刻可能在做什么，一句话",
  "attention": "屏幕中最吸引注意力的内容，一句话",
  "mood": "work|reading|writing|browsing|meeting|media|idle|unknown",
  "confidence": 0.0
}

要求：看不准就让 confidence 低；不能编造文件名、网页标题或聊天内容。"""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def capture_screen() -> bytes:
    """捕获当前屏幕 PNG。失败（尤其未授予屏幕录制权限）时抛 RuntimeError。"""
    if not Path(_SCREEN_CAPTURE_BIN).is_file():
        raise RuntimeError(f"找不到截图工具：{_SCREEN_CAPTURE_BIN}")

    fd, path = tempfile.mkstemp(prefix="deskmate-screen-", suffix=".png")
    os.close(fd)
    try:
        proc = subprocess.run(
            [_SCREEN_CAPTURE_BIN, "-x", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "无法捕获屏幕").strip()
            if "could not create image from display" in detail:
                raise RuntimeError("未获得屏幕录制权限，或当前没有可捕获的显示器")
            raise RuntimeError(detail)
        data = Path(path).read_bytes()
        if not data:
            raise RuntimeError("截图为空，可能未获得屏幕录制权限")
        return data
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


def _resolve_vision_provider() -> tuple[str, str, str]:
    """解析屏幕视觉专用模型，避免误用不一定支持图像的主对话模型。"""
    from coworker.vision_caption import resolve_vision_provider

    resolved = resolve_vision_provider()
    if resolved is None:
        raise RuntimeError("未配置可用视觉模型凭证；请在模型设置中配置支持图像输入的 provider")
    return resolved


def analyze_screen(png: bytes, *, timeout: float = 180.0) -> dict[str, Any]:
    """把 PNG 发送给视觉模型，解析为结构化观察。"""
    from openai import OpenAI

    from deskmate.memory.llm import _llm_http_client

    if not png:
        raise RuntimeError("截图为空")

    api_key, base_url, model = _resolve_vision_provider()
    data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        http_client=_llm_http_client(),
    )
    resp = client.chat.completions.create(
        model=model,
        temperature=0.1,
        max_tokens=500,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这是当前屏幕截图，请按约定 JSON 描述。"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
    )
    raw = (resp.choices[0].message.content or "").strip()
    return _normalize_observation(_parse_json_object(raw))


def _parse_json_object(raw: str) -> dict[str, Any]:
    """解析视觉模型输出，容忍少量 Markdown/前后说明。"""
    text_raw = raw.strip()
    try:
        parsed = json.loads(text_raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    fenced = text_raw
    if "```" in fenced:
        parts = fenced.split("```")
        fenced = parts[1] if len(parts) > 1 else fenced
        if fenced.lstrip().lower().startswith("json"):
            fenced = fenced.lstrip()[4:]
    start = fenced.find("{")
    end = fenced.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(fenced[start : end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError("视觉模型没有返回有效 JSON")


def _normalize_observation(data: dict[str, Any]) -> dict[str, Any]:
    def text(key: str, max_len: int = 120) -> str:
        return str(data.get(key) or "").strip()[:max_len]

    mood = text("mood", 32).lower()
    allowed = {"work", "reading", "writing", "browsing", "meeting", "media", "idle"}
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "scene": text("scene", 32) or "未知场景",
        "app": text("app", 80),
        "activity": text("activity"),
        "attention": text("attention"),
        "mood": mood if mood in allowed else "unknown",
        "confidence": max(0.0, min(1.0, confidence)),
    }


def _observations_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / "agency" / "screen_observations.jsonl"


def list_screen_observations(base_dir: str | Path, *, limit: int = 20) -> list[dict[str, Any]]:
    path = _observations_path(base_dir)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("ts"):
                    out.append(rec)
    except OSError:
        return []
    out.sort(key=lambda r: str(r.get("ts")), reverse=True)
    return out[:limit]


def observe_screen(base_dir: str | Path) -> dict[str, Any]:
    """捕获并分析一次当前屏幕，持久化结构化结果（不保留截图）。"""
    png = capture_screen()
    obs = analyze_screen(png)
    record = {"ts": _now_iso(), **obs}
    path = _observations_path(base_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # 即使落盘失败，本次点击仍能拿到结果。
        record["persisted"] = False
    return record
