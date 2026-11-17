#!/usr/bin/env python3
"""回填 emotion_log 的宾语与事件摘要（P0 · 情感层前置）。

对应 reports/2026-11-16_小桔_情感层设计.md §4 R1-b。

三类来源、三种做法：
- ``event_direct``（323 行）：**规则映射，不走 LLM**。事件名只在 event_summary
  里（event_type 被写死成 'experience'），取值有限，直接查表。
- ``echo``（114 行）：``event_summary`` 存的是原 emotion_log.id，复制原行宾语。
- ``chat`` / ``experience``（1,507 行）：LLM 归因，**每批 10 条、max_tokens=1600**。
  整批一个事务：解析出来的条目要么全落库、要么一条不落；模型漏掉的 id 保持
  ``attributed=0``，重跑再取。

红线：
- 只写 object_ref / object_kind / event_summary / attributed，**不碰 deltas、不碰激素**。
- 只选 ``attributed=0 AND source IN (四类)``；time_decay / pending / lapsed /
  landed / temper_foul 永远不进 LLM。
- 指不到具体对象就 none，**不默认萧盛**。
- 回填行标 ``attributed=2``，与在线写入（1）分开，验收计数不会被新行污染。

用法：
    .venv/bin/python scripts/backfill_emotion_objects.py --limit 30   # 先跑 3 批验分布
    .venv/bin/python scripts/backfill_emotion_objects.py              # 全量
    .venv/bin/python scripts/backfill_emotion_objects.py --dry-run    # 只看选行
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskmate.growth.emotion_engine import EmotionEngine, _clean_object  # noqa: E402
from deskmate.memory.llm import (  # noqa: E402
    _call_with_breaker,
    _creds_for,
    llm_available,
)
from deskmate.paths import deskmate_db_path  # noqa: E402

BATCH_SIZE = 10
# 2,500 而非 1,600：flash 偶发 reasoning 拉长，1,600 会整批空返回（实测 2/3 批
# 首跑被吃掉），2,500 留出余量，10 条正文约 600-1,200 tokens，仍装得下。
MAX_TOKENS = 2500
BACKFILLED = 2

# event_direct 的规则映射：实测 event_summary 共 8 种取值。
# relation 只给「我与他的相处」；天气/反思是 none。
_RELATION_SUMMARIES = {
    "他接住了我",
    "他按我说的做了",
    "他否了我递出去的那句",
    "他让我以后别提这个",
}
_RELATION_REF = "和小桔的相处"

_SYSTEM = (
    "你在帮一个角色回看自己的情绪日志，给每条事件标出「这件事关于谁/什么」。\n"
    "只做归因，不评价、不写感受、不补细节。\n"
    "三档判断，按顺序问：\n"
    "1. 这条事件能不能指到一个**具体的人/作品/事物**（萧盛、《夜不收》、某个读者、"
    "某本书、某件具体的东西）？能 → person/work/thing，object_ref 写那个名字。\n"
    "2. 指不到具体对象，但记的是「她和他之间的相处」（被接受、被拒绝、被接住、"
    "被冷落）？→ relation，object_ref 固定写「和小桔的相处」。\n"
    "3. 两条都不成立（天气、流程回执、自己做完一件事、泛泛的心情）→ none，"
    "object_ref 留空。\n"
    "硬规则：\n"
    "- work 只算能叫出具体名字的作品（如《夜不收》），或稿件里具体到某一章、"
    "某个情节、某个人物、某句话；只写「稿子」「章节」「文章」这种类别、说不出"
    "具体是哪本书/哪一章/哪个人物的，判 none——那是类别，不是情感对象。\n"
    "- **不许把泛泛的情绪安到萧盛头上**。只有事件本身真的关于他，才写 person/萧盛。\n"
    "- 审核/退稿/等结果这类**流程**里的角色（主编、总编、审稿人）不是情感对象，落 none；"
    "只有事件本身在说这个人，才写 person。\n"
    "- object_ref 必须来自事件文本，不许推断、不许编。\n"
    "- event_summary 不超过 40 字，只写发生了什么；若原文只有她的感受、看不出"
    "发生了什么，摘要留空字符串，**不要编**。\n"
    "- 摘要用**她的口吻**：对话另一人写「他」或直接写名字，**不许写「用户」「对方」"
    "「该用户」这类旁观的第三人称**。\n"
    "严格输出 JSON 数组，每项 {\"id\", \"object_ref\", \"object_kind\", \"event_summary\"}，"
    "id 必须原样回填，不要多不要少。"
)


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db), timeout=15, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _parse_batch(raw: str) -> list[dict] | None:
    """从模型输出里取出 JSON 数组；格式不对 → None（整批放弃）。"""
    if not raw:
        return None
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    lo, hi = text.find("["), text.rfind("]")
    if lo < 0 or hi <= lo:
        return None
    try:
        data = json.loads(text[lo:hi + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    return [d for d in data if isinstance(d, dict)]


def _backfill_event_direct(conn: sqlite3.Connection, dry: bool) -> int:
    rows = conn.execute(
        "SELECT id, event_summary FROM emotion_log"
        " WHERE attributed=0 AND source='event_direct'"
    ).fetchall()
    n = 0
    for rid, summary in rows:
        if (summary or "").strip() in _RELATION_SUMMARIES:
            ref, kind = _RELATION_REF, "relation"
        else:
            ref, kind = "", "none"
        if not dry:
            conn.execute(
                "UPDATE emotion_log SET object_ref=?, object_kind=?, attributed=?"
                " WHERE id=?",
                (ref, kind, BACKFILLED, rid),
            )
        n += 1
    return n


def _backfill_echo(conn: sqlite3.Connection, dry: bool) -> int:
    rows = conn.execute(
        "SELECT id, event_summary FROM emotion_log"
        " WHERE attributed=0 AND source='echo'"
    ).fetchall()
    n = 0
    for rid, origin_id in rows:
        origin = conn.execute(
            "SELECT object_ref, object_kind FROM emotion_log WHERE id=?",
            (origin_id,),
        ).fetchone() if origin_id else None
        ref, kind = (origin[0] or "", origin[1] or "none") if origin else ("", "none")
        if not dry:
            conn.execute(
                "UPDATE emotion_log SET object_ref=?, object_kind=?, attributed=?"
                " WHERE id=?",
                (ref, kind, BACKFILLED, rid),
            )
        n += 1
    return n


def _llm_complete(system: str, user: str, timeout: float) -> str:
    """归因专用调用：逐条付费通道试，**空返回也算失败换道**。

    实测小米 mimo-v2.6-pro 在长提示下会间歇性只回推理、content 为空
    （同提示 4 次里 2 次空），而空串不会抛异常、不会触发熔断，必须显式跳过。
    """
    from openai import OpenAI

    from deskmate.llm_channels import paid_channels
    from deskmate.memory.llm import _llm_http_client

    def _one(ak: str, bu: str, md: str) -> str:
        client = OpenAI(api_key=ak, base_url=bu, timeout=timeout,
                        http_client=_llm_http_client())
        resp = client.chat.completions.create(
            model=md,
            temperature=0.2,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (resp.choices[0].message.content or "").strip()

    # 首选 mimo-v2.6-flash：pro 是重推理模型，实测 10 条/批会把 1,600 tokens
    # 全花在 reasoning 上、content 为空（reasoning_tokens=1,600）；flash 只需
    # 约 400 reasoning tokens，10 条约 27s 就回完整 JSON。
    # 备选 ark deepseek-v4-flash；pro / 腾讯只作最后兜底（前者常空、后者 403）。
    candidates = [("xiaomi", "mimo-v2.6-flash"), ("ark-agent-plan-cn", "deepseek-v4-flash")]
    for ch in paid_channels():
        if (ch.provider, ch.model) not in candidates:
            candidates.append((ch.provider, ch.model))

    for provider, model in candidates:
        ak, bu, md = _creds_for(provider, model)
        if not ak:
            continue
        try:
            out = _call_with_breaker(
                provider, model, lambda a=ak, b=bu, m=md: _one(a, b, m)
            )
        except Exception:  # noqa: BLE001 换下一条通道
            continue
        if out:
            return out
        print(f"  通道 {provider}:{model} 返回空，换下一条")
    return ""


def _run_batch(db: Path, batch: list, idx: int, timeout: float) -> tuple[int, int]:
    """跑一批归因并落库。返回 (写入条数, 失败批数)。

    整批一个事务：要么这批解析出的条目全部落库，要么一条不落（进程中断/DB 错
    回滚，不会留下写了一半的批）。模型漏掉的 id 保持 attributed=0，重跑会再取。
    """
    lines = [
        f"- id={rid} | {(ts or '')[:10]} | {source} | {(desc or '')[:200]}"
        for rid, ts, source, desc in batch
    ]
    prompt = "逐条归因：\n" + "\n".join(lines)
    parsed = None
    t0 = time.time()
    for _attempt in (1, 2):
        try:
            raw = _llm_complete(_SYSTEM, prompt, timeout)
        except Exception as e:  # noqa: BLE001 网络/额度失败：整批跳过
            print(f"  batch {idx} 调用失败: {type(e).__name__}")
            continue
        parsed = _parse_batch(raw)
        if parsed is not None:
            break
    if parsed is None:
        print(f"  batch {idx} 解析失败，整批跳过")
        return 0, 1

    by_id = {str(d.get("id") or "").strip(): d for d in parsed}
    wrote = 0
    conn = _connect(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for rid, _ts, _source, _desc in batch:
            item = by_id.get(rid)
            if item is None:
                continue
            ref, kind = _clean_object(item.get("object_ref"), item.get("object_kind"))
            summary = str(item.get("event_summary") or "").strip()[:80]
            conn.execute(
                "UPDATE emotion_log SET object_ref=?, object_kind=?, event_summary=?,"
                " attributed=? WHERE id=?",
                (ref, kind, summary, BACKFILLED, rid),
            )
            wrote += 1
        conn.execute("COMMIT")
    except Exception:  # noqa: BLE001 写库出错：整批回滚，不留半批
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        conn.close()
        print(f"  batch {idx} 写库失败，整批回滚")
        return 0, 1
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    print(f"  batch {idx}: {wrote}/{len(batch)} 条（{time.time() - t0:.1f}s）")
    if wrote != len(batch):
        return wrote, 1
    return wrote, 0


def _backfill_llm(db: Path, limit: int | None, dry: bool,
                  timeout: float = 90.0, max_fail: int = 5,
                  workers: int = 1) -> tuple[int, int]:
    """chat / experience 走 LLM。返回 (成功条数, 失败批数)。"""
    conn = _connect(db)
    try:
        rows = conn.execute(
            "SELECT id, ts, source, description FROM emotion_log"
            " WHERE attributed=0 AND source IN ('chat','experience')"
            " ORDER BY ts"
        ).fetchall()
    finally:
        conn.close()
    if limit:
        rows = rows[:limit]
    batches = [rows[i:i + BATCH_SIZE] for i in range(0, len(rows), BATCH_SIZE)]
    if dry:
        for i in range(len(batches)):
            print(f"[dry] batch {i + 1}: {len(batches[i])} 条")
        return 0, 0
    if not batches:
        return 0, 0

    done = failed = 0
    if workers <= 1:
        for i, batch in enumerate(batches, 1):
            w, f = _run_batch(db, batch, i, timeout)
            done += w
            failed += f
            if failed >= max_fail:
                print(f"  累计失败 {failed} 批，中止（已写的不回滚，重跑幂等）")
                break
        return done, failed

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_run_batch, db, batch, i, timeout): i
            for i, batch in enumerate(batches, 1)
        }
        for fut in as_completed(futures):
            try:
                w, f = fut.result()
            except Exception as e:  # noqa: BLE001 单批异常不拖垮全量
                print(f"  batch {futures[fut]} 异常: {type(e).__name__}: {e}")
                w, f = 0, 1
            done += w
            failed += f
            if failed >= max_fail:
                for pending in futures:
                    pending.cancel()
                print(f"  累计失败 {failed} 批，中止（已写的不回滚，重跑幂等）")
                break
    return done, failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 条 chat/experience（验分布）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=float, default=90.0, help="单次 LLM 调用超时（秒）")
    ap.add_argument("--workers", type=int, default=2, help="并发批数（默认 2）")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    db = Path(args.db) if args.db else Path(deskmate_db_path())
    if not db.exists():
        print(f"数据库不存在: {db}")
        return 1
    # 幂等迁移（三列 + 两索引）由 _ensure_schema 兜底，重复执行安全。
    EmotionEngine(base_dir=db.parent)

    if not args.dry_run and not llm_available():
        print("LLM 不可用（付费 provider 未配置）：仅能跑 event_direct/echo 规则映射。")
        return 2

    t0 = time.time()
    with _connect(db) as conn:
        print("== event_direct 规则映射 ==")
        n_direct = _backfill_event_direct(conn, args.dry_run)
        print(f"   {n_direct} 条")
        print("== chat/experience LLM 归因 ==")
        n_llm, failed = _backfill_llm(db, args.limit, args.dry_run,
                                      timeout=args.timeout, workers=args.workers)
        print(f"   {n_llm} 条，失败批 {failed}")
        print("== echo 继承原事件 ==")
        n_echo = _backfill_echo(conn, args.dry_run)
        print(f"   {n_echo} 条")

    elapsed = time.time() - t0
    total = n_direct + n_llm + n_echo
    print(f"\n完成：{total} 条（event_direct {n_direct} / llm {n_llm} / echo {n_echo}），"
          f"耗时 {elapsed:.1f}s")
    if not args.dry_run:
        with _connect(db) as conn:
            print("attributed 分布:", conn.execute(
                "SELECT attributed, COUNT(*) FROM emotion_log GROUP BY attributed"
            ).fetchall())
            print("object_kind 分布:", conn.execute(
                "SELECT object_kind, COUNT(*) FROM emotion_log"
                " WHERE attributed=2 GROUP BY object_kind ORDER BY 2 DESC"
            ).fetchall())
            print("top 对象:", conn.execute(
                "SELECT object_kind, object_ref, COUNT(*) FROM emotion_log"
                " WHERE attributed=2 AND object_ref!=''"
                " GROUP BY object_kind, object_ref ORDER BY 3 DESC LIMIT 10"
            ).fetchall())
            if args.limit is None:
                # 审计落点（设计稿 R1-b）：只有全量跑完才记，避免 --limit 的部分数
                # 被当成覆盖率。
                stats = conn.execute(
                    "SELECT COUNT(*),"
                    " SUM(object_kind='none'), SUM(object_kind='relation')"
                    " FROM emotion_log WHERE attributed=2"
                ).fetchone()
                # 分来源计数从库里取，不用本轮增量——回填可分段续跑，
                # 最后一轮只补几十条，拿它当总账会把 1,507 写成 21。
                by_source = dict(conn.execute(
                    "SELECT source, COUNT(*) FROM emotion_log"
                    " WHERE attributed=2 GROUP BY source"
                ).fetchall())
                blob = {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "total": stats[0] or 0,
                    "none": stats[1] or 0,
                    "relation": stats[2] or 0,
                    "llm": by_source.get("chat", 0) + by_source.get("experience", 0),
                    "event_direct": by_source.get("event_direct", 0),
                    "echo": by_source.get("echo", 0),
                    "elapsed_s": round(elapsed, 1),
                }
                conn.execute(
                    "INSERT INTO json_blobs (key, data, updated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET data=excluded.data,"
                    " updated_at=excluded.updated_at",
                    ("emotion_backfill_done",
                     json.dumps(blob, ensure_ascii=False), blob["ts"]),
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
