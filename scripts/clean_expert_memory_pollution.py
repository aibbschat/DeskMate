"""清理专家/工人 1v1 会话对小桔记忆的污染（默认 dry-run，--apply 才动）。

2026-11-09 事故：build_engine 对**所有** agent 都挂了 post_turn_hook +
memory_store + context_provider，专家 1v1 的问答被当成「小桔与萧盛的对话」：
  - 会话转储写进 ~/.deskmate/sessions/*.md
  - post_session_summary 写 xiaosheng_observations / USER.md / MEMORY.md /
    vault/preferences / diary/materials / json_blobs.growth
  - evolve_skill_on_success 用专家问句当 reason 进化小桔技能
  - 剪贴板观察把这些内容当成萧哥对小桔说的话，生成 self_expression /
    emotional_care 建议并 delivered（弹给用户），落进 proactive_ledger、
    _sync_oplog、behavior_log、disturb_state

第一版脚本只清了 sessions/observations/USER/MEMORY 六处，且用 write_text
非原子写。本版按「集合级取证」补齐全部**活数据**落点：只精删可证伪的条目，
不动合法长期记忆（如 8-30 的「表达的观点 > 故事好看 > 史料真实」）、不动
coworker.db 里的专家对话记录本身（那是萧哥的工作资产）、不动技能文件里
内容合理的规则。

安全措施：
  - 先整库 sqlite3 .backup + 每个被改文件 before 副本 + 被删行明细，全部进
    ~/.deskmate/_quarantine/expert_memory_<ts>/
  - 文本文件一律临时文件 + os.replace 原子写
  - DB 改动走单个 BEGIN IMMEDIATE 事务；proactive_ledger 用同步墓碑软删
    （sync_tombstone=1）而不是物理删，_sync_oplog 只脱敏文本字段、保留序列
  - --apply 结束自带指纹复扫，报告剩余命中
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime

HOME = pathlib.Path.home()
COMPANION = {"deskmate", "cowork"}
COWORKER_DB = HOME / ".config/coworker/coworker.db"
DESKMATE_DB = HOME / ".deskmate/deskmate.db"
SESS = HOME / ".deskmate/sessions"
OBS = HOME / ".deskmate/agency/xiaosheng_observations.jsonl"
USER = HOME / ".deskmate/USER.md"
MEM = HOME / ".deskmate/MEMORY.md"
PREF_DIR = HOME / ".deskmate/vault/preferences"
DIARY_DIR = HOME / ".deskmate/diary/materials"
DISTURB = HOME / ".deskmate/memory/disturb_state.json"
BEHAVIOR = HOME / ".deskmate/behavior_log.jsonl"
INJECT = HOME / ".deskmate/traces/inject.jsonl"

# 事故窗口：15:37 专家会话落盘之后。用于限定 ledger / 日志类的清理范围，
# 避免误伤历史里恰好含相同字句的合法记录。
ACCIDENT_SINCE = "2026-11-09T15:37"
CLIPBOARD_Q = "你知道我是谁、写过哪些书吗"
EXPERT_OPENING = "我知道。你是萧盛"
EXTRA_MD = {"1794128260978114000.md"}
EXTRA_OBS_PREFIX = "你不要读正文以外的其他东西"

# post_session_summary 从两次专家 1v1 抽出来的「事实」原文。按整条文本精确匹配，
# 不用宽松关键词——「表达的观点 > 故事好看 > 史料真实」在 8-30 的合法长期记忆里也有。
POLLUTED_FACTS = (
    "2026-11-09: 萧盛自称“写手”不称“作家”，写东西的顺序是表达的观点 > 故事好看 > 史料真实，坚持“大事不虚、小事不拘”。",
    "2026-11-09: 萧盛最想要的是影视改编，这是小桔协助她工作的主要方向。",
    "2026-11-09: 萧盛名下还有几个书名（衙探、大明提塘官、京报笔谈：锦州之围、明朝简史、朕本凡人，奈何天子、明朝的裂变），出版状态未核。",
    "2026-11-09: 萧盛未写完的《国子监秘闻录》是北宋元祐背景，开封各衙门散着写，正二开，停在刘成业案开线处。",
    "2026-11-08: 萧盛要求小桔只读正文、以正文为准，不读正文以外的内容，只提供脑洞和思路。",
    "2026-11-08: 萧盛的写作规矩是代价需落到具体的人身上。",
)
EXPERT_USER_MSGS = (
    CLIPBOARD_Q,
    "你不要读正文以外的其他东西，以正文为准，给我脑洞和思路就行了",
)

# 复扫指纹：清完后活数据里不应再出现。注意不含「表达的观点 > …」——那是合法记忆。
SCAN_NEEDLES = (
    "1794209855592460000", "1794128260978114000",
    "萧盛最想要的是影视改编", "萧盛名下还有几个书名",
    "《国子监秘闻录》是北宋元祐", CLIPBOARD_Q,
    "萧盛要求小桔只读正文", "你不要读正文以外的其他东西",
)
# 10-11 的合法老建议：萧哥当时也问过同一句，只是巧合命中指纹，复扫时排除。
LEGACY_LEDGER_IDS = ("cloud_audit:self_expression:care",)

# 明确不清理的合法/审计落点，复扫时单列说明
KNOWN_KEEP = (
    "~/.config/coworker/coworker.db（专家对话记录本身）",
    "~/.deskmate/cognitive_profile.json / vault/小桔眼中的萧盛.md / vault/life/05-信念与底线.md（8-30 合法长期记忆）",
    "~/.deskmate/skills/writing/*.md（11-08 技能进化写入的『只读正文』规则，内容合理）",
    "deskmate.db:proactive_ledger 的 2026-10-11 snoozed 建议（同问句巧合，非本次污染）",
)


def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def read_lines(p: pathlib.Path) -> list[str]:
    return p.read_text(encoding="utf-8", errors="ignore").splitlines() if p.exists() else []


def atomic_write(p: pathlib.Path, text: str) -> None:
    """临时文件 + os.replace（红线：持久化必须原子写）。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".clean_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def is_fact_polluted(s: str) -> bool:
    return any(f in s for f in POLLUTED_FACTS)


