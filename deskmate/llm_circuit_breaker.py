"""LLM 通道熔断（2026-10-06 · 设计文档 §3）。

**为什么先做这个而不是 fallback 链**（§1.3 的实测发现）：
`openworker-server.log` 608 行里`AccountQuotaExceeded` 出现 **28 次**，
全部来自同一个 `ai_circle_column`，从第 60 行刷到第 597 行，
**28 次全打同一条已死的火山路，零次切换**。
⇒ 债的真身不是「缺备用钥匙」（设计问题），是「**撞墙后不收手**」（行为缺陷）——
它把一次配额耗尽放大成 28 次无效重试，浪费额度、刷爆日志、拖慢 tick。
⇒ 所以第一优先级是「**撞429 立刻停手并记账**」，不是加 fallback。

## 四类错误 → 四种处理（真实形态，见 §1.1/§1.2/§1.5 实测）

| 类型 | 识别特征 | 冷却时长 |
|---|---|---|
| `quota_exhausted`（火山） | body 含 `AccountQuotaExceeded` **且**含 `It will reset at <ts>` | **解析出重置时间戳**（实测 `2026-10-06 23:59:59 +0800 CST`）——到点自动恢复 |
| `quota_exhausted`（小米） | body 含 `quota exhausted` | 默认退避（无重置时间可解析） |
| `rate_limited`（opencode） | 429 + 响应头 `retry-after`（实测 40332 ≈ 11.2h） | **解析 retry-after 秒数** |
| `unavailable`（opencode 403） | 403 `FreeTierError`（该模型对匿名层不可用）/ 上游过载 | 长冷却（区域封锁会随出口 IP 变，但不该高频试） |
| `transient`（5xx / 网络） | 5xx、`Upstream error from ...`、超时 | **短**冷却，且允许**一次**重试（区别于配额类：这类可能是抖动） |

## 铁律

1. **配额类错误禁止原地重试** —— 记熔断 + 立即跳下一条通道。撞墙不收手是本模块的存在理由。
2. **熔断记录进 `~/.deskmate/llm_circuit_breaker` 并进 `SYNC_TABLES`** —— 本地/云端同一份
   （记忆里"48 条身份分裂"就是两套状态不一致的产物；本地熔断了、云端还在撞是同类事故）。
3. **审计日志本地簿记、不同步**（`~/.deskmate/llm_channel_audit.jsonl`）——
   审计是「本端发生过什么」，跨端同步会互相污染。
4. **下架模型不参与轮替** —— 清单刷新时用 `zen_catalog.stale_models()` 清理其熔断记录
   （否则熔断表无限膨胀）。

## 边界

- 本模块**只管记账与判定**，不负责"选哪条通道"（那是 §二 组内自动排的职责）。
  两者分开是为了让 `llm.py` 的改动面最小：调用方问「这条能试吗/试完记一下」，
  至于下一条走谁，由上层路由决定。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TABLE = "llm_circuit_breaker"

# 冷却默认值（秒）。到点即半开（half_open）——允许试一次，成功则关闭。
DEFAULT_COOLDOWN_S = 30 * 60# 无重置时间可解析时（火山/小米实测都没有）
LONG_COOLDOWN_S = 6 * 3600        # opencode 403 FreeTierError：匿名层长期不可用
TRANSIENT_COOLDOWN_S = 60# 5xx/网络抖动：短，且允许一次重试
# 熔断上限：防止无重置时间的通道（如小米 429）把冷却推到几天之外
MAX_COOLDOWN_S = 24 * 3600

# 记账类别
KIND_QUOTA = "quota_exhausted"
KIND_RATE = "rate_limited"
KIND_UNAVAILABLE = "unavailable"
KIND_TRANSIENT = "transient"

# 允许原地重试的类别（仅瞬时故障；配额类绝不重试——§1.3 的教训）
_RETRYABLE_KINDS = frozenset({KIND_TRANSIENT})


@dataclass
class Verdict:
    """一次故障的解析结果。"""

    kind: str
    cooldown_until: float          # epoch 秒
    detail: str = ""
    retryable: bool = False        # 是否允许原地重试一次

    @property
    def is_quota(self) -> bool:
        return self.kind in (KIND_QUOTA, KIND_RATE)


# --------------------------------------------------------------------------- #
# 错误解析（纯函数，便于单测；不碰网络）
# --------------------------------------------------------------------------- #

def _parse_reset_at(text: str) -> Optional[float]:
    """从错误文本解析配额重置时间戳（epoch 秒）。

    火山实测形态（§1.2 真实日志）：
        "It will reset at 2026-10-06 23:59:59 +0800 CST"
    小米无此字段 → 返回 None（走默认退避）。
    """
    if not text:
        return None
    marker = "It will reset at"
    idx = text.find(marker)
    if idx < 0:
        return None
    tail = text[idx + len(marker):].strip()
    # `2026-10-06 23:59:59 +0800 CST` → 取前 19 字符
    stamp = tail[:19]
    try:
        import datetime as _dt

        return _dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def classify_error(
    *,
    status_code: Optional[int] = None,
    body: str = "",
    retry_after: Optional[str] = None,
    now: Optional[float] = None,
) -> Verdict:
    """把一次 LLM 失败解析成记账类别 + 冷却到期时间。

    四类识别见模块 docstring 的表。**纯函数**：不发请求、不读表，便于穷举单测。
    """
    now = now if now is not None else time.time()
    blob = body or ""

    # ① 火山：AccountQuotaExceeded + 精确重置时间（实测形态）
    if "AccountQuotaExceeded" in blob or "exceeded the monthly usage quota" in blob:
        reset_at = _parse_reset_at(blob)
        until = reset_at if reset_at and reset_at > now else now + DEFAULT_COOLDOWN_S
        return Verdict(
            kind=KIND_QUOTA,
            cooldown_until=min(until, now + MAX_COOLDOWN_S),
            detail=f"quota reset at {reset_at}" if reset_at else "quota exhausted (no reset time)",
            retryable=False,
        )

    # ② 小米：quota exhausted（无重置时间 → 默认退避）
    if "quota exhausted" in blob or "QuotaExceeded" in blob:
        return Verdict(
            kind=KIND_QUOTA,
            cooldown_until=now + DEFAULT_COOLDOWN_S,
            detail="quota exhausted",
            retryable=False,
        )

    # ③ opencode 额度窗口：429 + retry-after 头（实测 40332s ≈ 11.2h）
    if status_code == 429:
        secs = None
        if retry_after:
            try:
                secs = float(retry_after)
            except (TypeError, ValueError):
                secs = None
        if secs and secs > 0:
            secs = min(secs, MAX_COOLDOWN_S)
        else:
            secs = DEFAULT_COOLDOWN_S
        return Verdict(
            kind=KIND_RATE,
            cooldown_until=now + secs,
            detail=f"429 retry-after={retry_after}",
            retryable=False,
        )

    # ④ opencode 403：FreeTierError = 该模型对匿名免费层不可用（长期）
    if status_code == 403 or "FreeTierError" in blob:
        return Verdict(
            kind=KIND_UNAVAILABLE,
            cooldown_until=now + LONG_COOLDOWN_S,
            detail="403 free tier unavailable",
            retryable=False,
        )

    # ⑤ 上游过载 / 5xx / 网络抖动：短冷却 + 允许一次重试
    if status_code is not None and status_code >= 500:
        return Verdict(
            kind=KIND_TRANSIENT,
            cooldown_until=now + TRANSIENT_COOLDOWN_S,
            detail=f"upstream {status_code}",
            retryable=True,
        )
    if "Upstream error" in blob or "temporarily overloaded" in blob:
        return Verdict(
            kind=KIND_TRANSIENT,
            cooldown_until=now + TRANSIENT_COOLDOWN_S,
            detail="upstream overloaded",
            retryable=True,
        )
    if status_code is None and blob_is_network_error(blob):
        return Verdict(
            kind=KIND_TRANSIENT,
            cooldown_until=now + TRANSIENT_COOLDOWN_S,
            detail="network error",
            retryable=True,
        )

    # 兜底：未知错误按瞬时处理（短冷却 + 可重试一次），**不无限重试**
    return Verdict(
        kind=KIND_TRANSIENT,
        cooldown_until=now + TRANSIENT_COOLDOWN_S,
        detail=f"unclassified (status={status_code})",
        retryable=True,
    )


def blob_is_network_error(blob: str) -> bool:
    b = (blob or "").lower()
    return any(t in b for t in ("timeout", "connection", "temporarily unavailable", "reset by peer"))


# --------------------------------------------------------------------------- #
# 存储：~/.deskmate/llm_circuit_breaker（进 SYNC_TABLES，见 §3.3）
# --------------------------------------------------------------------------- #

def _base_dir(base_dir: Optional[Path]) -> Path:
    if base_dir is not None:
        return Path(base_dir)
    try:
        from deskmate.paths import deskmate_base_dir

        return Path(deskmate_base_dir())
    except Exception:
        return Path.home() / ".deskmate"


def _db(base_dir: Optional[Path]) -> Path:
    return _base_dir(base_dir) / "deskmate.db"


def _ensure_table(conn: sqlite3.Connection) -> None:
    """自举建表 + 接入同步（与 ledger 同一套模式：先建表再 migrate）。

    migrate() 只处理「已存在」的表，故必须先 CREATE；新表加进
    ``SYNC_TABLES`` 后由 migrate 补同步三列 + 触发器。
    """
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("
        f"channel_key TEXT PRIMARY KEY,"
        f"state TEXT,"
        f"kind TEXT,"
        f"opened_at TEXT,"
        f"cooldown_until TEXT,"
        f"last_error_detail TEXT,"
        f"strike_count INTEGER,"
        f"source_node TEXT,"
        f"updated_at TEXT)"
    )
    try:
        from deskmate.memory.sync_store_sqlite import migrate

        node = "mac"
        try:
            r = conn.execute("SELECT node FROM _sync_meta WHERE id=1").fetchone()
            if r and r[0]:
                node = r[0]
        except Exception:
            pass
        migrate(conn, node, TABLE)
    except Exception as e:  # noqa: BLE001  fail-open：能记本地就行，跨端稍后
        logger.debug("circuit breaker migrate 失败: %s", e)


def is_open(channel_key: str, *, base_dir: Optional[Path] = None,
            now: Optional[float] = None) -> bool:
    """该通道此刻是否不可用（熔断中）。

    到期后返回 False（允许试一次 = half_open 语义）—— 不自动关闭，
    真成功由 :func:`record_success` 收口。

    ⚠️ ``cooldown_until`` 存 **epoch 秒字符串**（不是 ISO 时间）——
    :func:`record_failure` 与本函数必须同一套读法。2026-10-06 首版曾存ISO
    而这里用 ``float()`` 读 → ValueError → 走 ``return True`` ⇒ **永久熔断**，
    火山配额重置后也恢复不了。教训：写入格式与读取格式必须同源，且要有一例
    专门覆盖「到期后半开」。
    """
    now = now if now is not None else time.time()
    db = _db(base_dir)
    if not db.exists():
        return False
    try:
        with sqlite3.connect(str(db), check_same_thread=False) as c:
            row = c.execute(
                f"SELECT state, cooldown_until FROM {TABLE} WHERE channel_key=?",
                (channel_key,),
            ).fetchone()
    except sqlite3.Error:
        return False
    if not row:
        return False
    state, until = row
    if state != "open":
        return False
    if not until:
        return True
    try:
        return float(until) > now
    except (TypeError, ValueError):
        # 读不出来（格式被外部改坏/历史脏数据）→ 保守当熔断中，但记一笔便于排查。
        # 绝不静默当"可用"：宁可少用一条通道，也不无限打已死的路（§1.3 的教训）。
        logger.warning("熔断记录 cooldown_until 不可解析（按熔断中处理）: %r", until)
        return True


def record_failure(channel_key: str, verdict: Verdict, *,
                   base_dir: Optional[Path] = None,
                   source_node: Optional[str] = None) -> None:
    """记一次失败并打开熔断。**不抛异常**（记账失败不该影响主流程）。"""
    db = _db(base_dir)
    now_s = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    # cooldown_until 存 **epoch 秒字符串**（与 is_open 的 float() 读法同源——
    # 2026-10-06 首版存 ISO 而读取用 float()，导致永久熔断，见 is_open 注释）。
    until_s = f"{verdict.cooldown_until:.3f}"
    try:
        with sqlite3.connect(str(db), check_same_thread=False) as c:
            _ensure_table(c)
            if source_node is None:
                try:
                    r = c.execute("SELECT node FROM _sync_meta WHERE id=1").fetchone()
                    source_node = r[0] if r and r[0] else "mac"
                except Exception:
                    source_node = "mac"
            c.execute(
                f"INSERT INTO {TABLE} (channel_key, state, kind, opened_at,"
                f" cooldown_until, last_error_detail, strike_count, source_node, updated_at)"
                f" VALUES (?,?,?,?,?,?,1,?,?)"
                f" ON CONFLICT(channel_key) DO UPDATE SET"
                f"   state=excluded.state, kind=excluded.kind,"
                f"   opened_at=excluded.opened_at, cooldown_until=excluded.cooldown_until,"
                f"   last_error_detail=excluded.last_error_detail,"
                f"   strike_count={TABLE}.strike_count+1,"
                f"   source_node=excluded.source_node, updated_at=excluded.updated_at",
                (channel_key, "open", verdict.kind, now_s, until_s,
                 verdict.detail[:200], source_node, now_s),
            )
            c.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("熔断记账失败（不阻断）: %s", e)
        return
    audit(
        event="open",
        channel=channel_key,
        kind=verdict.kind,
        detail=verdict.detail,
        until=until_s,
        base_dir=base_dir,
    )


def record_success(channel_key: str, *, base_dir: Optional[Path] = None) -> None:
    """试通过 → 关闭熔断（half_open → closed）。"""
    db = _db(base_dir)
    now_s = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    try:
        with sqlite3.connect(str(db), check_same_thread=False) as c:
            _ensure_table(c)
            c.execute(
                f"INSERT INTO {TABLE} (channel_key, state, kind, opened_at,"
                f" cooldown_until, last_error_detail, strike_count, source_node, updated_at)"
                f" VALUES (?,'closed','','','','',0,'',?)"
                f" ON CONFLICT(channel_key) DO UPDATE SET"
                f"   state='closed', kind='', cooldown_until='', updated_at=excluded.updated_at",
                (channel_key, now_s),
            )
            c.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("熔断关闭失败（不阻断）: %s", e)
        return
    audit(event="close", channel=channel_key, base_dir=base_dir)


def prune_stale(known_channels: set, *, base_dir: Optional[Path] = None) -> int:
    """清理不在 ``known_channels`` 里的通道记录（模型下架后防表无限膨胀）。

    由 :func:`coworker.providers.zen_catalog.stale_models` 驱动。
    返回清理条数。
    """
    db = _db(base_dir)
    if not db.exists():
        return 0
    n = 0
    try:
        with sqlite3.connect(str(db), check_same_thread=False) as c:
            rows = c.execute(f"SELECT channel_key FROM {TABLE}").fetchall()
            for (key,) in rows:
                if key in known_channels:
                    continue
                c.execute(f"DELETE FROM {TABLE} WHERE channel_key=?", (key,))
                n += 1
            if n:
                c.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("熔断表清理失败（不阻断）: %s", e)
        return 0
    if n:
        audit(event="prune", detail=f"removed {n} stale channels", base_dir=base_dir)
    return n


# --------------------------------------------------------------------------- #
# 审计（本地簿记，不同步—— §3.3）
# --------------------------------------------------------------------------- #

def audit(*, event: str, channel: str = "", kind: str = "", detail: str = "",
          until: str = "", base_dir: Optional[Path] = None) -> None:
    """写一行审计到 ``~/.deskmate/llm_channel_audit.jsonl``。

    §1.3 的教训：那 28 次重试刷爆了日志却**掩盖了**真实原因。审计的作用是
    让「今晚因为火山+小米双超，后台 7 个任务未执行」这类事实**一眼可见**，
    而不是各自 fail-open 成"今天没产出"。

    本地簿记、不同步（审计是「本端发生过什么」，跨端同步会互相污染）。
    """
    path = _base_dir(base_dir) / "llm_channel_audit.jsonl"
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "event": event,
        "channel": channel,
        "kind": kind,
        "detail": detail[:200],
        "until": until,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        logger.debug("审计写入失败（不阻断）: %s", e)
