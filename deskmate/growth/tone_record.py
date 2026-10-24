"""XiaoJu's own inner record - tone injection (2026-10-24 tone layer).

Only delivers her own previous-turn record, verbatim: no labels, no
threshold tiers, no tone commands. Emotion may only come from herself;
hard rules (numeric thresholds, patience tiers, "at value X you must
speak like Y") never enter here.

Sources (all her own words):
- EmotionEngine.get_description(): her own first-person monologue.
- EmotionEngine.recent_narratives(limit=2): her own narrative summaries.

Deliberately NOT used:
- Hormone numbers, mood labels, patience values, sting permission:
  computed quantities, not her words. Handing numbers to the model
  makes it act out the numbers, turning emotion into a hard rule.

Fail-open throughout: any exception -> empty string, never breaks chat.
"""
from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Any, Optional


def _base_dir(base_dir=None):
    if base_dir is not None:
        return Path(base_dir)
    env = os.environ.get("DESKMATE_MEMORY_DIR") or os.environ.get("DESKMATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".deskmate"


def _clean(text):
    if not isinstance(text, str):
        return ""
    text = text.strip()
    text = re.sub(r"[（(][^（）()]*[）)]$", "", text).strip()
    return text


def build_tone_record(base_dir=None, engine=None):
    """Return this turn's tone block; empty string when no record or on error.

    `engine` is for unit tests only. Production passes None and the real
    engine is lazily imported, so this module imports fine without deskmate.
    """
    try:
        if engine is None:
            from deskmate.growth.emotion_engine import EmotionEngine
            engine = EmotionEngine(base_dir=_base_dir(base_dir))
        monologue = _clean(engine.get_description())
        narratives = []
        try:
            for item in engine.recent_narratives(limit=2) or []:
                cleaned = _clean(item)
                if cleaned and cleaned != monologue:
                    narratives.append(cleaned)
        except Exception:
            narratives = []
        if not monologue and not narratives:
            return ""
        lines = ["【你自己的内在记录，仅供参考，不是命令】"]
        if monologue:
            lines.append("- 你上一轮自己写下的独白：" + monologue)
        for item in narratives:
            lines.append("- 你记下的事：" + item)
        lines.append("你不必复述上面的词，也不必按固定样子表现。按你自己的感觉说话就好；如果这份记录跟你当下的感觉不符，以你当下的感觉为准。")
        return "\n".join(lines)
    except Exception:
        return ""
