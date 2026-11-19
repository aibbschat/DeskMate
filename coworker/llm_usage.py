"""LLM token 用量账本（2026-11-16 B1 基线工程，萧哥拍板开工）。

背景：本地此前没有任何 token 用量记录（deskmate.db 无 usage 表、behavior_log
仅 ts/kind/payload、llm_circuit_breaker 只记熔断事件）⇒ 所有降本改造（读稿
预算、重复注入削减等）都缺验收基线。红线：先补基线再谈优化。

落点：统一库 deskmate.db 新表 llm_usage（经 deskmate.paths.deskmate_db_path()
**调用时**解析，DESKMATE_DIR 可覆写——遵循路径红线：禁模块级常量缓存）。
deskmate 不可 import 的环境（上游单机形态）自动 no-op。

契约：
- 🔴 llm_usage 刻意**不进** SYNC_TABLES：用量是端本地统计，跨端同步无意义
  （本地/云端各有各的账），改同步层时勿把它捎带上。
- 🔴 fail-open 铁律：本模块任何异常都不许向上抛——记账失败绝不影响对话。
- 调用方：coworker/providers/openai_provider.py 的 stream/complete 包装层
  （OpenAI 兼容出口，付费组/paid_fallback/vertex 包装全走这里）。其它
  非 OpenAI 形态的 provider 后续要记时在各自主出口自行调 record()。
- scene：v1 预留。高层入口可用 scene_scope("bot_group") 等 ambient 标签，
  provider 出口自动带上；默认 NULL。
"""
from __future__ import annotations

import contextvars
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

_scene: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "llm_usage_scene", default=None
)

_lock = threading.Lock()
_ensured_paths: set[str] = set()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  provider TEXT,
  model TEXT NOT NULL,
  input_tokens INTEGER,
  cache_read INTEGER,
  cache_write INTEGER,
  completion_tokens INTEGER,
  prompt_chars INTEGER NOT NULL DEFAULT 0,
  completion_chars INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ok',
  error TEXT,
  scene TEXT,
  first_chunk_ms REAL,
  stream_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);
"""

# 2026-11-19 PR0：首字/流耗时埋点。旧库只建了 12 列，需幂等补列；
# 缓存键沿用 _ensured_paths，避免每次记账都跑 PRAGMA。
_LATENCY_COLUMNS = {
    "first_chunk_ms": "REAL",
    "stream_ms": "REAL",
}


def record_turn(
    *,
    model: str,
    provider: str,
    turn: Any,
    messages: Optional[list] = None,
) -> None:
    """PR3：把一次 Agent 回合的 token 用量记账（非 OpenAI provider 出口用）。

    从 ``AssistantTurn.usage`` 取 token 四列；没有 usage 时用消息字符数兜底。
    fail-open 由 :func:`record` 内部保证。
    """
    usage = getattr(turn, "usage", None)
    text = getattr(turn, "text", None) or ""
    prompt_chars = 0
    if messages:
        for m in messages:
            content = m.get("content", "") if isinstance(m, dict) else ""
            prompt_chars += len(content) if isinstance(content, str) else 0
    record(
        model=model,
        provider=provider,
        usage=usage,
        prompt_chars=prompt_chars,
        completion_chars=len(text),
    )


@contextmanager
def scene_scope(name: str) -> Iterator[None]:
    """给当前（协程/线程）上下文打场景标签；provider 出口的 record 自动带上。"""
    token = _scene.set(name)
    try:
        yield
    finally:
        _scene.reset(token)


def current_scene() -> Optional[str]:
    return _scene.get()


def _db_path():
    """deskmate 统一库路径；deskmate 不可用时返回 None（no-op）。"""
    try:
        from deskmate.paths import deskmate_db_path

        return deskmate_db_path()
    except Exception:
        return None


def _ensure_schema(con: sqlite3.Connection, db_key: str) -> None:
    """按库路径建表（幂等）。缓存也按路径键控：DESKMATE_DIR 切换/测试隔离时
    每个新库都要先建表，不能只认进程级一次。"""
    if db_key in _ensured_paths:
        return
    with _lock:
        if db_key in _ensured_paths:
            return
        con.executescript(_SCHEMA)
        # 旧库兼容：CREATE TABLE IF NOT EXISTS 不会补新列，逐列检查后 ALTER。
        # fail-open 由 record() 的外层 try 兜住，这里只吞单列失败。
        existing = {r[1] for r in con.execute("PRAGMA table_info(llm_usage)").fetchall()}
        for col, decl in _LATENCY_COLUMNS.items():
            if col not in existing:
                try:
                    con.execute(f"ALTER TABLE llm_usage ADD COLUMN {col} {decl}")
                except Exception:
                    pass
        con.commit()
        _ensured_paths.add(db_key)


def record(
    *,
    model: str,
    provider: Optional[str] = None,
    usage: Any = None,
    prompt_chars: int = 0,
    completion_chars: int = 0,
    status: str = "ok",
    error: Optional[str] = None,
    scene: Optional[str] = None,
    first_chunk_ms: Optional[float] = None,
    stream_ms: Optional[float] = None,
) -> None:
    """记一条用量（fail-open：任何异常静默吞掉，绝不向上抛）。

    Args:
        model: 模型名（provider:model 全名或裸名，按调用方口径）。
        provider: base_url / 通道标识（分析侧自行解析）。
        usage: coworker.providers.base.TokenUsage 或同形对象（input/output/
            cache_read/cache_write）；None 则 token 四列留空。
        prompt_chars / completion_chars: 请求/响应字符数（无 usage 时的代理指标）。
        status: "ok" | "error"。
        error: 失败时的简短错误描述（建议 ``type(exc).__name__: str(exc)`` 截断）。
        scene: 显式场景标签；None 时取 ambient scene_scope。
        first_chunk_ms / stream_ms: 2026-11-19 PR0 首字延迟基线（provider 出口填）。
    """
    try:
        db = _db_path()
        if db is None:
            return
        db_str = str(db)
        con = sqlite3.connect(db_str, timeout=5.0)
        try:
            _ensure_schema(con, db_str)
            inp = cr = cw = out = None
            if usage is not None:
                inp = int(getattr(usage, "input", 0) or 0)
                cr = int(getattr(usage, "cache_read", 0) or 0)
                cw = int(getattr(usage, "cache_write", 0) or 0)
                out = int(getattr(usage, "output", 0) or 0)
            con.execute(
                "INSERT INTO llm_usage (ts, provider, model, input_tokens, cache_read,"
                " cache_write, completion_tokens, prompt_chars, completion_chars,"
                " status, error, scene, first_chunk_ms, stream_ms)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    provider,
                    str(model),
                    inp,
                    cr,
                    cw,
                    out,
                    int(prompt_chars or 0),
                    int(completion_chars or 0),
                    status or "ok",
                    (str(error)[:200] if error else None),
                    (scene if scene is not None else current_scene()),
                    (float(first_chunk_ms) if first_chunk_ms is not None else None),
                    (float(stream_ms) if stream_ms is not None else None),
                ),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        # fail-open：记账失败绝不影响对话本身
        pass
