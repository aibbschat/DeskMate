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


# ---- 节流（本端 proactivity_meta 簿记 + R2 跨节点共享簿记，2026-10-10）----
def _throttled(
    base_dir: Optional[Path],
    key: str,
    interval_s: float,
    *,
    source: Optional[str] = None,
    kind: Optional[str] = None,
    window_s: Optional[float] = None,
) -> bool:
    """本端 + 跨节点节流（R2，2026-10-10）。

    - 本端：距上次跑 >= interval 才放行（读写 ``proactivity_meta``）。
    - 跨节点：source+kind 在 window_s 内已由任一节点（同步来的）proactive_ledger 产出过
      → 视为「已产出」跳过，使 Mac+云端同一时段只报一次（治 R2）。
    返回 True=放行，False=节流跳过。fail-open：异常一律放行（True），绝不阻断调用方。
    """
    try:
        db = _resolve_base(base_dir) / "deskmate.db"
        conn = sqlite3.connect(str(db), check_same_thread=False)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS proactivity_meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            now = time.time()
            r = conn.execute(
                "SELECT value FROM proactivity_meta WHERE key=?", (key,)
            ).fetchone()
            last = float(r[0]) if r else 0.0
            if (now - last) < interval_s:
                return False  # 本端未到间隔 → 节流
            # R2 跨节点：其他节点近期已产出同 source+kind → 节流（不写本端时间戳）
            if source and window_s:
                from .ledger import _throttled_shared

                if _throttled_shared(
                    base_dir, source, window_s, kinds={kind} if kind else None
                ):
                    return False
            conn.execute(
                "INSERT INTO proactivity_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, f"{now:.3f}"),
            )
            conn.commit()
        finally:
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


# ⚠️ 2026-10-09 **移除**关键词失败词表（原 _FAIL_WORDS / _hit_fail_words）。
# 它是「回执 ok 但零产出」判据的唯一依据，实测**双向都错**：
#   误伤：词表含「抱歉/失败/无法/火山/额度」，而**成功**的汇报原文就会写
#         「不擅自删除」「没有超时」这类措辞 ⇒ 成功被判失败；
#   漏报：它假设「产出必进 artifacts」，而读书任务 artifacts 恒为 []
#         （正文写进 .deskmate/tmp_reading/，不走 artifact 通道）。
# 零产出判据已改为**只认 timeline.jsonl 硬锚点**（见 _automation_producer ②）。
# 保留符号仅供旧测试/外部引用不断裂，实际不再被调用。
_FAIL_WORDS: tuple[str, ...] = ()


def _hit_fail_words(text: str) -> bool:
    """**已废弃**，恒返回 False。

    保留函数名只为不打断可能存在的外部引用；新逻辑不再用关键词判失败。
    """
    return False


def _title_stem(title: str) -> str:
    """取任务标题的「冒号前缀」——它才是与 timeline 的 task_name 对得上的部分。

    ⚠️ 2026-10-09 实测：任务 title 与 timeline task_name 是**冒号前缀关系**——

        title「每日读书：萧盛已出版作品」  →  task_name「每日读书」
        title「进度跟踪：国子监秘闻录」    →  task_name「进度跟踪」

    所以不能再拿 ``task.get("task_type")`` 去匹配（实测 5 个启用任务的 task_type
    **全是 None**，该判据对所有任务永不触发）。
    """
    t = (title or "").strip()
    for sep in ("：", ":", "·", "-", "—", "—"):
        if sep in t:
            head = t.split(sep, 1)[0].strip()
            if head:
                return head
    return t


