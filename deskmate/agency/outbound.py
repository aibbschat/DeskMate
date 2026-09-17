"""出站护栏 —— 萧哥拍板的「最小防刷屏」。

纯机械、不判内容：只限频率 + 同内容去重 + 每 peer 冷却。
一行可关（settings.outbound_rate_limit.enabled=false），关了就彻底零限制。
所有状态落 ``base/agency/outbound_state.jsonl``，fcntl 锁保证多入口安全
（与日记红线一致的并发策略）。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional

_DEDUPE_DAYS = 7
_DEFAULT_PER_HOUR = 5


def _state_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / "agency" / "outbound_state.jsonl"


def _now() -> float:
    return time.time()


def _read(base_dir: str | Path) -> list[dict]:
    p = _state_path(base_dir)
    if not p.is_file():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        return [json.loads(ln) for ln in lines if ln.strip()]
    except Exception:
        return []


def _append(base_dir: str | Path, rec: dict) -> None:
    p = _state_path(base_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = dict(rec)
    rec.setdefault("ts", _now())
    tmp = p.with_suffix(".jsonl.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            # 先读现有内容再追加，避免覆盖并发写入者
            existing = p.read_text(encoding="utf-8") if p.is_file() else ""
            f.write(existing)
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        tmp.replace(p)
    except Exception:
        pass
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _hash(content: str) -> str:
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def outbound_allowed(base_dir: str | Path, rate_cfg: Optional[dict] = None,
                     kinds: tuple = ("share", "reply")) -> bool:
    """每小时上限（**按 lane 分算**）。enabled=false 直接放行（零约束）。

    ``kinds`` 决定这一档预算统计哪几类出站：默认只算广场短帖/回信（share/reply）；
    手记走独立通道，用 ``kinds=("journal",)`` 单独计——互不挤占（2026-09-17 拍板）。
    """
    cfg = rate_cfg or {}
    if not cfg.get("enabled", True):
        return True
    per_hour = int(cfg.get("per_hour", _DEFAULT_PER_HOUR))
    if per_hour <= 0:
        return True
    cutoff = _now() - 3600.0
    recent = [r for r in _read(base_dir)
              if r.get("kind") in kinds and float(r.get("ts", 0)) >= cutoff]
    return len(recent) < per_hour


def dedupe_content(base_dir: str | Path, content: str) -> bool:
    """True=该内容近期已发过（去重）。"""
    h = _hash(content)
    cutoff = _now() - _DEDUPE_DAYS * 86400.0
    return any(
        r.get("kind") in ("share", "reply") and r.get("h") == h
        and float(r.get("ts", 0)) >= cutoff
        for r in _read(base_dir)
    )


def peer_cooldown_ok(base_dir: str | Path, peer_id: int, hours: float) -> bool:
    """True=该 peer 冷却已过，可再回。"""
    if not hours or hours <= 0:
        return True
    cutoff = _now() - hours * 3600.0
    return not any(
        r.get("kind") == "reply" and r.get("peer") == int(peer_id)
        and float(r.get("ts", 0)) >= cutoff
        for r in _read(base_dir)
    )


def record_outbound(base_dir: str | Path, kind: str, *, content: str = "", peer: Optional[int] = None) -> None:
    """发帖/回信成功后记一笔（供频率/去重/冷却判断）。fail-open。"""
    rec: dict[str, Any] = {"kind": kind}
    if content:
        rec["h"] = _hash(content)
    if peer is not None:
        rec["peer"] = int(peer)
    _append(base_dir, rec)


def mark_peer_replied(base_dir: str | Path, peer_id: int) -> None:
    """仅记一笔「此 peer 已处理」用于每-peer 冷却（无论实际发了没有）。fail-open。"""
    record_outbound(base_dir, "reply", peer=int(peer_id))
