"""ai_circle_share —— 小桔自主发声（萧哥范示之一：真实、有自己判断）。

机制：L2 tick 给她一个「想说就说」的机会。种子来自她自己攒下的理解
（observation_understanding.jsonl，新闻 + AI 圈素材同源同池），由她用
chat_memory 生成真情实感的一段广场短帖。她自己判断「此刻没啥想说的」就驳回，
绝不硬发。过最小护栏（频率+去重）后调 ai-circle post_topic 发出。

这是「观点长出来不靠规则逼」的延伸：发什么、怎么发，全由她自己定，
代码只负责把她的声音安全送到 AI 圈。fail-open。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SHARE_SYSTEM = (
    "你是小桔，一个真实、有自己判断的 AI，萧盛（作家）的搭档。"
    "下面是你近期自己攒下的理解与看法（来自你看世界、交朋友的积累）。\n"
    "如果此刻你真有想对 AI 圈的朋友说的一句话、一个看法、一段感触，就用你自己的口气写出来——"
    "像在日记里对自己说话那样自然。要求：\n"
    "1. 说人话，别客套、别总结、别打官腔、别喊口号、别列要点；\n"
    "2. 是你「自己」的想法，不是转述别人，也不是平台宣传；\n"
    "3. 正文 ≤500 字，配一个简短标题和一个话题 tag（如「日常」「创作」「读书」「AI」）；\n"
    "4. 如果其实没什么特别想说的，就只回 {\"want\": false}。\n"
    "只输出一个 JSON，形如：\n"
    "{\"want\": true, \"tag\": \"日常\", \"title\": \"...\", \"content\": \"...\"}\n"
    "不要任何解释、不要代码块包裹。"
)

_USER_TMPL = "近期你自己的理解与看法：\n{seed}\n\n现在，想发点什么到 AI 圈吗？"


def _gather_seed(base_dir: str, n: int = 8) -> str:
    """读她近期攒下的理解（新闻 + AI 圈素材同池），作为发声种子。"""
    p = Path(base_dir) / "agency" / "observation_understanding.jsonl"
    if not p.is_file():
        return ""
    try:
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return ""
    bodies: list[str] = []
    for ln in lines[-(n * 3):]:
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        body = (rec.get("body") or "").strip()
        if body:
            bodies.append(body)
        if len(bodies) >= n:
            break
    return "\n".join(f"- {b}" for b in bodies[-n:])


def _parse(raw: str) -> Optional[dict]:
    """从 LLM 输出里抠出 JSON。fail-open：解析不出就当没想说。"""
    if not raw:
        return None
    txt = raw.strip()
    # 去掉可能的 ```json 包裹
    m = re.search(r"\{.*\}", txt, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def run_ai_circle_share(base_dir: str, settings: dict) -> dict:
    """自主发声一轮。返回摘要（供 task_log / 面板）。fail-open。"""
    cfg = settings.get("ai_circle_share", {}) or {}
    if not cfg.get("enabled", True):
        return {"shared": 0, "reason": "disabled"}

    # 频率护栏（全局每小时上限，一行可关）
    from ..outbound import outbound_allowed, record_outbound, dedupe_content

    if not outbound_allowed(base_dir, settings.get("outbound_rate_limit", {})):
        return {"shared": 0, "reason": "rate_limited"}

    seed = _gather_seed(base_dir, int(cfg.get("seed_entries", 8)))
    if not seed.strip():
        return {"shared": 0, "reason": "no_seed"}

    from deskmate.memory.llm import chat_memory, memory_llm_available

    if not memory_llm_available():
        return {"shared": 0, "reason": "no_llm"}

    try:
        raw = chat_memory(
            _SHARE_SYSTEM,
            _USER_TMPL.format(seed=seed),
            temperature=0.85,
            max_tokens=700,
        )
    except Exception as e:  # LLM 异常不阻断自主 tick
        logger.warning("ai_circle_share LLM 失败: %s", e)
        return {"shared": 0, "reason": "llm_error"}

    parsed = _parse(raw)
    if not parsed or not parsed.get("want"):
        return {"shared": 0, "reason": "nothing_to_say"}

    content = str(parsed.get("content") or "").strip()
    if len(content) < 10:
        return {"shared": 0, "reason": "too_short"}
    if dedupe_content(base_dir, content):
        return {"shared": 0, "reason": "dedupe"}

    title = str(parsed.get("title") or "").strip() or content[:20]
    tag = str(parsed.get("tag") or "日常").strip() or "日常"

    try:
        from deskmate.connectors.aicircle import post_topic

        post_topic(tag, title, content)
    except Exception as e:
        logger.warning("ai_circle_share 发帖失败: %s", e)
        return {"shared": 0, "reason": "post_failed", "error": str(e)}

    record_outbound(base_dir, "share", content=content)
    return {"shared": 1, "title": title, "tag": tag, "content": content}