def _timeline_has_today(base_dir: Optional[Path], task_name: str) -> bool:
    """读 ``auto_tasks/timeline.jsonl``，判今天是否有某 ``task_name`` 的真实产出。

    这是「status=ok 但实际零产出」的**硬锚点**。stdlib 逐行解析，fail-open。

    匹配用 task_name，并额外容忍 record 自带 task_type 时按 type 匹配
    （老记录可能只有 type 没有 name）。
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
                if rec.get("date") != today:
                    continue
                name = rec.get("task_name") or ""
                rtype = rec.get("task_type") or ""
                if (name and name == task_name) or (rtype and rtype == task_name):
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


def _cron_due_today(cron: Optional[str], now: float) -> bool:
    """今天**是否已经到过**该任务的应跑时刻（不含 30 分钟宽限，那是 _cron_missed 的事）。

    为什么需要它（2026-10-10 实测）：判据②「今天零产出」若不先问「今天该不该跑」，
    就会在**任务还没到点**的清晨报「今天零产出」。实测 06:49 跑体检，5 个当日
    09:30–16:00 才到点的任务被**全部**误报——它们连跑都还没跑。

    fail-open 方向选择：**无 cron / croniter 不可用 → True**（保守，可能误报但不会漏报），
    与 _cron_missed 的 fail-closed 方向相反，因为二者管的是不同后果。
    """
    if not cron:
        return False  # 无 cron = 手动任务，不按「今天该跑」处理
    try:
        from croniter import croniter
        from datetime import datetime, timedelta

        # ⚠️ 本地午夜**不能**用 now % 86400 算——那是 UTC 午夜。本机 UTC+8 时，
        # now=06:49 本地时 now%86400 会落到前一天 22:49，起点直接错一天，
        # 导致「每天 09:30 的任务」在清晨被判成「今天还没到点」之外的怪结果。
        # 必须用 datetime 构造本地 00:00。
        day_start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        itr = croniter(cron, day_start - timedelta(seconds=1))
        for _ in range(96):  # 每天至多 96 个应跑点，足够
            m = itr.get_next(datetime)
            if m.timestamp() > now:
                return False
            if _today() == time.strftime("%Y-%m-%d", time.localtime(m.timestamp())):
                return True
        return False
    except Exception:  # noqa: BLE001
        return True  # 判不出来 → 保守，不漏报


def _automation_db_path(base_dir: Optional[Path] = None) -> Optional[Path]:
    """定位 Coworker 定时任务库 ``automation.db`` 的**真实落点**（找不到返回 None）。

    该库属 Coworker 主程序（``manager.TaskStore``，基目录由 ``--data-dir`` / ``state_dir``
    决定），**不在 deskmate 数据目录下**。历史误写成 ``~/.deskmate/automation.db``，
    而该路径两端都不存在 → 生产者永远空转（已踩：本机/云端均 MISSING）。
    实际落点两端不同，故按候选顺序取第一个存在的：
    - 本机：``$COWORKER_STATE_DIR`` 或 ``~/.config/coworker/automation.db``；
    - 云端：``/var/lib/openworker/automation.db``（= deskmate 基目录的上一级）；
    - 显式 ``base_dir`` 内若有（测试注入 / 调用方指定）优先。
    """
    cands: list[Path] = []
    if base_dir is not None:
        b = Path(base_dir).expanduser()
        cands += [b / "automation.db", b.parent / "automation.db"]
    try:
        from coworker.secrets import state_dir

        cands.append(state_dir() / "automation.db")
    except Exception:  # noqa: BLE001
        cands.append(Path.home() / ".config" / "coworker" / "automation.db")
    cands.append(Path.home() / ".deskmate" / "automation.db")  # 历史误写位置，兼容
    for c in cands:
        if c.is_file():
            return c
    return None


@register_producer("automation")
def _automation_producer(base_dir: Optional[Path] = None) -> list:
    """``automation`` → ``task_anomaly``：定时任务回执异常（该跑没跑 / 跑了零产出）。

    数据：Coworker 定时任务库 ``automation.db``（见 ``_automation_db_path``）的
    ``scheduled_tasks`` / ``task_runs``（JSON 列）+ ``auto_tasks/timeline.jsonl``。
    节流：每小时最多扫一次。
    """
    if not _throttled(base_dir, "producer:automation", 3600, source="cloud_audit", kind="task_anomaly", window_s=3600):
        return []
    items: list = []
    try:
        adb = _automation_db_path(base_dir)
        if adb is None:
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
            # 最近一次 run 回执 + 它的 started_at
            #
            # ⚠️ 2026-10-09 修误报（缺陷①）：原实现只读 ``last_run``，而 last_run **只在任务
            # 结束时才更新** ⇒ 任务运行期间读到的仍是上一次（甚至 None）→ 必然判「没跑成」。
            # 实测铁证：10-09 读书任务 10:00:57 开始、10:37:16 结束，而误报发生在
            # **10:34:13** —— 跑了 36 分钟，它在第 34 分钟报「该跑却没跑成」。
            # task_runs.started_at 记的是**开始**时间，用它判定「本次已开跑」即可识破。
            run_row = conn.execute(
                "SELECT data, started_at FROM task_runs WHERE task_id=?"
                " ORDER BY started_at DESC LIMIT 1",
                (tid,),
            ).fetchone()
            run = json.loads(run_row[0]) if run_row and run_row[0] else {}
            # ⚠️ started_at 必须是**能转 float 的**值：真实库是 REAL，但脏数据/历史遗留
            # 可能是 TEXT（如旧 fixture 写的 'x'）。此处裸 float() 会抛 ValueError，
            # 而异常会被本函数的 except 吞掉 ⇒ **整个生产者静默返回空**（连真故障也不报）。
            # 故逐条 try，解析不了就当作「没有 started_at」而非致命错误。
            last_started = None
            if run_row and run_row[1] is not None:
                try:
                    last_started = float(run_row[1])
                except (TypeError, ValueError):
                    last_started = None
            # 「本次已开跑」= started_at 晚于最近一次应跑时刻（_cron_missed 内部同口径）。
            # 用 max(last_run, last_started) 参与判定：谁新用谁，
            # 这样既认「已跑完」也认「正在跑」，运行期间不再误报。
            ran_at = max([x for x in (last_run, last_started) if x] or [None])
            # ⚠️ 2026-10-10 补漏判据（方案 B 自身的漏洞，恢复 automation 时实测抓到）：
            # 判据②「今天零产出」原本**只**看今天 timeline 有无记录，却没先确认
            # 「今天到底该不该跑」。实测 10-10 06:49（任务 09:30/09:40/10:00/10:30/16:00
            # 才到点、最近一次全是 10-09），5 个任务被**全部**报成「今天零产出」——
            # 它们连跑都还没跑。这是缺陷①「用 last_run 判运行中」的同型错误：
            # 换了字段，但同样没问「此刻该不该已经有产出」。
            # 故②的前置条件改为：**今天应跑时刻已过**，且本次已开跑（ran_at 在今天应跑点之后）。
            # 两者都不满足 → 今天本来就没到点，什么都不报。
            due_today = _cron_due_today(cron, now)
            ran_today = bool(ran_at) and _today() == _iso(ran_at)[:10]
            # ① 该跑却没跑成
            if cron and _cron_missed(cron, ran_at, now):
                items.append({
                    "kind": "task_anomaly",
                    "content": f"今天「{title}」该跑却还没跑成，要不要去查一下？",
                    "evidence": f"cron={cron}; last_run={_iso(last_run)}; last_status={last_status}",
                    "confidence": 0.82,
                    "anchor": f"{tid}:{_today()}:miss",
                })
                continue
            # ② 跑了但零产出（status=ok 但无真实产出）
            #
            # ⚠️ 2026-10-09 修误报（缺陷②③）：原判据两处都错——
            #   缺陷②：锚点分支写 ``task_type in ("daily_reading", "progress_tracking")``，
            #     而实测 5 个启用任务的 task_type **全是 None** ⇒ 该分支对所有任务永不触发，
            #     timeline 里明明有正确记录却认不出来。改用标题冒号前缀匹配 task_name。
            #   缺陷③：零产出靠 ``_hit_fail_words`` 关键词表 —— 该表含「抱歉/失败/无法/火山/
            #     额度」，而**成功**的汇报原文就会写「不擅自删除」这类措辞 ⇒ 成功被误判失败；
            #     且它假设「产出必进 artifacts」，但读书任务 artifacts 恒为 []（正文写进
            #     .deskmate/tmp_reading/）。关键词判据**整条删除**，只认硬锚点。
            status = run.get("status") or last_status
            if status == "ok" and due_today and ran_today:
                zero = False
                reason = ""
                stem = _title_stem(title)
                if not _timeline_has_today(base_dir, stem):
                    zero = True
                    reason = f"timeline.jsonl {_today()} 无「{stem}」记录"
                if zero:
                    items.append({
                        "kind": "task_anomaly",
                        "content": f"「{title}」今天回执 ok，但 timeline 里查无实产，得留意。",
                        "evidence": (
                            f"last_run={_iso(last_run)} status=ok; {reason}"
                        ),
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
    if not _throttled(base_dir, "producer:sync_health", 3600, source="cloud_audit", kind="window_failed", window_s=3600):
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
    if not _throttled(base_dir, "producer:writing", 3600 * 24, source="cloud_audit", kind="stall_flag", window_s=3600 * 24):  # 每日至多扫一次
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
