"""小桔调用专家（2026-11-09；2026-11-17 改为后台任务）。

设计边界（萧哥拍板）：
- 小桔是主体，知道专家存在、可以派活；专家**看不到**小桔的记忆，小桔也**不读**专家笔记本；
- **派发即返回**：专家在后台跑，不占用小桔这一回合（2026-11-17 根因治理，见下）；
- 简单问题 1 位，复杂/多问题 2 位，最多 3 位；每位专家一个线程并行；
- 专家可读全书（只读 read_book），可写自己的笔记本；不写正文、不碰小桔记忆；
- 咨询记录落 ~/.deskmate/consultations/ 审计，不进小桔长期记忆；咨询轮不自动抽记忆。

## 为什么改成后台（2026-11-17）

原实现是**同步**工具：跑完才返回，总预算 240 秒。实盘两次咨询、两次超时，超时的都是
人物专家——它要通读 13.7 万字并逐字引证，8 轮 × 40~60 秒，240 秒结构上必然不够；而且
超时是 `raise`，七轮中间产物一个字都不留；这 4 分钟里小桔整轮卡住，用户只觉得「调不动」。

把预算调大只是把一个死结拉长。真正的病是**同步**：一个需要 8 分钟的活，被塞进一个
必须 4 分钟内结束的回合。所以：

1. 工具立刻返回「已派出」，小桔先跟萧盛说一句，继续手上的事；
2. 专家在后台线程里跑，单次预算放宽到 15 分钟（`DESKMATE_CONSULT_TIMEOUT`）；
3. 进度落 `consult_runs.json`（原子写），前端据此显示「专家正在工作 · 已 3:12」，
   不让用户空等；
4. 跑完落审计 + 通知 manager 投递回会话，小桔被唤醒后用自己的话汇报；
5. 超时/失败不再清空：已产出的部分原样保留，标注截断。

状态文件是**唯一事实源**：前端只读它，投递与去重也认它，多个入口不会各说一套。
"""
from __future__ import annotations

import asyncio
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

MAX_EXPERTS = 3

#: 每位专家的总时间预算（秒）。后台跑不再占用对话回合，所以给足：8 轮 × 60s 的最坏情况
#: 约 480s，15 分钟留了一倍余量。env `DESKMATE_CONSULT_TIMEOUT` 可覆盖（调用时解析）。
DEFAULT_TIMEOUT = 900

#: 同一时刻最多几个咨询任务（每个任务内部再并行 1-3 位专家）。超过就拒绝，
#: 避免一次派活把机器和通道打满。
MAX_CONCURRENT_RUNS = 4

#: 状态文件保留多少条历史（running 的不受限制）。
RUNS_KEEP = 20

#: 可调用专家目录（名字 / 擅长 / 什么时候叫）。
EXPERTS: tuple[dict[str, str], ...] = (
    {
        "id": "lit-editor",
        "name": "文学编辑",
        "best": "情节、结构、语言、人物逻辑",
        "when": "判断这段/这个情节对不对、写得好不好、人物是否可信",
    },
    {
        "id": "brainstormer",
        "name": "脑洞专家",
        "best": "发散方向、反转、钩子",
        "when": "下一步怎么走、需要几条真正不同的方向",
    },
    {
        "id": "outline",
        "name": "大纲专家",
        "best": "卷纲、章纲、主线支线、节奏",
        "when": "大纲/章序/结构骨架/主线支线问题",
    },
    {
        "id": "character",
        "name": "人物专家",
        "best": "人物弧光、动机、关系、口吻、OOC",
        "when": "某人的行为是否合理、是否跑偏、关系是否可信、人物下一步",
    },
    {
        "id": "adaptation",
        "name": "影视改编",
        "best": "影视化、分集、改编可行性",
        "when": "能不能改、怎么改、IP/AI剧/版权方向",
    },
)
EXPERT_IDS = frozenset(item["id"] for item in EXPERTS)
_NAME_TO_ID = {item["name"]: item["id"] for item in EXPERTS}
_NAME = {item["id"]: item["name"] for item in EXPERTS}


