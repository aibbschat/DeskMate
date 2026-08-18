"""DeskMate 通用聊天增强：聊天角色 + @召唤协作（不钉死圆桌）。"""
from .mention_router import (
    MentionHit,
    build_handoff_context,
    mention_context,
    parse_mentions,
    unified_roster,
)

__all__ = [
    "MentionHit",
    "build_handoff_context",
    "mention_context",
    "parse_mentions",
    "unified_roster",
]
