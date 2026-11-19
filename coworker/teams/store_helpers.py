"""TeamStore 行映射/哈希/分页 helper（从 store.py 机械抽取，PR16/#33）。

LIKE 转义 / 规范化哈希 / sqlite Row→dict / 文本分页校验。行为与抽取前一致。
_HASHED_FIELDS 经父模块 store 命名空间解析（_L）；BoardError 直接从 model 导入。
store.py 通过 re-export 保持 from coworker.teams.store import X 不变。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

# _L 懒代理父模块（复检修复）：避免子模块被「首个直接导入」时与父模块 re-export 循环导入。
# 仅在函数调用访问 _L.X 时解析父模块（此时必已完整加载）；monkeypatch(父, X) 经 getattr 实时生效。
class _LazyParent:
    def __getattr__(self, name):
        from coworker.teams import store as _m
        return getattr(_m, name)
_L = _LazyParent()
from coworker.teams.model import BoardError


def _like_escape(text: str) -> str:
    """Quote LIKE metacharacters so a literal string matches only itself. Space
    keys are filesystem paths, where `_` (LIKE's single-character wildcard) is
    ordinary — left raw it also matches a neighbouring space's rows."""
    for char in ("\\", "%", "_"):
        text = text.replace(char, "\\" + char)
    return text


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash(record: dict[str, Any], *, fields: tuple[str, ...] = _L._HASHED_FIELDS) -> str:
    material = _canonical({key: record[key] for key in fields})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["proposal"] = json.loads(item.get("proposal") or "null")
    try:
        item["refs"] = json.loads(item.get("refs") or "[]")
    except json.JSONDecodeError:
        item["refs"] = []
    return item


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    event = dict(row)
    try:
        event["payload"] = json.loads(event.get("payload") or "{}")
    except json.JSONDecodeError:
        event["payload"] = {}
    return event


def validate_text_page(offset: int, max_chars: int) -> None:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise BoardError("offset must be a non-negative integer")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= 16000:
        raise BoardError("max_chars must be between 1 and 16000")


def text_page(text: str, offset: int, max_chars: int) -> dict:
    end = min(len(text), offset + max_chars)
    return {"text": text[offset:end], "offset": offset, "next_offset": end,
            "total_chars": len(text), "has_more": end < len(text)}
