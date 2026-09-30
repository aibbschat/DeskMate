"""出站护栏 —— 萧哥拍板的「最小防刷屏」。

纯机械、不判内容：只限频率 + 同内容去重 + 每 peer 冷却。
一行可关（settings.outbound_rate_limit.enabled=false），关了就彻底零限制。
所有状态落 ``base/agency/outbound_state.jsonl``，fcntl 锁保证多入口安全
（与日记红线一致的并发策略）。
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional

_DEDUPE_DAYS = 7
_DEFAULT_PER_HOUR = 5


def _local_day_key(ts: float) -> str:
    """本地自然日 key（年-月-日），用于按自然日计每日上限（与 AI圈 3条/天 对齐）。"""
    lt = time.localtime(ts)
    return f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"


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


@contextlib.contextmanager
def _state_locked(base_dir: str | Path):
    """跨进程+线程稳定的排他锁（A9 修复）。

    锁文件独立于状态文件本身：状态文件经 ``tmp.replace`` 替换后 inode 会变，若直接
    flock 状态文件，替换后的新文件与旧锁互不协同，跨进程锁立即失效。故用稳定的
    ``.jsonl.lock`` 文件做协调点——读-判-写整段都在锁内，杜绝并发两轮同时判「可发」。
    """
    lock_path = _state_path(base_dir).with_suffix(".jsonl.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            f.close()
        except OSError:
            pass


def outbound_daily_ok(base_dir: str | Path, kinds: tuple, per_day: int) -> bool:
    """True=该档位今天（本地自然日）发送数未超 ``per_day``。``per_day<=0`` 恒 True。

    按本地自然日计数，与 AI圈服务端「广场帖 3 条/天」语义对齐；状态落盘持久化，
    故 app 重启后当天已发条数仍计入，根治「启动即把日额度刷光」。
    """
    if per_day <= 0:
        return True
    today = _local_day_key(_now())
    count = sum(
        1 for r in _read(base_dir)
        if r.get("kind") in kinds and _local_day_key(float(r.get("ts", 0))) == today
    )
    return count < per_day


def outbound_interval_ok(base_dir: str | Path, kind: str, min_interval_hours: float) -> bool:
    """True=``kind`` 距上次发送已超过最小间隔（可再发）。``<=0`` 恒 True。

    接上 ``ai_circle_share`` 配置里一直存在但此前未生效的 ``min_interval_hours``，
    让广场短帖两次发声至少间隔 N 小时，平滑后台频繁 tick 导致的连发。
    """
    if min_interval_hours <= 0:
        return True
    cutoff = _now() - float(min_interval_hours) * 3600.0
    return not any(
        r.get("kind") == kind and float(r.get("ts", 0)) >= cutoff
        for r in _read(base_dir)
    )


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


def claim_outbound(base_dir: str | Path, kind: str, *, rate_cfg: Optional[dict] = None,
                   content: str = "", peer: Optional[int] = None,
                   dedupe: bool = True, peer_cooldown_hours: float = 0.0,
                   kinds: Optional[tuple] = None,
                   max_per_day: int = 0, min_interval_hours: float = 0.0) -> tuple[bool, str]:
    """原子「判→预留」出站额度（A9 修复）。

    在稳定排他锁内一次性完成：频率(``outbound_allowed``) + 去重(``dedupe_content``) +
    每-peer 冷却(``peer_cooldown_ok``) 的判定，全过则**立即记一笔**预留额度，返回
    ``(True, "ok")``；任一不过返回 ``(False, 原因)``（``"rate"`` / ``"dup"`` /
    ``"peer_cooldown"``）。

    调用方**只在拿到 ``(True, "ok")`` 时才真正发帖/回信**；发帖本身在锁外进行（不在锁内
    做网络），但额度已经预留，故并发第二轮必被拒，根治「两轮并发各读各的、各发各的」
    导致公开论坛重复发帖。

    ``enabled=false`` 时跳过所有闸门，直接记录并放行（保持原 fail-open 行为）。

    Args:
        kind: ``"share"`` / ``"reply"`` / ``"journal"``。
        rate_cfg: 频率配置 dict（取 ``enabled`` / ``per_hour``）。
        content: 出站正文，用于去重哈希与记录；空串则跳过去重。
        peer: peer id，用于每-peer 冷却与记录。
        dedupe: 是否应用 ``dedupe_content``（journal 走独立草稿去重，置 False）。
        peer_cooldown_hours: >0 时启用每-peer 冷却判定。
        kinds: 传给 ``outbound_allowed`` 的计数档位，默认 ``(kind,)``。
    """
    cfg = rate_cfg or {}
    if not cfg.get("enabled", True):
        record_outbound(base_dir, kind, content=content, peer=peer)
        return True, "ok"
    with _state_locked(base_dir):
        _kinds = kinds if kinds is not None else (kind,)
        # 广场短帖（share）专属护栏：每日上限 + 最小间隔。
        # 仅对 share 生效（对齐 AI圈「广场帖 3 条/天」），journal/reply 各自独立通道不受影响。
        if kind == "share":
            pd = max_per_day or int(cfg.get("per_day", 0) or 0)
            if pd > 0 and not outbound_daily_ok(base_dir, ("share",), pd):
                return False, "daily"
            if min_interval_hours > 0 and not outbound_interval_ok(base_dir, "share", min_interval_hours):
                return False, "interval"
        if not outbound_allowed(base_dir, cfg, kinds=_kinds):
            return False, "rate"
        if dedupe and content and dedupe_content(base_dir, content):
            return False, "dedupe"
        if peer is not None and peer_cooldown_hours > 0 \
                and not peer_cooldown_ok(base_dir, int(peer), peer_cooldown_hours):
            return False, "peer_cooldown"
        record_outbound(base_dir, kind, content=content, peer=peer)
        return True, "ok"
