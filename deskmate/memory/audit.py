"""记忆层自动化体检（P0-1，对标 MemPalace ``mempalace audit``）。

为什么需要它
------------
deskmate-memory-audit / deskmate-display-audit 两个 skill 都是**人工**巡检：
每次都要现查表结构、现写 SQL、现判读。而记忆层的故障大多是「静默退化」形态——
语义检索退回关键词、gap 全 0 访谈断粮、日记块固化进 MEMORY.md——不主动体检
就只能等「小桔看起来变笨了」才被发现。本模块把体检变成一条命令：
``python -m deskmate.memory.audit``。

五层检查（对齐 MemPalace 五层 audit，判据全部来自真实踩过的坑）
================================================================
1. **向量健康**：external_items 的「digested 数 > 0 而向量数 = 0」指纹
   （2026-09-22 语义检索坑：升格走 mark_digested 从不经过 embedded，
   于是既搜不到也补不上）；embedding_dim 混用（换后端未重刷全表，
   cosine 维度不等静默返 0.0）；有 blob 但 dim=0 的脏行。
2. **画像健康**：profile_items 各层 status 分布、低置信活跃条目、
   open gap 计数（=0 是「访谈引擎断粮」的静默失败信号，见
   cognitive/conflict_gap.py 顶部说明）；过期仍 active 的条目。
3. **全局记忆**：MEMORY.md 体积（超 20K 早该触发压缩）、
   「§ 日记沉淀」分节计数（**应恒为 0**——日记只活在 DiaryStore，
   出现即 09-21 那种 load/save 不对称的固化 bug 复发）、
   衰减白名单条目日期前缀是否可解析（日期正则失配 → 衰减永不触发的老坑）。
4. **检索可达**：history 目录各库（史料 ``<expert>.db`` / 私人 ``private-*.db``）
   articles/chunks/chunks_fts 行数一致性（chunks_fts 落后于 chunks = 「记了查不到」）。
5. **溯源完整**：traces/inject.jsonl、forgot.jsonl 是否在写（P0-4 + 8.3：
   「懂」与「忘」必须可查证；inject.jsonl 长期为空 = 溯源链路断）。

硬约束
------
- **全程只读**：所有 SQLite 连接 ``mode=ro``，不写任何库、任何文件。
- **fail-open**：任何单项失败不阻塞整体，该项标 ``fail`` 并记录原因。
- **零 LLM、零新依赖**：纯 stdlib。
- **路径**：统一走 :func:`deskmate.paths.deskmate_base_dir`（调用时解析，
  禁模块级常量——测试隔离红线）。

输出形态
--------
``run_audit(base_dir=None) -> dict``：机器可读 JSON（每层 status/detail/problems
+ 全局 problems 汇总）。CLI 人类可读摘要；``--json`` 出 JSON。
退出码恒为 0（这是诊断不是断言）；CI 想卡关自行解析 JSON 里的 problems。
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量（判据阈值，全部有出处）
# ---------------------------------------------------------------------------
#: 与 manager.MEMORY_FILE_MAX_CHARS 对齐：超过即「压缩该触发没触发」
MEMORY_FILE_MAX_CHARS = 20_000
#: 「§ 日记沉淀」在 MEMORY.md 里的合法数量：恒为 0（日记绝不落 MEMORY.md，红线）
_DIARY_SECTION_MAX = 0
#: § 分节头：`§ 标题 #标签1 #标签2`（与 manager._parse_sections 的格式约定同源）
_SEC_HEADER_RE = re.compile(r"^§\s*(.+?)\s*(#.*)?$")
_TAG_RE = re.compile(r"#([A-Za-z0-9_\-]+)")
#: 与 manager._ENTRY_DATE_RE 同源：条目日期前缀（列表前缀 + 中英冒号兼容）
_ENTRY_DATE_RE = re.compile(r"^\s*[-*]?\s*(\d{4}-\d{1,2}-\d{1,2})[:：]")
#: 衰减白名单标签（与 manager.MEMORY_DECAY_TTL 的 key 一致）——这些**分节**下的
#: 条目才会衰减；无日期前缀的行按 decay_memory 的「安全起见保留」**永不衰减**
#: （2026-09-21 前的老坑：日期正则失配 → 整节条目全部漏判、衰减永不触发）
_DECAY_TAGS = ("work", "note")
#: active 条目低置信告警线
_LOW_CONFIDENCE = 0.3

_STATUS_OK = "ok"
_STATUS_WARN = "warn"
_STATUS_ERROR = "error"   # 有确定的问题（对应已知 bug 形态）
_STATUS_FAIL = "fail"     # 检查本身跑不了（库缺表/文件缺失等，检查对象的错不算）


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _ro(db_path: Path) -> Optional[sqlite3.Connection]:
    """只读打开 SQLite；文件不存在 / 打不开返回 None（不抛）。"""
    if not Path(db_path).is_file():
        return None
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _check(name: str, status: str, detail: dict, problems: Optional[list] = None,
           note: str = "") -> dict:
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "problems": problems or [],
        "note": note,
    }


def _problem(level: str, message: str, **kw: Any) -> dict:
    p = {"level": level, "message": message}
    p.update(kw)
    return p


# ---------------------------------------------------------------------------
# 1. 向量健康
# ---------------------------------------------------------------------------
def check_vector_health(db_path: Path) -> dict:
    """external_items 向量覆盖率 / 维度一致性。判据出处见模块 docstring 第 1 层。"""
    detail: dict[str, Any] = {}
    problems: list[dict] = []
    conn = _ro(db_path)
    if conn is None:
        return _check("向量健康", _STATUS_FAIL, detail,
                      note="deskmate.db 不存在或无法只读打开（首次安装属正常）")
    try:
        if not _table_exists(conn, "external_items"):
            return _check("向量健康", _STATUS_OK, detail,
                          note="external_items 表不存在（尚无外部信息，跳过）")
        cols = _columns(conn, "external_items")
        total = conn.execute("SELECT COUNT(*) FROM external_items").fetchone()[0]
        detail["total"] = total
        if total == 0:
            return _check("向量健康", _STATUS_OK, detail, note="外部信息为空，跳过")
        if "ingestion_status" in cols:
            detail["by_status"] = {
                str(r[0] or "raw"): r[1]
                for r in conn.execute(
                    "SELECT ingestion_status, COUNT(*) FROM external_items "
                    "GROUP BY ingestion_status")
            }
        if "embedding" not in cols:
            return _check("向量健康", _STATUS_FAIL, detail,
                          note="external_items 缺 embedding 列（库版本异常）")
        n_vec = conn.execute(
            "SELECT COUNT(*) FROM external_items WHERE embedding IS NOT NULL").fetchone()[0]
        detail["with_embedding"] = n_vec

        # 健康指纹：digested > 0 而向量 = 0 → 既搜不到也补不上（09-22 实锤形态）
        digested = (detail.get("by_status") or {}).get("digested", 0)
        if digested > 0 and n_vec == 0:
            problems.append(_problem(
                "error",
                f"digested 条目 {digested} 条但全表 0 向量——语义通道「既搜不到也补不上」"
                f"（2026-09-22 已知坑：需清空重刷 / 检查 embedding 后端）",
                digested=digested))
        elif total > 0 and n_vec == 0:
            problems.append(_problem(
                "warn", f"全表 {total} 条均无向量，语义检索退化为纯关键词"))

        # 维度一致性：混用后端 → cosine 维度不等静默返 0.0
        if "embedding_dim" in cols:
            dims = {int(r[0] or 0) for r in conn.execute(
                "SELECT DISTINCT embedding_dim FROM external_items "
                "WHERE embedding IS NOT NULL")}
            detail["dims"] = sorted(dims)
            if len(dims - {0}) > 1:
                problems.append(_problem(
                    "error",
                    f"向量维度混用 {sorted(dims)}——换后端必须重刷全表，否则"
                    f"维度不等的条目相似度恒为 0（静默故障）"))
            if 0 in dims:
                problems.append(_problem(
                    "warn", "存在有 blob 但 dim=0 的脏行（写入未记维度）"))
        # 模型混用
        if "embedding_model" in cols:
            models = {str(r[0] or "") for r in conn.execute(
                "SELECT DISTINCT embedding_model FROM external_items "
                "WHERE embedding IS NOT NULL")}
            detail["models"] = sorted(m for m in models if m)
            if len([m for m in models if m]) > 1:
                problems.append(_problem(
                    "warn", f"embedding_model 混用：{sorted(models)}"))
        status = _STATUS_ERROR if any(p["level"] == "error" for p in problems) else (
            _STATUS_WARN if problems else _STATUS_OK)
        return _check("向量健康", status, detail, problems)
    except sqlite3.Error as e:
        return _check("向量健康", _STATUS_FAIL, detail, note=f"查询失败: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. 画像健康
# ---------------------------------------------------------------------------
def check_profile_health(db_path: Path) -> dict:
    """profile_items 分层状态分布 / 低置信 / 过期仍活跃 / open gap 断粮信号。"""
    detail: dict[str, Any] = {}
    problems: list[dict] = []
    conn = _ro(db_path)
    if conn is None:
        return _check("画像健康", _STATUS_FAIL, detail,
                      note="deskmate.db 不存在或无法只读打开")
    try:
        if not _table_exists(conn, "profile_items"):
            return _check("画像健康", _STATUS_OK, detail,
                          note="profile_items 表不存在（认知层未启用，跳过）")
        cols = _columns(conn, "profile_items")
        rows = conn.execute(
            "SELECT layer, status, COUNT(*) FROM profile_items GROUP BY layer, status"
        ).fetchall()
        by_layer: dict[str, dict[str, int]] = {}
        for layer, status, n in rows:
            by_layer.setdefault(str(layer), {})[str(status)] = n
        detail["by_layer_status"] = by_layer
        total = sum(n for d in by_layer.values() for n in d.values())
        detail["total"] = total
        if total == 0:
            return _check("画像健康", _STATUS_OK, detail, note="画像为空，跳过")

        # 低置信活跃条目（活跃却没什么证据）
        low = conn.execute(
            "SELECT COUNT(*) FROM profile_items WHERE status='active' AND confidence < ?",
            (_LOW_CONFIDENCE,)).fetchone()[0]
        detail["active_low_confidence"] = low
        if low:
            problems.append(_problem(
                "warn", f"{low} 条 active 条目置信度 < {_LOW_CONFIDENCE}，"
                        f"考虑 questioned / 校准而非继续带着注入"))

        # 过期仍 active（valid_until 列存在时）
        if {"valid_until"} <= cols:
            expired = conn.execute(
                "SELECT COUNT(*) FROM profile_items WHERE status='active' "
                "AND valid_until IS NOT NULL AND valid_until != '' "
                "AND valid_until < ?", (_now_iso(),)).fetchone()[0]
            detail["active_but_expired"] = expired
            if expired:
                problems.append(_problem(
                    "warn", f"{expired} 条 active 条目 valid_until 已过——"
                            f"加载侧会自行失效，但建议归档以免越积越多"))

        # 僵尸：终态条目（superseded/archived/forgotten）计数，仅报告
        terminal = {s: sum(d.get(s, 0) for d in by_layer.values())
                    for s in ("superseded", "archived", "forgotten")}
        detail["terminal_items"] = {k: v for k, v in terminal.items() if v}

        # open gap：全 0 = 访谈引擎断粮（conflict_gap.py 顶部说明的静默失败）
        if _table_exists(conn, "cognitive_gaps"):
            gap_rows = conn.execute(
                "SELECT layer, COUNT(*) FROM cognitive_gaps WHERE status='open' "
                "GROUP BY layer").fetchall()
            open_gaps = {str(l): n for l, n in gap_rows}
            detail["open_gaps"] = open_gaps
            if sum(open_gaps.values()) == 0:
                problems.append(_problem(
                    "warn",
                    "open gap 全层为 0——访谈引擎无燃料（should_interview_now "
                    "需 open gap ≥ 2），且这是静默失败：现象是「她再也不问了」",
                    hint="确认 conflict_gap.tick 调度是否在跑 / ensure_emotional_gaps 是否幂等封顶"))
        status = _STATUS_ERROR if any(p["level"] == "error" for p in problems) else (
            _STATUS_WARN if problems else _STATUS_OK)
        return _check("画像健康", status, detail, problems)
    except sqlite3.Error as e:
        return _check("画像健康", _STATUS_FAIL, detail, note=f"查询失败: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. 全局记忆（MEMORY.md / USER.md / forgot）
# ---------------------------------------------------------------------------
def _scan_sections(text: str) -> tuple[list[str], int]:
    """按 manager 的 § 分节格式扫 MEMORY.md。

    Returns:
        (missing_date_entries, diary_sections)
        - missing_date_entries: 衰减白名单分节（#work / #note）下**无日期前缀**
          的正文行——按 decay_memory 语义这些行永不衰减；
        - diary_sections: 「§ 日记沉淀」分节数（红线：恒为 0，出现即固化 bug）。
    """
    missing: list[str] = []
    diary_sections = 0
    current_is_decay = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("§"):
            m = _SEC_HEADER_RE.match(line)
            title = m.group(1) if m else line[1:].strip()
            tags = set(_TAG_RE.findall(line))
            # 中文标题本身也可能是标签名（manager 归并按「标题或英文标签」双口径）
            if title in _DECAY_TAGS:
                tags.add(title)
            current_is_decay = bool(tags & set(_DECAY_TAGS))
            if re.match(r"日记沉淀\b", title or ""):
                diary_sections += 1
            continue
        if not line or line.startswith("#"):
            continue
        if current_is_decay and not _ENTRY_DATE_RE.match(line):
            missing.append(line[:60])
    return missing, diary_sections


def check_global_memory(base_dir: Path) -> dict:
    """MEMORY.md 体积 / 日记沉淀回流 / 衰减条目日期前缀 / USER.md 体积。"""
    detail: dict[str, Any] = {}
    problems: list[dict] = []
    mem = base_dir / "MEMORY.md"
    if not mem.is_file():
        return _check("全局记忆", _STATUS_OK, detail, note="MEMORY.md 不存在（尚未积累，跳过）")
    try:
        text = mem.read_text(encoding="utf-8")
    except OSError as e:
        return _check("全局记忆", _STATUS_FAIL, detail, note=f"读取失败: {e}")
    detail["chars"] = len(text)
    if len(text) > MEMORY_FILE_MAX_CHARS:
        problems.append(_problem(
            "warn", f"MEMORY.md {len(text)} 字符 > 上限 {MEMORY_FILE_MAX_CHARS}，"
                    f"LLM 压缩合并该触发而未触发"))
    # 日记沉淀回流：红线「日记绝不落 MEMORY.md」，出现即固化 bug 复发
    no_date, diary_sections = _scan_sections(text)
    detail["diary_sections"] = diary_sections
    if diary_sections > _DIARY_SECTION_MAX:
        problems.append(_problem(
            "error",
            f"MEMORY.md 出现 {diary_sections} 个「§ 日记沉淀」分节——日记固化 bug"
            f"（load 读视图/save 不写视图的不对称）复发，清理脚本 "
            f"scripts/cleanup_memory_diary_blocks.py"))
    # 衰减白名单分节下无日期前缀的行：按 decay_memory 语义永不衰减
    detail["decay_entries_missing_date"] = len(no_date)
    if no_date:
        problems.append(_problem(
            "warn",
            f"#work/#note 分节下 {len(no_date)} 行无日期前缀——按衰减语义"
            f"这些行「安全保留」= 永不衰减；若是条目应补「YYYY-MM-DD: 」前缀，"
            f"若是摘要段落可无视",
            examples=no_date[:3]))
    # USER.md 仅报告体积
    user = base_dir / "USER.md"
    if user.is_file():
        try:
            detail["user_md_chars"] = len(user.read_text(encoding="utf-8"))
        except OSError:
            pass
    status = _STATUS_ERROR if any(p["level"] == "error" for p in problems) else (
        _STATUS_WARN if problems else _STATUS_OK)
    return _check("全局记忆", status, detail, problems)


# ---------------------------------------------------------------------------
# 4. 检索可达（history 目录：史料库 + 私人库）
# ---------------------------------------------------------------------------
def check_reachability(base_dir: Path) -> dict:
    """history 目录各库的 chunks vs chunks_fts 一致性（落后 = 「记了查不到」）。"""
    detail: dict[str, Any] = {}
    problems: list[dict] = []
    hist = base_dir / "history"
    if not hist.is_dir():
        return _check("检索可达", _STATUS_OK, detail,
                      note="history 目录不存在（未灌过库，跳过）")
    dbs = sorted(p for p in hist.glob("*.db")
                 if p.is_file() and not p.name.endswith(("-wal", "-shm")))
    detail["dbs"] = [p.name for p in dbs]
    if not dbs:
        return _check("检索可达", _STATUS_OK, detail, note="history 目录无库，跳过")
    for p in dbs:
        conn = _ro(p)
        entry: dict[str, Any] = {}
        if conn is None:
            problems.append(_problem("warn", f"{p.name} 无法只读打开"))
            detail[p.name] = {"error": "open_failed"}
            continue
        try:
            for table in ("articles", "chunks", "chunks_fts"):
                if _table_exists(conn, table):
                    entry[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if "chunks" in entry and "chunks_fts" in entry:
                entry["fts_lag"] = entry["chunks"] - entry["chunks_fts"]
                if entry["fts_lag"] > 0:
                    problems.append(_problem(
                        "error",
                        f"{p.name}: chunks 比 chunks_fts 多 {entry['fts_lag']} 行——"
                        f"新灌的内容 FTS 不可见（「记了查不到」）"))
        except sqlite3.Error as e:
            entry["error"] = str(e)
            problems.append(_problem("warn", f"{p.name} 查询失败: {e}"))
        finally:
            conn.close()
        detail[p.name] = entry
    status = _STATUS_ERROR if any(p["level"] == "error" for p in problems) else (
        _STATUS_WARN if problems else _STATUS_OK)
    return _check("检索可达", status, detail, problems)


# ---------------------------------------------------------------------------
# 5. 溯源完整
# ---------------------------------------------------------------------------
def check_traceability(base_dir: Path) -> dict:
    """traces/inject.jsonl 与 forgot.jsonl 是否在写（P0-4 + 8.3）。"""
    detail: dict[str, Any] = {}
    problems: list[dict] = []
    tdir = base_dir / "traces"
    if not tdir.is_dir():
        return _check("溯源完整", _STATUS_WARN, detail,
                      problems=[_problem(
                          "warn", "traces 目录不存在——溯源链路从未写过"
                                  "（若 app 已用过一段时间，prefetch 溯源可能没接线）")],
                      note="traces 目录不存在")
    for fname in ("inject.jsonl", "forgot.jsonl"):
        p = tdir / fname
        if not p.is_file():
            detail[fname] = "missing"
            continue
        try:
            n = sum(1 for _ in p.open("r", encoding="utf-8", errors="replace"))
            detail[fname] = {"lines": n, "bytes": p.stat().st_size}
        except OSError as e:
            detail[fname] = {"error": str(e)}
    inject = detail.get("inject.jsonl")
    if inject == "missing":
        problems.append(_problem(
            "warn", "traces/inject.jsonl 不存在——「每轮注入了哪些记忆」不可查证"))
    elif isinstance(inject, dict) and inject.get("lines", 0) == 0:
        problems.append(_problem(
            "warn", "traces/inject.jsonl 为空——溯源在接线但没写（或从未对话过）"))
    status = _STATUS_WARN if problems else _STATUS_OK
    return _check("溯源完整", status, detail, problems)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def run_audit(base_dir: str | Path | None = None) -> dict:
    """跑全部五层体检。任何一层异常不影响其余层（fail-open）。

    Args:
        base_dir: 记忆根目录；None 时调用 :func:`deskmate_base_dir()`
            （**调用时**解析 DESKMATE_DIR——测试隔离红线）。

    Returns:
        {"base_dir", "ran_at", "checks": [五层结果], "problems": 汇总,
         "summary": {"ok": n, "warn": n, "error": n, "fail": n}}
    """
    if base_dir is None:
        from deskmate.paths import deskmate_base_dir
        base = deskmate_base_dir()
    else:
        base = Path(base_dir)
    base = Path(base).expanduser()

    runners = (
        ("vector", lambda: check_vector_health(base / "deskmate.db")),
        ("profile", lambda: check_profile_health(base / "deskmate.db")),
        ("global_memory", lambda: check_global_memory(base)),
        ("reachability", lambda: check_reachability(base)),
        ("traceability", lambda: check_traceability(base)),
    )
    checks: list[dict] = []
    for key, fn in runners:
        try:
            c = fn()
        except Exception as e:  # fail-open：单项崩了不拖垮整体
            logger.warning("体检项 %s 失败", key, exc_info=True)
            c = _check(key, _STATUS_FAIL, {}, note=f"检查自身异常: {e}")
        c["key"] = key
        checks.append(c)

    problems = [
        {"check": c["name"], **p}
        for c in checks for p in c.get("problems", [])
    ]
    summary = {"ok": 0, "warn": 0, "error": 0, "fail": 0}
    for c in checks:
        summary[c["status"]] = summary.get(c["status"], 0) + 1
    return {
        "base_dir": str(base),
        "ran_at": _now_iso(),
        "checks": checks,
        "problems": problems,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
_STATUS_MARK = {_STATUS_OK: "✓", _STATUS_WARN: "⚠", _STATUS_ERROR: "✗", _STATUS_FAIL: "–"}


def _print_human(report: dict) -> None:
    print(f"记忆体检 · {report['base_dir']} · {report['ran_at']}")
    print("=" * 60)
    for c in report["checks"]:
        print(f"{_STATUS_MARK.get(c['status'], '?')} {c['name']} [{c['status']}]")
        for k, v in c["detail"].items():
            vs = json.dumps(v, ensure_ascii=False)
            print(f"    {k}: {vs[:120]}")
        for p in c["problems"]:
            print(f"    [{p['level']}] {p['message']}")
        if c.get("note"):
            print(f"    (note) {c['note']}")
    s = report["summary"]
    print("=" * 60)
    print(f"汇总: ok={s['ok']} warn={s['warn']} error={s['error']} fail={s['fail']}"
          f" · 问题 {len(report['problems'])} 条")
    for p in report["problems"]:
        print(f"  [{p['level']}] ({p['check']}) {p['message']}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m deskmate.memory.audit",
        description="小桔记忆层自动化体检（只读，不写任何数据）",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--base-dir", default=None,
                        help="记忆根目录（默认 DESKMATE_DIR 或 ~/.deskmate）")
    args = parser.parse_args(argv)
    report = run_audit(args.base_dir)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
