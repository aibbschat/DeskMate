"""P2 三生产者（2026-10-07）：让小桔主动「报告坏消息」。

设计文档 §11.3 / §11.8：新增来源 = 登记 + 产出函数，handoff / 同步 / 防 nag 全自动获得。
本模块提供三个「周期健康自检」生产者，全部 fail-open（异常一律返回 []，绝不阻断调用方）：

- ``automation``  → ``task_anomaly``：定时任务回执异常（该跑没跑 / 跑了零产出）。
- ``sync_health`` → ``drift`` / ``window_failed``：跨端同步漂移 / 同步窗口失败。
- ``writing``    → ``stall_flag``：手稿停笔位点（基于 docx mtime，确定性高）。

各生产者**内部按 ``proactivity_meta`` 节流**（每小时 / 每日），故可从任意 30s tick 或
每小时云端窗口无脑调用，不会高频打扰。确定性 id（``<source>:<kind>:<指纹>``）保证跨节点 /
跨调用不重复写。

接入点（见 ledger.run_producers_all 与 §11.14 部署接线）：
- Mac 桌面端：``ProactiveEngine.tick()``（复用 understanding 后处理范式，用户不在也跑）；
- 云端：``scripts/xiaoju_agency_tick.py`` 的 hourly 窗口；
- ``sync_health`` 的 ``drift`` 由 ``scripts/sync_daemon_runner.py`` 在同步窗口（有 transport、
  能拉对端快照）内调用 ``run_producer("sync_health", ctx={"drift": ...})`` 产出——单端生产者
  看不见对端全量，drift 必须发生在有隧道的同步层。

不变量（§九）：生产者只产出业务字段（kind/content/evidence/confidence/anchor），其余
（确定性 id / 同步 / 截断 / 写入门槛 / 递送 / understanding 加工）由管道统一解决。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from .ledger import register_producer, _resolve_base


# ---- 节流（复用 proactivity_meta 本端簿记，与 understanding 后处理同表）----
def _throttled(base_dir: Optional[Path], key: str, interval_s: float) -> bool:
    """距上次跑 >= interval 才放行；读写 ``proactivity_meta``。fail-open：异常即放行。"""
    try:
        db = _resolve_base(base_dir) / "deskmate.db"
        conn = sqlite3.connect(str(db), check_same_thread=False)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS proactivity_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        now = time.time()
        r = conn.execute(
            "SELECT value FROM proactivity_meta WHERE key=?", (key,)
        ).fetchone()
        last = float(r[0]) if r else 0.0
        if (now - last) < interval_s:
            conn.close()
            return False
        conn.execute(
            "INSERT INTO proactivity_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, f"{now:.3f}"),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:  # noqa: BLE001
        return True


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def _iso(ts: Optional[float]) -> str:
    if not ts:
        return "未知"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except Exception:  # noqa: BLE001
        return str(ts)


_FAIL_WORDS = ("火山", "限额", "quota", "额度", "无法", "失败", "对不起", "抱歉", "没能", "跑空", "超时", "429")


def _hit_fail_words(text: str) -> bool:
    t = (text or "").lower()
    return any(w.lower() in t for w in _FAIL_WORDS)


def _timeline_has_today(base_dir: Optional[Path], task_type: str) -> bool:
    """读 ``~/.deskmate/auto_tasks/timeline.jsonl``，判今天是否有某 task_type 的真实产出。

    这是「status=ok 但实际零产出」的硬锚点（文档 §11.3 automation 示例：每日读书
    status=ok 但 timeline.jsonl 停更）。stdlib 逐行解析，fail-open。
    """
    try:
        path = _resolve_base(base_dir) / "auto_tasks" / "timeline.jsonl"
        if not path.is_file():
            return True  # 无时间线文件 → 不误报（保守）
        today = _today()
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if rec.get("task_type") == task_type and rec.get("date") == today:
                    return True
        return False
    except Exception:  # noqa: BLE001
        return True


def _cron_missed(cron: str, last_run: Optional[float], now: float) -> bool:
    """用 croniter 判「该跑却没跑成」：距最近一次**已过去的**应跑时刻超过 30 分钟，
    且 last_run 早于该时刻 → miss。fail-open：croniter 不可用 / 解析失败 → 不报（保守，防 nag）。"""
    try:
        from croniter import croniter
        from datetime import datetime

        base = datetime.fromtimestamp(now - 86400)  # 24h 前起算
        itr = croniter(cron, base)
        past = []
        for _ in range(80):  # 足够覆盖 48h 内的应跑时刻
            m = itr.get_next(datetime)
            if m.timestamp() > now:
                break
            past.append(m.timestamp())
        if not past:
            return False
        last_due = max(past)  # 最近一次已过去的应跑时刻
        if last_run and last_run > last_due:
            return False  # 跑过了
        # 应跑时刻已过 30 分钟仍没跑 → 视为没跑成（给调度器一点宽限）
        return (now - last_due) > 1800
    except Exception:  # noqa: BLE001
        return False


@register_producer("automation")
def _automation_producer(base_dir: Optional[Path] = None) -> list:
    """``automation`` → ``task_anomaly``：定时任务回执异常（该跑没跑 / 跑了零产出）。

    数据：``~/.deskmate/automation.db`` 的 ``scheduled_tasks`` / ``task_runs``（JSON 列）+ ``auto_tasks/timeline.jsonl``。
    节流：每小时最多扫一次。
    """
    if not _throttled(base_dir, "producer:automation", 3600):
        return []
    items: list = []
    try:
        adb = _resolve_base(base_dir) / "automation.db"
        if not adb.is_file():
            return []
        conn = sqlite3.connect(str(adb), check_same_thread=False)
        try:
            tasks = conn.execute(
                "SELECT id, data FROM scheduled_tasks WHERE enabled=1"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        now = time.time()
        for tid, data in tasks:
            try:
                task = json.loads(data) if data else {}
            except Exception:  # noqa: BLE001
                continue
            title = task.get("title") or task.get("name") or tid
            cron = (task.get("schedule") or {}).get("cron")
            last_run = task.get("last_run")
            last_status = task.get("last_status")
            task_type = task.get("task_type")
            # 最近一次 run 回执
            run_row = conn.execute(
                "SELECT data FROM task_runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
                (tid,),
            ).fetchone()
            run = json.loads(run_row[0]) if run_row and run_row[0] else {}
            # ① 该跑却没跑成
            if cron and _cron_missed(cron, last_run, now):
                items.append({
                    "kind": "task_anomaly",
                    "content": f"今天「{title}」该跑却还没跑成，要不要去查一下？",
                    "evidence": f"cron={cron}; last_run={_iso(last_run)}; last_status={last_status}",
                    "confidence": 0.82,
                    "anchor": f"{tid}:{_today()}:miss",
                })
                continue
            # ② 跑了但零产出（status=ok 但无真实产出）
            status = run.get("status") or last_status
            if status == "ok":
                zero = False
                reason = ""
                if task_type in ("daily_reading", "progress_tracking"):
                    if not _timeline_has_today(base_dir, task_type):
                        zero = True
                        reason = f"timeline.jsonl {_today()} 无 {task_type} 记录"
                if not zero:
                    artifacts = run.get("artifacts") or []
                    result_text = run.get("result_text") or ""
                    if not artifacts and _hit_fail_words(result_text):
                        zero = True
                        reason = "回执 ok 但无产出文件且回复疑似被限额/失败"
                if zero:
                    items.append({
                        "kind": "task_anomaly",
                        "content": f"「{title}」今天回执 ok，但实际没产出（疑似火山限额跑空），得留意。",
                        "evidence": f"last_run={_iso(last_run)} status=ok; {reason}",
                        "confidence": 0.8,
                        "anchor": f"{tid}:{_today()}:zero",
                    })
        conn.close()
    except Exception as e:  # noqa: BLE001
        logger.debug("automation 生产者失败（不阻断）: %s", e, exc_info=True)
        return []
    return items


@register_producer("sync_health")
def _sync_health_producer(base_dir: Optional[Path] = None, drift: Optional[dict] = None) -> list:
    """``sync_health`` → ``window_failed`` / ``drift``。

    - 无 ``drift``（由 ``run_producers_all`` 触发）：只做 ``window_failed``——读 ``.sync_cursor.json``
      游标，若最近同步窗口超时（>2h 未成功）即报；本地可判定、价值明确。
    - 有 ``drift``（由 ``scripts/sync_daemon_runner.py`` 在同步窗口内传，含对端快照对称差）：
      产出 ``drift`` 条目；单端生产者看不见对端全量，drift 必须发生在有隧道的同步层。
    """
    items: list = []
    # ---- drift（由同步层注入真实两端数据）----
    if drift:
        for table, delta in drift.items():
            try:
                delta = int(delta)
            except Exception:  # noqa: BLE001
                continue
            if delta <= 0:
                continue
            items.append({
                "kind": "drift",
                "content": f"表「{table}」本地↔云端 sync_uuid 漂移 {delta} 条，疑似某端漏同步，得查。",
                "evidence": f"集合级对账对称差(table={table})={delta}",
                "confidence": 0.8,
                "anchor": f"drift:{table}",
            })
        return items
    # ---- window_failed（本地游标超时）----
    if not _throttled(base_dir, "producer:sync_health", 3600):
        return []
    try:
        db = _resolve_base(base_dir) / "deskmate.db"
        cursor_path = str(db) + ".sync_cursor.json"
        if not os.path.exists(cursor_path):
            return []
        with open(cursor_path, "r", encoding="utf-8") as f:
            cur = json.load(f)
        # 游标可能含 pull_after_seq / push_after_seq / updated_at 等；取最可信的更新时间
        updated = (
            cur.get("updated_at")
            or cur.get("pull_updated_at")
            or cur.get("push_updated_at")
        )
        if not updated:
            return []
        now = time.time()
        ts = None
        if isinstance(updated, (int, float)):
            ts = float(updated)
        else:
            try:
                ts = time.mktime(time.strptime(str(updated)[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:  # noqa: BLE001
                try:
                    ts = float(updated)
                except Exception:  # noqa: BLE001
                    ts = None
        if ts is None:
            return []
        overdue_min = int((now - ts) / 60)
        if overdue_min > 120:  # 窗口每小时一次，>2h 未成功 = 异常
            items.append({
                "kind": "window_failed",
                "content": "跨端同步窗口已超过 2 小时没成功跑过，本地和云端可能没在同步了，得查一下。",
                "evidence": f"cursor updated_at={updated}; 已超时约 {overdue_min} 分钟",
                "confidence": 0.8,
                "anchor": f"window_failed:{_today()}",
            })
    except Exception as e:  # noqa: BLE001
        logger.debug("sync_health 生产者失败（不阻断）: %s", e, exc_info=True)
        return []
    return items


# 《长编》（国子监秘闻录）工作稿默认路径；可经环境变量 SCRIBE_DOCX 覆盖（多书场景扩展点）。
_DEFAULT_SCRIBE_DOCX = os.path.expanduser(
    "~/Documents/已出版/20.国子监秘闻录/国子监秘闻录.docx"
)
_STALL_DAYS_THRESHOLD = 3  # 停笔超过此天数才报（避免每天 nag）


@register_producer("writing")
def _writing_producer(base_dir: Optional[Path] = None) -> list:
    """``writing`` → ``stall_flag``：手稿停笔位点（基于 docx mtime，确定性高）。

    只读 docx 文件 mtime（不读内容、不碰写，安全）；停笔超阈值才报，每天重扫同 id 合并。
    伏笔未收 / 新增段落需读正文 + LLM，置信度难达 0.75 写入门槛，本期先只做 ``stall_flag``
    （设计文档 §11.8 标注「16:00 待验证」，stall_flag 靠 mtime 差值最稳）。
    """
    if not _throttled(base_dir, "producer:writing", 3600 * 24):  # 每日至多扫一次
        return []
    items: list = []
    try:
        docx = os.environ.get("SCRIBE_DOCX") or _DEFAULT_SCRIBE_DOCX
        p = Path(docx).expanduser()
        if not p.is_file():
            return []
        mtime = p.stat().st_mtime
        stall_days = (time.time() - mtime) / 86400.0
        if stall_days >= _STALL_DAYS_THRESHOLD:
            items.append({
                "kind": "stall_flag",
                "content": f"你《长编》已经 {int(stall_days)} 天没动笔了，是卡在哪儿了吗？",
                "evidence": f"文档: {p.name}; 最后修改: {_iso(mtime)}; 停笔约 {int(stall_days)} 天",
                "confidence": 0.82,
                "anchor": "writing:stall:guozijian",
            })
    except Exception as e:  # noqa: BLE001
        logger.debug("writing 生产者失败（不阻断）: %s", e, exc_info=True)
        return []
    return items