def directory_text() -> str:
    lines = ["# 你可以调用专家（他们看不到你的记忆）"]
    for item in EXPERTS:
        lines.append(
            f"- {item['name']}（{item['id']}）：{item['best']}。什么时候叫：{item['when']}。"
        )
    return "\n".join(lines)


CONSULT_GUIDANCE = directory_text() + """

用法：consult_experts(experts=["lit-editor"], brief="自包含的任务简报", book="夜不收")
- 简单问题叫 1 位；复杂/多问题叫 2 位；最多 3 位。自动派，不用问萧盛。
- 简报要自包含：书、章节/范围、卡在哪、要他回答什么。不要把你的记忆或你和萧盛的聊天私密内容塞进去；他们看不到。
- 🔴 **这是后台任务，调用会立刻返回**：不要等结果、不要连续重派、不要把「已派出」当成结论。
  派完先对萧盛说一句「我请 X 看了，回头跟你说」，然后继续你手上别的事。
  口径上别说「稍等，马上就好」——专家要读稿，是几分钟的事，别让他干等。
- 结果回来后系统会给你一条新消息（含专家原文）。那时再用**你自己的话**向萧盛汇报：
  标明是谁的判断，不要照搬，不要把专家原话当事实。
- 咨询结果不会自动进你的长期记忆；汇报后你可以把「咨询了谁 + 结论 + 萧盛采纳/驳回」记一条。
"""


# ---------------------------------------------------------------------------
# 状态文件（唯一事实源）
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()
_ACTIVE: dict[str, threading.Thread] = {}
_NOTIFIER: Optional[Callable[[str, dict], None]] = None


def _runs_path() -> Path:
    return Path(
        os.environ.get("DESKMATE_CONSULT_RUNS")
        or (Path.home() / ".deskmate" / "consult_runs.json")
    )


def _consult_dir() -> Path:
    return Path(
        os.environ.get("DESKMATE_CONSULT_DIR")
        or (Path.home() / ".deskmate" / "consultations")
    )