def is_reason_polluted(s: str) -> bool:
    return any(m in s for m in EXPERT_USER_MSGS)


def collect():
    """只读取证，返回所有待清项。"""
    con = sqlite3.connect(f"file:{COWORKER_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    pairs, sessions = set(), []
    for row in con.execute("select session_id, agent, title, updated_at from sessions"):
        if row["agent"] in COMPANION or str(row["session_id"]).startswith("__run__"):
            continue
        sessions.append(dict(row))
        conv = HOME / ".config/coworker/conversations" / (str(row["session_id"]) + ".jsonl")
        if not conv.exists():
            continue
        user = None
        for ln in read_lines(conv):
            try:
                m = json.loads(ln)
            except Exception:
                continue
            role, c = m.get("role"), norm(m.get("content"))
            if role == "user" and c:
                user = c
            elif role == "assistant" and c and user:
                pairs.add((user[:80], c[:80]))
                user = None
    con.close()

    # 1) sessions/*.md —— 专家会话被当成小桔会话落盘
    md_hits = []
    for p in SESS.glob("*.md"):
        text = p.read_text(encoding="utf-8", errors="ignore")
        mu = re.search(r"## User\s*\n(.+?)(?:\n\s*## Assistant|\Z)", text, re.S)
        ma = re.search(r"## Assistant\s*\n(.+)", text, re.S)
        if not (mu and ma):
            continue
        u, a = norm(mu.group(1)), norm(ma.group(1))
        if (u[:80], a[:80]) in pairs or p.name in EXTRA_MD:
            md_hits.append(p)
    session_ids = {p.stem for p in md_hits}

    # 2) xiaosheng_observations —— 误记为「（小桔本轮回应）」
    obs_lines = read_lines(OBS)
    obs_hits = []
    for ln in obs_lines:
        try:
            d = json.loads(ln)
        except Exception:
            continue
        body = norm(d.get("body"))
        if "（小桔本轮回应）" not in body:
            continue
        head, tail = body.split("（小桔本轮回应）", 1)
        if (head.strip()[:80], tail.strip()[:80]) in pairs or head.strip().startswith(EXTRA_OBS_PREFIX):
            obs_hits.append(ln)

    # 3) USER.md / MEMORY.md —— 按整行精确匹配事实原文（含 11-08 两行）
    user_lines, mem_lines = read_lines(USER), read_lines(MEM)
    user_drop = [(i, l) for i, l in enumerate(user_lines) if is_fact_polluted(l)]
    mem_drop = [(i, l) for i, l in enumerate(mem_lines) if is_fact_polluted(l)]

    # 4) vault/preferences —— frontmatter 的 source 指向专家 session
    pref_hits = []
    if PREF_DIR.exists():
        for p in PREF_DIR.glob("*.md"):
            t = p.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"^source:\s*session-(\d+)", t, re.M)
            if m and m.group(1) in session_ids:
                pref_hits.append(p)

    # 5) diary/materials —— memory_learned.texts / skill_evolved.reason 含污染
    diary_hits: dict[pathlib.Path, list[str]] = {}
    for p in (DIARY_DIR / "2026-11-08.jsonl", DIARY_DIR / "2026-11-09.jsonl"):
        if not p.exists():
            continue
        hits = []
        for ln in read_lines(p):
            try:
                d = json.loads(ln)
            except Exception:
                continue
            texts = " ".join(str(x) for x in (d.get("texts") or []))
            if is_fact_polluted(texts) or is_reason_polluted(str(d.get("reason") or "")):
                hits.append(ln)
        if hits:
            diary_hits[p] = hits

    # 6) deskmate.db —— growth blob / proactive_ledger / _sync_oplog
    db = sqlite3.connect(f"file:{DESKMATE_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    growth = json.loads(db.execute("select data from json_blobs where key='growth'").fetchone()[0])
    growth_drop = []
    for ev in growth.get("recent_events", []):
        texts = " ".join(str(x) for x in (ev.get("texts") or []))
        if is_fact_polluted(texts) or is_reason_polluted(str(ev.get("reason") or "")):
            growth_drop.append(ev)
    ledger_rows = []
    for r in db.execute(
        "select id, kind, status, created_at, content, evidence from proactive_ledger "
        "where created_at >= ? order by created_at", (ACCIDENT_SINCE,)
    ):
        blob = f"{r['content'] or ''} {r['evidence'] or ''}"
        if CLIPBOARD_Q in blob or EXPERT_OPENING in blob or "剪贴板里那句『你知道我是谁" in blob:
            ledger_rows.append(dict(r))
    # 不按 ledger id 过滤：oplog 里还有几条「记录已不在 ledger、但 payload 仍带
    # 污染文本」的历史 upsert，必须一并脱敏。
    # 例外：cloud_audit:self_expression:care 是 2026-10-11 的合法老建议（萧哥当时
    # 也问过同一句），只是 11-09 被 snooze 了一次；它的 payload 不是污染，不能动。
    oplog_rows = []
    for r in db.execute(
        "select seq, record_id, payload_json from _sync_oplog "
        "where table_name='proactive_ledger' and op='upsert'"
    ):
        if str(r["record_id"]).endswith("cloud_audit:self_expression:care"):
            continue
        pj = r["payload_json"] or ""
        if CLIPBOARD_Q in pj or EXPERT_OPENING in pj:
            oplog_rows.append(dict(r))
    db.close()

    # 7) 主动行为的浮动层状态与日志
    disturb_hits = []
    if DISTURB.exists():
        state = json.loads(DISTURB.read_text(encoding="utf-8", errors="ignore"))
        for i, rec in enumerate(state.get("records", [])):
            key = str(rec.get("suggestion_key") or "")
            if CLIPBOARD_Q in key or EXPERT_OPENING in key:
                disturb_hits.append(i)
    behavior_hits = [ln for ln in read_lines(BEHAVIOR) if CLIPBOARD_Q in ln]
    inject_hits = [ln for ln in read_lines(INJECT) if CLIPBOARD_Q in ln]

    return dict(
        pairs=pairs, sessions=sessions, md_hits=md_hits, obs_lines=obs_lines, obs_hits=obs_hits,
        user_lines=user_lines, user_drop=user_drop, mem_lines=mem_lines, mem_drop=mem_drop,
        pref_hits=pref_hits, diary_hits=diary_hits, growth=growth, growth_drop=growth_drop,
        ledger_rows=ledger_rows, oplog_rows=oplog_rows, disturb_hits=disturb_hits,
        behavior_hits=behavior_hits, inject_hits=inject_hits,
    )


def report(c: dict) -> None:
    print(f"非 companion session: {len(c['sessions'])}；可用 (user,assistant) 对: {len(c['pairs'])}")
    print(f"\n[1] sessions/*.md 命中 {len(c['md_hits'])}")
    for p in c["md_hits"]:
        print("   -", p.name)
    print(f"[2] xiaosheng_observations 命中 {len(c['obs_hits'])} 行")
    print(f"[3] USER.md {len(c['user_drop'])} 行 / MEMORY.md {len(c['mem_drop'])} 行")
    for _, l in c["user_drop"] + c["mem_drop"]:
        print("   -", l.strip()[:110])
    print(f"[4] vault/preferences 命中 {len(c['pref_hits'])} 个文件")
    for p in c["pref_hits"]:
        print("   -", p.name)
    print(f"[5] diary/materials 命中 {sum(len(v) for v in c['diary_hits'].values())} 行")
    for p, hits in c["diary_hits"].items():
        print(f"   - {p.name}: {len(hits)} 行")
    print(f"[6] deskmate.db: growth.recent_events {len(c['growth_drop'])} 条 / "
          f"proactive_ledger {len(c['ledger_rows'])} 条 / _sync_oplog {len(c['oplog_rows'])} 条")
    for r in c["ledger_rows"]:
        print(f"   - {str(r['created_at'])[:19]} {r['kind']:16s} {r['id'][:66]}")
    print(f"[7] disturb_state {len(c['disturb_hits'])} 条 / behavior_log "
          f"{len(c['behavior_hits'])} 行 / traces/inject {len(c['inject_hits'])} 行")


def apply(c: dict) -> pathlib.Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    q = HOME / ".deskmate" / "_quarantine" / f"expert_memory_{stamp}"
    (q / "files").mkdir(parents=True, exist_ok=True)
    (q / "removed").mkdir(parents=True, exist_ok=True)
    manifest = {"stamp": stamp, "moved": [], "edited": [], "removed_lines": {}, "db": {}}

    def backup(p: pathlib.Path) -> None:
        rel = str(p.relative_to(HOME)).replace("/", "__")
        shutil.copy2(p, q / "files" / (rel + ".before"))
        manifest["edited"].append(str(p))

    # 整库在线备份（含 WAL 中的最新数据），红线要求副本迁移用 sqlite3 .backup
    src = sqlite3.connect(f"file:{DESKMATE_DB}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(str(q / "deskmate.db.before"))
    src.backup(dst)
    dst.close()
    src.close()

    # 1) 会话转储：移进隔离区（可回退）
    (q / "sessions").mkdir(parents=True, exist_ok=True)
    for p in c["md_hits"]:
        shutil.move(str(p), str(q / "sessions" / p.name))
        manifest["moved"].append(str(p))

    # 2) observations：删行，原子写
    if c["obs_hits"]:
        backup(OBS)
        hit = set(c["obs_hits"])
        kept = [ln for ln in c["obs_lines"] if ln not in hit]
        atomic_write(OBS, "\n".join(kept) + ("\n" if kept else ""))
        (q / "removed" / "xiaosheng_observations.jsonl").write_text("\n".join(c["obs_hits"]) + "\n", encoding="utf-8")
        manifest["removed_lines"]["observations"] = len(c["obs_hits"])

    # 3) USER / MEMORY：按行删，原子写
    for path, lines, drops, label in (
        (USER, c["user_lines"], c["user_drop"], "USER.md"),
        (MEM, c["mem_lines"], c["mem_drop"], "MEMORY.md"),
    ):
        if drops:
            backup(path)
            drop = {i for i, _ in drops}
            atomic_write(path, "\n".join(l for i, l in enumerate(lines) if i not in drop) + "\n")
            (q / "removed" / label).write_text("\n".join(l for _, l in drops) + "\n", encoding="utf-8")
            manifest["removed_lines"][label] = len(drops)

    # 4) 偏好文件：整文件移进隔离区
    for p in c["pref_hits"]:
        backup(p)
        shutil.move(str(p), str(q / "files" / (p.name + ".removed")))
        manifest["moved"].append(str(p))

    # 5) 日记素材：按行删，原子写
    for path, hits in c["diary_hits"].items():
        backup(path)
        hit = set(hits)
        kept = [ln for ln in read_lines(path) if ln not in hit]
        atomic_write(path, "\n".join(kept) + ("\n" if kept else ""))
        (q / "removed" / (path.name + ".removed.jsonl")).write_text("\n".join(hits) + "\n", encoding="utf-8")
        manifest["removed_lines"][path.name] = len(hits)

    # 6) 浮动层状态 / 日志
    if c["disturb_hits"]:
        backup(DISTURB)
        state = json.loads(DISTURB.read_text(encoding="utf-8", errors="ignore"))
        drop = set(c["disturb_hits"])
        state["records"] = [r for i, r in enumerate(state.get("records", [])) if i not in drop]
        atomic_write(DISTURB, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        manifest["removed_lines"]["disturb_state"] = len(drop)
    for path, hits, label in (
        (BEHAVIOR, c["behavior_hits"], "behavior_log.jsonl"),
        (INJECT, c["inject_hits"], "inject.jsonl"),
    ):
        if hits:
            backup(path)
            hit = set(hits)
            kept = [ln for ln in read_lines(path) if ln not in hit]
            atomic_write(path, "\n".join(kept) + ("\n" if kept else ""))
            (q / "removed" / label).write_text("\n".join(hits) + "\n", encoding="utf-8")
            manifest["removed_lines"][label] = len(hits)

    # 7) DB：单事务，proactive_ledger 用同步墓碑软删，oplog 只脱敏文本字段
    db = sqlite3.connect(str(DESKMATE_DB), timeout=30)
    try:
        db.execute("BEGIN IMMEDIATE")
        if c["growth_drop"]:
            def _dirty(ev) -> bool:
                texts = " ".join(str(x) for x in (ev.get("texts") or []))
                return is_fact_polluted(texts) or is_reason_polluted(str(ev.get("reason") or ""))

            growth = c["growth"]
            growth["recent_events"] = [
                ev for ev in growth.get("recent_events", []) if not _dirty(ev)
            ]
            db.execute(
                "update json_blobs set data=? where key='growth'",
                (json.dumps(growth, ensure_ascii=False),),
            )
            manifest["db"]["growth_events_removed"] = len(c["growth_drop"])
        if c["ledger_rows"]:
            ids = [r["id"] for r in c["ledger_rows"]]
            qmarks = ",".join("?" for _ in ids)
            db.execute(
                f"update proactive_ledger set content='', evidence='', understanding='', "
                f"payload='', status='discarded', sync_tombstone=1 where id in ({qmarks})",
                ids,
            )
            manifest["db"]["ledger_tombstoned"] = len(ids)
        if c["oplog_rows"]:
            redact = "[redacted: expert 1v1 pollution]"
            n = 0
            for r in c["oplog_rows"]:
                try:
                    payload = json.loads(r["payload_json"])
                except Exception:
                    continue
                for k in ("content", "evidence", "understanding", "payload"):
                    if k in payload:
                        payload[k] = redact
                db.execute(
                    "update _sync_oplog set payload_json=? where seq=?",
                    (json.dumps(payload, ensure_ascii=False), r["seq"]),
                )
                n += 1
            manifest["db"]["oplog_payloads_redacted"] = n
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    (q / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return q


def verify() -> None:
    """清理后指纹复扫：文本文件按字节、SQLite 按 SQL（二进制页里的已删残留不算命中）。"""
    skip = {"_quarantine", "__pycache__", "node_modules", ".git"}
    exts = {".md", ".json", ".jsonl", ".txt", ".log"}
    hits = []
    for p in (HOME / ".deskmate").rglob("*"):
        if not p.is_file() or any(x in p.parts for x in skip) or p.suffix not in exts:
            continue
        try:
            if p.stat().st_size > 200 * 1024 * 1024:
                continue
            t = p.read_bytes().decode("utf-8", "ignore")
        except OSError:
            continue
        found = [n for n in SCAN_NEEDLES if n in t]
        if found:
            hits.append((str(p.relative_to(HOME)), found))

    db = sqlite3.connect(f"file:{DESKMATE_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    for t in [r[0] for r in db.execute("select name from sqlite_master where type='table'")]:
        cols = [r[1] for r in db.execute(f'pragma table_info("{t}")')]
        for c in cols:
            for n in SCAN_NEEDLES:
                extra, params = "", ["%" + n + "%"]
                if t == "proactive_ledger" and "id" in cols:
                    extra = " and id not in (%s)" % ",".join("?" for _ in LEGACY_LEDGER_IDS)
                    params += list(LEGACY_LEDGER_IDS)
                elif t == "_sync_oplog" and "record_id" in cols:
                    extra = " and record_id not like '%cloud_audit:self_expression:care%'"
                try:
                    k = db.execute(
                        f'select count(*) from "{t}" where cast("{c}" as text) like ? {extra}',
                        params,
                    ).fetchone()[0]
                except Exception:
                    continue
                if k:
                    hits.append((f"deskmate.db:{t}.{c}", [f"{n} x{k}"]))
    db.close()

    print("\n=== 复扫（活数据：~/.deskmate 文本文件 + deskmate.db SQL）===")
    if not hits:
        print("  ✅ 0 命中：污染指纹已全部清除")
    for where, found in hits:
        print(f"  ⚠ {where}: {found[:4]}")
    print("  明确保留（非污染，已逐条核对）：")
    for k in KNOWN_KEEP:
        print("   -", k)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行（默认仅 dry-run）")
    ap.add_argument("--verify-only", action="store_true", help="只做指纹复扫")
    args = ap.parse_args()
    if args.verify_only:
        verify()
        return
    c = collect()
    report(c)
    if not args.apply:
        print("\n(dry-run；加 --apply 执行。会先整库备份到 ~/.deskmate/_quarantine/)")
        return
    q = apply(c)
    print(f"\n已清理，隔离区 / 备份：{q}")
    verify()


if __name__ == "__main__":
    main()
