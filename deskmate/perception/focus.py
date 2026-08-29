"""专注度合成（P1-1）——把「活跃窗口 + 全屏」合成轻量专注度信号。

借鉴 OpenDesktop-Pet「先判适不适合打扰」：专注时只静默记录、不弹建议，
让主动出击「懂时机」。纯规则、零 LLM、fail-open。

- deep：深度工作（编辑器/IDE/终端/写作/演示/全屏）→ 不打扰
- light：内容消费/阅读 → 可轻提醒
- casual：其它常规活跃 → 正常节奏
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class FocusState:
    score: float        # 0-1 专注度
    level: str          # "deep" / "light" / "casual"
    reason: str         # 判定依据（供前端/日志展示）


# 深度工作关键词（应用名 / 窗口标题 / URL，小写匹配）
_DEEP_KEYWORDS = (
    "vscode", "visual studio code", "code", "xcode", "pycharm", "intellij",
    "webstorm", "goland", "sublime", "vim", "neovim", "emacs",
    "terminal", "iterm", "warp", "kitty", "alacritty",
    "typora", "obsidian", "markdown", "marktext", "notion", "logseq",
    "keynote", "powerpoint", "figma", "excel", "numbers", "pages",
    "word", "写作", "稿子", "章节", "剧本", "小说", "代码", "调试", "编译", "schema",
)

# 轻度专注（内容消费 / 阅读）
_LIGHT_KEYWORDS = (
    "safari", "chrome", "edge", "firefox", "arc", "brave", "opera",
    "阅读", "pdf", "preview", "kindle", "微信读书", "抖音", "bilibili",
)


def _fullscreen() -> bool | None:
    """macOS 全屏检测（best-effort；不可用返回 None 不计分）。"""
    try:
        script = (
            'tell application "System Events" to tell (first application process '
            'whose frontmost is true) to get value of attribute "AXFullScreen" '
            "of front window"
        )
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0:
            return None
        return "true" in out.stdout.strip().lower()
    except Exception:
        return None


def get_focus_score(
    app_name: str = "",
    window_title: str = "",
    url: str | None = None,
) -> FocusState:
    """合成当前专注度。任一输入缺失都不抛异常（fail-open → casual 0.5）。"""
    text = f"{app_name} {window_title} {url or ''}".lower()
    score = 0.5
    reasons: list[str] = []

    deep_hit = any(k in text for k in _DEEP_KEYWORDS)
    light_hit = any(k in text for k in _LIGHT_KEYWORDS)

    if deep_hit:
        score += 0.3
        reasons.append("深度工作")
    elif light_hit:
        score -= 0.1
        reasons.append("内容消费/阅读")

    full = _fullscreen()
    if full is True:
        score += 0.2
        reasons.append("全屏")

    score = max(0.0, min(1.0, score))
    if score >= 0.7:
        level = "deep"
    elif score > 0.5:
        level = "light"
    else:
        level = "casual"
    return FocusState(
        score=round(score, 2),
        level=level,
        reason="、".join(reasons) or "常规活跃",
    )
