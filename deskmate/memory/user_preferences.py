"""非常驻偏好层的关键词兜底召回（2026-11-11 C 方案·过渡期）。

背景：USER.md 的偏好被分成两层——
- 「硬规矩」常驻注入（短、硬、违反就出错，见 ``## 硬规矩``）；
- 「偏好与事实」不常驻（见 ``## 偏好与事实``），留待三段向量检索按需取用。

三段检索还没接上。如果这一层既不常驻、又检索不到，过渡期内她等于完全不知道
这些偏好——比之前还差。所以这里提供一条**过渡兜底**：每轮拿当前消息做关键词
打分，从偏好层捞少量最相关的条目临时注入（默认最多 3 条 / 600 字）。

局限（必须说清）：只认词面命中，换个说法就捞不到；这是三段向量检索上线前的
临时桥，不是最终检索能力。
"""

from __future__ import annotations

import re
from pathlib import Path

_PREF_HEAD_RE = re.compile(r"^##\s*(?:偏好与事实|偏好与事实（非常驻[^）]*）)")
_BULLET_PREFIX_RE = re.compile(r"^[-*•·]\s+")
_DEFAULT_LIMIT = 3
_DEFAULT_MAX_CHARS = 600

# 泛词：命中它们不算相关，避免「用户」「萧盛」这类词把整段偏好都捞出来。
_STOPWORDS = {
    "用户", "萧盛", "小桔", "助手", "要求", "希望", "表示", "认为", "自己",
    "这个", "那个", "一下", "可以", "不要", "需要", "已经", "还是", "就是",
}


def _load_preference_lines(base_dir: str | Path) -> list[str]:
    """抽取 USER.md 里 ``## 偏好与事实`` 节的 bullet 条目（去前缀）。"""
    try:
        from .user_profile import load_user

        text = load_user(base_dir) or ""
    except Exception:
        return []
    lines: list[str] = []
    in_sec = False
    for raw in text.splitlines():
        if raw.startswith("## "):
            in_sec = bool(_PREF_HEAD_RE.match(raw))
            continue
        if not in_sec:
            continue
        s = raw.strip()
        if not s or s[0] not in "-*•·":
            continue
        body = _BULLET_PREFIX_RE.sub("", s).strip()
        if body:
            lines.append(body)
    return lines


def _terms(query: str) -> tuple[list[str], set[str]]:
    """切词：完整词（≥2 字）+ 相邻二字组；过滤泛词。"""
    parts = [
        t for t in re.split(r"[\s,，。;；:：/\\|（）()\[\]「」《》\"'“”]+", query or "")
        if len(t) >= 2
    ]
    terms = [t for t in parts if t not in _STOPWORDS]
    q = re.sub(r"\s+", "", query or "")
    grams = {q[i:i + 2] for i in range(len(q) - 1)} - _STOPWORDS if len(q) >= 2 else set()
    return terms, grams


def recall_preferences(
    base_dir: str | Path,
    query: str,
    limit: int = _DEFAULT_LIMIT,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> str:
    """按当前消息关键词，从非常驻偏好层召回少量条目。

    命中口径与 ``DeskMateMemoryManager._search_user_memory`` 一致：完整词命中
    记 3 分，二字组命中记 1 分；至少命中一个完整词，或累计 ≥3 分才算相关。
    无命中/无数据返回空串。fail-open：任何异常都返回空串。
    """
    try:
        if not query or len(query.strip()) < 2:
            return ""
        terms, grams = _terms(query)
        if not terms and not grams:
            return ""
        scored: list[tuple[int, str]] = []
        for line in _load_preference_lines(base_dir):
            score = sum(3 for t in terms if t in line)
            if score == 0 and grams:
                score = sum(1 for g in grams if g in line)
            if score and (any(t in line for t in terms) or score >= 3):
                scored.append((score, line))
        if not scored:
            return ""
        scored.sort(key=lambda x: x[0], reverse=True)
        picked: list[str] = []
        used = 0
        for _score, line in scored:
            if len(picked) >= limit:
                break
            if used + len(line) > max_chars and picked:
                break
            picked.append(line)
            used += len(line)
        if not picked:
            return ""
        body = "\n".join(f"- {x}" for x in picked)
        return "## 可能相关的偏好（按本轮关键词临时召回，未常驻）\n" + body
    except Exception:
        return ""