def _timeout() -> int:
    raw = os.environ.get("DESKMATE_CONSULT_TIMEOUT", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def _read_state() -> dict[str, Any]:
    import json

    path = _runs_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 文件缺失/损坏都当空账
        return {"version": 1, "runs": []}
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        return {"version": 1, "runs": []}
    return data


def _write_state(state: dict[str, Any]) -> None:
    import json

    from deskmate.growth._atomic import atomic_write_text

    runs = [r for r in state.get("runs", []) if isinstance(r, dict)]
    running = [r for r in runs if r.get("status") == "running"]
    # running 一条都不能丢（前端靠它显示"正在工作"）；已结束的只留最近 RUNS_KEEP 条。
    finished = sorted(
        (r for r in runs if r.get("status") != "running"),
        key=lambda r: r.get("started_at") or 0,
        reverse=True,
    )[:RUNS_KEEP]
    kept = running + finished
    atomic_write_text(
        _runs_path(), json.dumps({"version": 1, "runs": kept}, ensure_ascii=False, indent=2)
    )


def _reap_zombies(state: dict[str, Any]) -> bool:
    """上个进程留下的 running：线程已经不在了，标成 interrupted。

    不做这一步的话，界面上会永远挂着一个「专家正在工作」，而背后根本没有线程——
    正是本仓最忌讳的静默说谎。
    """
    changed = False
    for run in state.get("runs", []):
        if not isinstance(run, dict) or run.get("status") != "running":
            continue
        rid = str(run.get("id") or "")
        live = _ACTIVE.get(rid)
        if live is not None and live.is_alive():
            continue
        run["status"] = "interrupted"
        run["finished_at"] = run.get("finished_at") or time.time()
        run["error"] = run.get("error") or "进程重启，任务中断（未产出结果）"
        changed = True
    return changed


def _mutate(fn: Callable[[dict[str, Any]], Any]) -> Any:
    """读-改-写状态文件（进程内加锁 + 原子替换）。

    总是落盘：`fn` 可能只是原地改了某个 run（返回 None），若按返回值决定写不写，
    任务完成就不会落盘——那正是"进度永远停在 running"的成因。
    """
    with _LOCK:
        state = _read_state()
        _reap_zombies(state)
        out = fn(state)
        _write_state(state)
        return out


def _find(state: dict[str, Any], run_id: str) -> Optional[dict[str, Any]]:
    for run in state.get("runs", []):
        if isinstance(run, dict) and run.get("id") == run_id:
            return run
    return None


def _mark_delivered(run_id: str) -> bool:
    """把一条结果标成「已投递给小桔」。返回 True 表示本次抢到了投递权（幂等去重）。"""
    claimed = {"ok": False}

    def _do(state: dict[str, Any]) -> None:
        run = _find(state, run_id)
        if run is not None and not run.get("delivered"):
            run["delivered"] = True
            run["delivered_at"] = time.time()
            claimed["ok"] = True

    _mutate(_do)
    return claimed["ok"]


def _unmark_delivered(run_id: str) -> None:
    """投递失败（通道抛错）时把标记还回去，让 tick 兜底重投。"""

    def _do(state: dict[str, Any]) -> None:
        run = _find(state, run_id)
        if run is not None:
            run["delivered"] = False
            run.pop("delivered_at", None)

    _mutate(_do)


def claim_delivery(run_id: str) -> bool:
    """公开的投递所有权抢占（manager 用）：True = 这次抢到了，由你投。"""
    return _mark_delivered(run_id)


def release_delivery(run_id: str) -> None:
    """投递失败时归还所有权，交给下一轮兜底。"""
    _unmark_delivered(run_id)


def _notification_payload(run: dict[str, Any]) -> dict[str, Any]:
    """给前端/投递用的精简视图：不带全文（全文在审计文件与投递消息里）。"""
    results = run.get("results") or []
    return {
        "id": run.get("id"),
        "session_id": run.get("session_id"),
        "book": run.get("book") or "",
        "model": run.get("model") or "",
        "status": run.get("status"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "elapsed": run.get("elapsed"),
        "experts": run.get("experts") or [],
        "expert_names": run.get("expert_names") or [],
        "results": [
            {
                "expert": r.get("expert"),
                "name": r.get("name"),
                "status": r.get("status"),
                "chars": len(str(r.get("text") or "")),
                "elapsed": r.get("elapsed"),
            }
            for r in results
            if isinstance(r, dict)
        ],
        "audit_path": run.get("audit_path") or "",
        "excerpt": str((results[0] or {}).get("text") or "")[:400] if results else "",
        "error": run.get("error") or "",
    }


def read_runs(*, session_id: Optional[str] = None) -> dict[str, Any]:
    """接口层：活动中的 + 最近完成的。只读，顺手清僵尸。"""
    with _LOCK:
        state = _read_state()
        if _reap_zombies(state):
            _write_state(state)
    runs = [r for r in state.get("runs", []) if isinstance(r, dict)]
    if session_id:
        runs = [r for r in runs if r.get("session_id") in (None, session_id)]
    active = []
    recent = []
    for run in runs:
        item = _notification_payload(run)
        if run.get("status") == "running":
            item["elapsed"] = round(time.time() - float(run.get("started_at") or time.time()), 1)
            active.append(item)
        else:
            recent.append(item)
    recent.sort(key=lambda r: r.get("finished_at") or 0, reverse=True)
    return {"active": active, "recent": recent[:10]}


def pending_delivery(session_id: str) -> list[dict[str, Any]]:
    """已完成但还没告诉小桔的任务（完成时 engine 不在、app 重启等场景的兜底）。"""
    return pending_by_session().get(session_id, [])


def pending_by_session() -> dict[str, list[dict[str, Any]]]:
    """一次读完所有待投递，按会话分组。

    调度器每 30s 扫一遍；若按会话逐个读，几十个会话就是几十次文件读。分组返回后
    调用方只需处理「有引擎的那些」，其余留着下次再试。
    """
    with _LOCK:
        state = _read_state()
        if _reap_zombies(state):
            _write_state(state)
    out: dict[str, list[dict[str, Any]]] = {}
    for run in state.get("runs", []):
        if not isinstance(run, dict):
            continue
        if run.get("status") == "running" or run.get("delivered"):
            continue
        sid = str(run.get("session_id") or "")
        if not sid:
            continue
        out.setdefault(sid, []).append(run)
    for runs in out.values():
        runs.sort(key=lambda r: r.get("finished_at") or 0)
    return out


def set_notifier(fn: Optional[Callable[[str, dict], None]]) -> None:
    """注册进程内通知器（manager 用）：`(kind, payload)`，kind ∈ {started, finished}。

    payload 是**完整**任务记录（含专家全文）——投递消息需要原文；广播前由调用方用
    :func:`public_view` 裁成适合前端的形状，避免把几万字塞进 WS。
    线程里调用，实现方负责切回事件循环；这里只保证「通知失败绝不影响咨询本身」。
    """
    global _NOTIFIER
    _NOTIFIER = fn


def public_view(run: dict[str, Any]) -> dict[str, Any]:
    """给前端/WS 的视图：不带专家全文，只带摘要与计数。"""
    return _notification_payload(run)


def _notify(kind: str, run: dict[str, Any]) -> None:
    fn = _NOTIFIER
    if fn is None:
        return
    try:
        fn(kind, dict(run))
    except Exception:  # noqa: BLE001 —— 通知是锦上添花，不能拖垮任务
        pass


# ---------------------------------------------------------------------------
# 专家引擎
# ---------------------------------------------------------------------------


def _normalize_experts(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [p for p in raw.replace("，", ",").replace("、", ",").split(",") if p.strip()]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        key = str(item).strip()
        expert_id = _NAME_TO_ID.get(key, key)
        if expert_id and expert_id not in out:
            out.append(expert_id)
    return out


def _build_expert_engine(expert_id: str, provider: Any, model: str, shelf: Any) -> Any:
    from coworker.agent import build_engine
    from coworker.permissions import Mode
    from coworker.personas.registry import get_registry
    from deskmate.bots.reading import make_reading_tools

    agent = get_registry().get(expert_id).agent()
    engine = build_engine(
        agent=agent,
        provider=provider,
        model=model,
        workspace=None,
        mode=Mode.BYPASS_APPROVALS,
        extra_tools=list(make_reading_tools(shelf)),
    )
    # 咨询里不允许弹问询卡片，也不允许沉淀 skill：专家只交意见，问题由小桔对萧盛说。
    engine.registry._tools.pop("ask_user", None)
    engine.registry._tools.pop("save_skill", None)
    return engine


def _prompt(expert_id: str, brief: str, book: str) -> str:
    name = _NAME.get(expert_id, expert_id)
    book_line = f"《{book}》" if book else "未指定（可以先 list_books 查书架）"
    return (
        f"你是「{name}」。萧盛让小桔（他的主助手）请你独立看一个问题。你只给专家意见，"
        "不直接对话、不反问、不写正文。\n"
        f"书：{book_line}\n"
        "任务简报：\n"
        f"{brief}\n\n"
        "输出要求：中文；结论先行；给依据（章节/人物/设定）；不确定的标出来；"
        "不要写成品正文。"
    )


async def _collect(engine: Any, prompt: str, timeout: int) -> tuple[str, str]:
    """跑一轮专家咨询，返回 (文本, 状态)。

    **超时不再清空**：已经流出来的分析原样返回并标 `timeout`——七轮白烧是最贵的浪费，
    而这七轮本身往往已经够小桔用了（2026-11-17 实盘：8 轮跑到第 8 轮才被掐）。
    """
    from coworker.events import EventType

    chunks: list[str] = []

    async def consume() -> None:
        async for event in engine.run(prompt):
            if event.type == EventType.ASSISTANT_MESSAGE:
                chunk = (event.data or {}).get("text") or ""
                if chunk:
                    chunks.append(chunk)

    try:
        await asyncio.wait_for(consume(), timeout=timeout)
        return (chunks[-1] if chunks else ""), "ok"
    except asyncio.TimeoutError:
        try:
            engine.request_interrupt()
        except Exception:  # noqa: BLE001
            pass
        partial = next((c for c in reversed(chunks) if c.strip()), "")
        return partial, "timeout"


def _run_one(
    expert_id: str,
    brief: str,
    book: str,
    provider: Any,
    model: str,
    shelf: Any,
    timeout: int,
) -> tuple[str, str, float]:
    """返回 (状态, 文本, 耗时)。状态 ∈ {ok, timeout, error}。"""
    started = time.time()
    try:
        engine = _build_expert_engine(expert_id, provider, model, shelf)
        text, status = asyncio.run(_collect(engine, _prompt(expert_id, brief, book), timeout))
        text = text.strip()
        if status == "timeout":
            if text:
                text = f"{text}\n\n（注：专家超过 {timeout}s 预算被截断，以上是已产出的部分。）"
            else:
                text = f"【{_NAME.get(expert_id, expert_id)}】超过 {timeout}s 仍未产出任何内容。"
        return status, (text or "（专家没有产出文字）"), time.time() - started
    except Exception as exc:  # noqa: BLE001 —— 一位专家失败不拖垮整次咨询
        return (
            "error",
            f"【{_NAME.get(expert_id, expert_id)}】调用失败：{type(exc).__name__}: {exc}",
            time.time() - started,
        )


def _write_audit(
    results: list[dict[str, Any]],
    brief: str,
    book: str,
    run: dict[str, Any],
    base_dir: Optional[Path] = None,
) -> Optional[str]:
    try:
        root = Path(base_dir or _consult_dir())
        now = datetime.now()
        folder = root / now.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (now.strftime("%H%M%S") + "-" + "-".join(
            str(r.get("expert")) for r in results
        ) + ".md")
        lines = [
            f"# 专家咨询 · {now.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"- 书：{book or '未指定'}",
            f"- 专家：{'、'.join(str(r.get('name')) for r in results)}",
            f"- 任务：{run.get('id')}（{run.get('elapsed', 0):.0f}s，模型 {run.get('model') or '?'}）",
            "",
            "## 简报",
            "",
            brief,
            "",
        ]
        for r in results:
            flag = {"ok": "", "timeout": " · 超时截断", "error": " · 失败"}.get(
                str(r.get("status")), ""
            )
            lines += [f"## {r.get('name')}（{r.get('status')}{flag}）", "", str(r.get("text") or ""), ""]
        lines.append("> 专家意见仅供参考，不是小桔本人判断，也不自动进入小桔长期记忆。")
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)
    except Exception:  # noqa: BLE001 —— 审计落盘失败不能影响咨询
        return None


def _job_thread(
    run_id: str,
    chosen: list[str],
    brief: str,
    book: str,
    provider: Any,
    model: str,
    shelf: Any,
    timeout: int,
    audit_dir: Optional[Path],
) -> None:
    started = time.time()
    results: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
            futures = {
                pool.submit(_run_one, eid, brief, book, provider, model, shelf, timeout): eid
                for eid in chosen
            }
            for fut in as_completed(futures):
                eid = futures[fut]
                try:
                    status, text, elapsed = fut.result()
                except Exception as exc:  # noqa: BLE001
                    status, text, elapsed = "error", f"调用失败：{exc}", time.time() - started
                results.append(
                    {"expert": eid, "name": _NAME.get(eid, eid), "status": status,
                     "text": text, "elapsed": round(elapsed, 1)}
                )
        order = {eid: i for i, eid in enumerate(chosen)}
        results.sort(key=lambda r: order.get(str(r.get("expert")), 99))
        elapsed = time.time() - started
        statuses = {str(r.get("status")) for r in results}
        final = (
            "done"
            if statuses == {"ok"}
            else "timeout"
            if statuses <= {"ok", "timeout"} and "timeout" in statuses
            else "failed"
        )
        run = {
            "id": run_id,
            "status": final,
            "elapsed": round(elapsed, 1),
            "finished_at": time.time(),
            "results": results,
            "error": "",
        }
        audit_path = _write_audit(results, brief, book, run, audit_dir)
        run["audit_path"] = audit_path or ""

        def _finish(state: dict[str, Any]) -> Optional[dict[str, Any]]:
            target = _find(state, run_id)
            if target is None:
                return None
            target.update(run)
            return dict(target)

        snapshot = _mutate(_finish) or run
        _notify("finished", snapshot)
    except Exception as exc:  # noqa: BLE001 —— 线程兜底，绝不让任务悬在 running
        def _fail(state: dict[str, Any]) -> Optional[dict[str, Any]]:
            target = _find(state, run_id)
            if target is None:
                return None
            target["status"] = "failed"
            target["finished_at"] = time.time()
            target["elapsed"] = round(time.time() - started, 1)
            target["error"] = f"{type(exc).__name__}: {exc}"
            return dict(target)

        snapshot = _mutate(_fail)
        if snapshot:
            _notify("finished", snapshot)
    finally:
        with _LOCK:
            _ACTIVE.pop(run_id, None)


def _active_count() -> int:
    with _LOCK:
        return sum(1 for t in _ACTIVE.values() if t.is_alive())


def _new_run_id() -> str:
    return f"c-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def dispatch(
    *,
    chosen: list[str],
    brief: str,
    book: str,
    provider: Any,
    model: str,
    session_id: str = "",
    shelf: Any = None,
    audit_dir: Optional[Path] = None,
    timeout: Optional[int] = None,
) -> dict[str, Any]:
    """起一个后台咨询任务，立刻返回任务记录。校验由调用方负责。"""
    run_id = _new_run_id()
    budget = timeout or _timeout()
    now = time.time()
    run = {
        "id": run_id,
        "session_id": session_id,
        "experts": list(chosen),
        "expert_names": [_NAME.get(e, e) for e in chosen],
        "book": book or "",
        "brief_head": brief[:160],
        "status": "running",
        "started_at": now,
        "deadline_at": now + budget,
        "timeout": budget,
        "model": model,
        "delivered": False,
        "results": [],
        "error": "",
    }

    def _add(state: dict[str, Any]) -> None:
        state.setdefault("runs", []).append(run)

    with _LOCK:
        _mutate(_add)
        thread = threading.Thread(
            target=_job_thread,
            args=(run_id, list(chosen), brief, book, provider, model, shelf, budget, audit_dir),
            name=f"consult-{run_id}",
            daemon=True,
        )
        _ACTIVE[run_id] = thread
        thread.start()
    _notify("started", run)
    return dict(run)


def wait_for_jobs(timeout: float = 120.0) -> bool:
    """等当前所有咨询线程结束（测试与优雅退出用）。返回是否全部结束。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _LOCK:
            alive = [t for t in _ACTIVE.values() if t.is_alive()]
        if not alive:
            return True
        for t in alive:
            t.join(timeout=min(0.2, max(0.0, deadline - time.time())))
    return False


def _result_text(run: dict[str, Any]) -> str:
    """投递给小桔的正文：她在新一回合里看到的全部材料。"""
    results = [r for r in (run.get("results") or []) if isinstance(r, dict)]
    names = "、".join(str(r.get("name")) for r in results) or "、".join(
        str(n) for n in (run.get("expert_names") or [])
    )
    flags = {
        "ok": "",
        "timeout": "（超时，只交出了已产出的部分）",
        "error": "（失败）",
    }
    if results:
        body = "\n\n".join(
            f"## {r.get('name')}{flags.get(str(r.get('status')), '')}\n{r.get('text') or ''}"
            for r in results
        )
    else:
        # 中断/崩溃：一位都没产出。如实说清楚，否则小桔只能干等或瞎编。
        reason = str(run.get("error") or "任务中断，没有产出结果").strip()
        body = f"（本次没有产出任何意见：{reason}）"
    audit = run.get("audit_path")
    tail = f"\n\n（审计：{audit}）" if audit else ""
    return (
        f"【专家任务结束】{names or '专家'}·《{run.get('book') or '未指定书'}》"
        f"，用时 {float(run.get('elapsed') or 0):.0f}s。{tail}\n\n{body}\n\n"
        "（以上是专家意见原文，仅供参考。请用你自己的话向萧盛汇报：结论先行、标明是谁的判断；"
        "不要照搬，不要把专家原话当事实。若还缺关键信息，可以再派一次。）"
    )


def result_message(run: dict[str, Any]) -> str:
    """公开给 manager 的投递正文构造器。"""
    return _result_text(run)


def make_consult_tool(
    *,
    provider: Any,
    model: str,
    timeout: Optional[int] = None,
    on_consulted: Optional[Callable[[], None]] = None,
    shelf: Any = None,
    audit_dir: Optional[Path] = None,
    session_id: str = "",
):
    """给小桔的 consult_experts 工具（**后台派发，立即返回**）。"""

    def consult_experts(experts: list, brief: str, book: str = "") -> str:
        """让专家在后台独立看一个问题，立刻返回派出回执。

        experts: 1-3 位专家 id 或名字，如 ["lit-editor", "brainstormer"]。
        brief: 自包含的任务简报（书/章节/卡点/要他们回答什么）。
        book: 书名（如 "夜不收"）；不填则专家自己查书架。
        专家看不到小桔的记忆；结果回来后系统会再给你一条消息，届时再向萧盛汇报。
        """
        chosen = _normalize_experts(experts)
        if not chosen:
            return "没指定专家。可用：" + "、".join(f"{i['name']}({i['id']})" for i in EXPERTS)
        unknown = [e for e in chosen if e not in EXPERT_IDS]
        if unknown:
            return "没有这些专家：" + "、".join(unknown) + "。可用：" + "、".join(sorted(EXPERT_IDS))
        if len(chosen) > MAX_EXPERTS:
            return f"一次最多 {MAX_EXPERTS} 位专家；你给了 {len(chosen)} 位。"
        clean = str(brief or "").strip()
        if len(clean) < 8:
            return "简报太短（至少 8 个字）：写清书/章节/卡点/要专家回答什么。"
        if provider is None:
            return "当前没有可用的模型通道，叫不了专家。"

        from deskmate.bots.reading import default_shelf

        sh = shelf or default_shelf()
        book_title = str(book or "").strip()
        if book_title:
            try:
                if sh.resolve(book_title) is None:
                    return f"找不到《{book_title}》这本书；先用 list_books 对一下书名。"
            except Exception:  # noqa: BLE001
                return f"找不到《{book_title}》这本书；先用 list_books 对一下书名。"

        if _active_count() >= MAX_CONCURRENT_RUNS:
            return (
                f"现在已经有 {MAX_CONCURRENT_RUNS} 个专家任务在跑（后台），先别再派："
                "等结果回来、或等其中一位结束再派。别把同一件事重派一遍。"
            )

        run = dispatch(
            chosen=chosen,
            brief=clean,
            book=book_title,
            provider=provider,
            model=model,
            session_id=session_id,
            shelf=sh,
            audit_dir=audit_dir,
            timeout=timeout,
        )
        if on_consulted is not None:
            try:
                on_consulted()
            except Exception:  # noqa: BLE001
                pass
        names = "、".join(_NAME.get(e, e) for e in chosen)
        budget = run.get("timeout") or _timeout()
        where = f"《{book_title}》" if book_title else "稿子（自己查书架）"
        return (
            f"【已派出】{names} 在后台看{where}，任务 {run['id']}。\n"
            "**不要等结果**（这是后台任务，不占你的回合）：先跟萧盛说一句你请了谁、"
            "在从什么角度看，然后继续你手上的事。\n"
            f"结果回来时系统会给你一条新消息（含专家原文），那时再用自己的话汇报。"
            f"预计几分钟内；超过 {budget // 60} 分钟没回音才算超时，届时可以再派一次——"
            "但绝对不要反复重派同一件事。"
        )

    return consult_experts
