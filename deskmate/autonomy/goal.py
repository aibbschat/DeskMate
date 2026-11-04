"""目标制任务 —— 让一次「盯到变化再报告」跨多次定时触发持续下去。

施工图 §4 B2 改道方案：不补 selfwake 接线（``_build_task_engine`` 不传
``wake_store``，定时任务里四工具不注册），改用现有 scheduler cron 重复触发
＋ ``TaskRun.checkpoint`` 承载跨轮状态。

职责边界（刻意做小）：
* **纯函数 + 显式传参**，不碰数据库、不碰引擎、不碰权限；
* 启用是「下达时显式声明」：``ScheduledTask.goal_mode`` 非空才生效，
  默认关闭 —— 不预设任务类别，也不预设可访问域名（D1/D2）；
* 无变化时静默（不投台账、不发事件），变化时正常送达并结案；
* 结案后 cron 仍会触发，但本模块给出 ``skipped`` 判定，不重复打扰。

输出协议由 opening 注入，模型只需在「无变化」时回两行标记：
``[[NO_CHANGE]]`` 与 ``[[STATE]]<本轮状态摘要>``。没给 NO_CHANGE 一律
按「有结果」处理 —— 宁可多投一次，绝不漏投（fail-safe）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterable, Optional

NO_CHANGE = "[[NO_CHANGE]]"
STATE_MARK = "[[STATE]]"
#: 模型自述卡点。连续卡住到 STUCK_GIVE_UP 轮 → 投一次卡点并结案，不再空转。
STUCK_MARK = "[[STUCK]]"
STUCK_GIVE_UP = 3
STUCK_ESCALATE = 2
FLAT_HINT = 3
ATTEMPT_LIMIT = 5
_MAX_STATE = 200
_STEP_MAX = 80
_RESULT_MAX = 160
_OFF = {"", "off", "false", "0", "none", "no"}


def goal_enabled(task: Any) -> bool:
    """本次下达是否声明了目标制运行。缺省 / 关闭值一律 False（零行为变化）。"""
    value = str(getattr(task, "goal_mode", "") or "").strip().lower()
    return bool(value) and value not in _OFF


def parse_quiet(text: str) -> tuple[bool, str]:
    """解析本轮结果：``(是否无变化, 本轮状态摘要)``。

    只有明确出现 ``NO_CHANGE`` 才静默；``STATE`` 缺失时返回空串，由
    ``record_check`` 沿用上一轮状态，绝不让状态链断掉。
    """
    raw = text or ""
    if NO_CHANGE not in raw:
        return False, ""
    state = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith(STATE_MARK):
            state = stripped[len(STATE_MARK):].strip()[:_MAX_STATE]
            break
    return True, state


def build_goal_opening(previous: Optional[dict]) -> str:
    """注入 opening 的目标制协议。带上轮状态，模型才知道「变没变」。"""
    prev = previous or {}
    raw_state = str(prev.get("state") or "").strip()
    if raw_state:
        state = raw_state
    elif prev.get("attempts"):
        state = "（无状态摘要，但下方有前几轮尝试记录）"
    else:
        state = "（无，这是第一次检查）"
    checked = prev.get("last_checked")
    when = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(float(checked)))
        if isinstance(checked, (int, float)) and checked
        else "（无）"
    )
    return (
        "\n\n--- 目标制任务（本次下达启用）---\n"
        "这是一个可以跨天持续的目标，本轮只是其中一次复查，不必一次做完。\n"
        f"上轮状态：{state}\n上轮检查时间：{when}\n"
        "本轮判断：\n"
        f"· 目标状态没有实质变化：只输出一行 {NO_CHANGE}，再输出一行 "
        f"{STATE_MARK}<本轮状态摘要，不超过 200 字，供下次比对>。"
        "不要写报告、不要总结——本轮不会打扰用户，任务会在下次计划时间继续。\n"
        "· 目标状态发生了实质变化：给出变化点、原文链接和你的一句判断，"
        "正常结束本轮。这一轮结果会送达用户并结案，之后不再复查。\n"
        f"· 试了几次都推进不了：以 {STUCK_MARK}<一句话说明缺什么才能继续> 开头回应，"
        "不要反复做同样的事。\n"
        "不要创建或修改任何定时任务。"
        + _stuck_hint(prev)
        + _attempts_block(prev)
    )


def goal_of(run: Any) -> Optional[dict]:
    """取 run 的 goal 子状态；普通 run / 脏数据返回 None。"""
    cp = getattr(run, "checkpoint", None)
    if isinstance(cp, dict) and isinstance(cp.get("goal"), dict):
        return cp["goal"]
    return None


def _in_generation(run: Any, generation: Optional[int]) -> bool:
    """代次过滤（§C1）。``generation=None`` = 不过滤，保持 B2 语义与旧调用全绿。

    旧库 run 没有 ``goal_generation`` 字段，一律按 0 处理 —— 与所有既有
    任务的默认代次相同，因此「不传 generation」和「传 0」都不改变历史行为。
    """
    if generation is None:
        return True
    try:
        stamp = int(getattr(run, "goal_generation", 0) or 0)
        target = int(generation)
    except (TypeError, ValueError):
        return False
    return stamp == target


def previous_goal_state(runs: Iterable[Any], *, generation: Optional[int] = None) -> dict:
    """最近一轮**同代次**带 goal 状态的快照（``store.runs()`` 按 started_at DESC）。

    不排除 ``status=running`` 且 ``finished_at=None`` 的 run —— 那正是
    kill -9 后留下的中断检查点，必须能被新一轮继承（崩溃续跑）。
    传 ``generation`` 时只认该代，重启目标后不会继承上一代的状态摘要。
    """
    for run in runs or []:
        if not _in_generation(run, generation):
            continue
        state = goal_of(run)
        if state is not None:
            return dict(state)
    return {}


def is_concluded(runs: Iterable[Any], *, generation: Optional[int] = None) -> bool:
    """最近一轮**同代次**带 goal 状态的 run 是否已结案。结案后本轮应 skipped。

    §C1：``skipped`` run 自己也带 ``done=True``，若不按代次过滤，已结案目标
    重启后会形成自续链永远 skipped。这里绝不改历史 run 的 ``done``。
    """
    for run in runs or []:
        if not _in_generation(run, generation):
            continue
        state = goal_of(run)
        if state is not None:
            return bool(state.get("done"))
    return False


def _checkpoint(run: Any) -> dict:
    cp = getattr(run, "checkpoint", None)
    if not isinstance(cp, dict):
        cp = {}
        run.checkpoint = cp
    return cp


def record_check(run: Any, *, state: str, now: float) -> dict:
    """记一次静默检查。合并而非覆盖，保住 ``last_iteration`` 等检查点键。"""
    cp = _checkpoint(run)
    goal = dict(cp.get("goal") or {})
    fresh = str(state or "").strip()[:_MAX_STATE]
    if fresh:
        goal["state"] = fresh          # 没给 STATE 就沿用上轮，状态链不断
    goal.setdefault("state", "")
    goal["last_checked"] = now
    goal["done"] = False
    cp["goal"] = goal
    return goal


def record_concluded(run: Any) -> dict:
    """变化已送达，标记结案，后续 cron run 不再复查。"""
    cp = _checkpoint(run)
    goal = dict(cp.get("goal") or {})
    goal["done"] = True
    cp["goal"] = goal
    return goal


# -- 自主循环：跨轮尝试记忆 / 卡点升级 -----------------------------------------
def parse_stuck(text: str) -> tuple[bool, str]:
    """解析模型自述的卡点：(是否卡住, 卡在哪/缺什么)。

    只认显式标记，不做关键词猜测——「我觉得有点卡」不算，避免把正常等待
    误判成失败。连续卡住到 STUCK_GIVE_UP 轮由 manager 投递并结案。
    """
    raw = text or ""
    if STUCK_MARK not in raw:
        return False, ""
    tail = raw.split(STUCK_MARK, 1)[1]
    lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    reason = (lines[0] if lines else "").strip()[:_MAX_STATE]
    return True, reason


def _brief(value: Any, limit: int) -> str:
    if isinstance(value, (dict, list)):
        try:
            value = json.dumps(value, ensure_ascii=False)
        except Exception:  # noqa: BLE001 - 纯展示，绝不因序列化炸掉主流程
            value = str(value)
    return " ".join(str(value or "").split())[:limit]


def attempt_from_messages(
    messages: Any, *, result: str = "", error: str = "", at: float = 0.0
) -> dict:
    """把本轮真实消息压成一条尝试记录：调了什么工具、结论是什么、错在哪。

    这是跨轮记忆的原料。没有它，下一轮只能对着同一份 opening 重做一遍，
    永远学不到「上次这个做法已经失败」。只做摘要，不存全文。
    """
    steps: list[str] = []
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            name = str(fn.get("name") or "?")
            args = _brief(fn.get("arguments"), _STEP_MAX)
            steps.append(f"{name}({args})" if args else name)
    return {
        "at": float(at or 0.0),
        "steps": steps[-4:],
        "result": _brief(result, _RESULT_MAX),
        "error": _brief(error, _RESULT_MAX),
    }


def note_attempt(
    run: Any,
    *,
    outcome: str,
    attempt: Optional[dict] = None,
    state: str = "",
    blocker: str = "",
    error: str = "",
    now: Optional[float] = None,
) -> dict:
    """记一轮结果并维护卡住/原地踏步计数。

    合并而非覆盖（保住 last_iteration 等检查点键）。outcome：
    quiet（无变化）/ changed（有变化）/ error（执行失败）/ stuck（模型自述卡住）。
    """
    cp = _checkpoint(run)
    goal = dict(cp.get("goal") or {})
    prev_state = str(goal.get("state") or "")
    item = dict(attempt or {})
    item["outcome"] = str(outcome or "")
    if error and not item.get("error"):
        item["error"] = _brief(error, _RESULT_MAX)
    if blocker:
        item["blocker"] = _brief(blocker, _MAX_STATE)
    attempts = list(goal.get("attempts") or [])
    attempts.append(item)
    goal["attempts"] = attempts[-ATTEMPT_LIMIT:]
    goal["last_outcome"] = item["outcome"]
    fresh = str(state or "").strip()[:_MAX_STATE]
    if fresh:
        goal["state"] = fresh          # 没给 STATE 就沿用上轮，状态链不断
    goal.setdefault("state", "")
    if outcome in ("stuck", "error"):
        goal["stuck_rounds"] = int(goal.get("stuck_rounds") or 0) + 1
        goal["error_rounds"] = int(goal.get("error_rounds") or 0) + (
            1 if outcome == "error" else 0
        )
        goal["done"] = False
        if blocker:
            goal["last_blocker"] = str(blocker).strip()[:_MAX_STATE]
    else:
        goal["stuck_rounds"] = 0
        goal["error_rounds"] = 0
    # 未结案前 done 恒为 False；结案由 manager 在投递成功之后显式写入。
    goal.setdefault("done", False)
    # 原地踏步只在「没有新状态 / 状态与上轮一模一样」时累计，正常等待不误判。
    if outcome == "quiet":
        if not fresh or (prev_state and fresh == prev_state):
            goal["flat_rounds"] = int(goal.get("flat_rounds") or 0) + 1
        else:
            goal["flat_rounds"] = 0
    else:
        goal["flat_rounds"] = 0
    goal["last_checked"] = now if now is not None else time.time()
    cp["goal"] = goal
    return goal


def _attempts_block(previous: dict) -> str:
    """把前几轮的尝试摊给模型看——这是「会拐弯」的前提。"""
    attempts = previous.get("attempts") or []
    if not attempts:
        return ""
    lines = ["", "--- 你之前几轮的尝试（已经失败的做法不要再原样重复）---"]
    for a in attempts[-3:]:
        if not isinstance(a, dict):
            continue
        ts = ""
        try:
            at = float(a.get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        if at:
            ts = time.strftime("%m-%d %H:%M", time.localtime(at))
        steps = "；".join(str(s) for s in (a.get("steps") or [])) or "（没有工具动作）"
        line = f"· {ts} [{a.get('outcome') or '?'}] {steps}"
        if a.get("result") and not a.get("blocker"):
            line += f" → {str(a['result'])[:80]}"
        if a.get("error"):
            line += f" → 失败：{str(a['error'])[:80]}"
        if a.get("blocker"):
            line += f" → 卡在：{str(a['blocker'])[:80]}"
        lines.append(line)
    lines.append(
        "换一种路径/工具/说法再试；如果确实无路可走，"
        f"用 {STUCK_MARK}<缺什么才能继续> 说明。"
    )
    return "\n".join(lines)


def _stuck_hint(previous: dict) -> str:
    stuck = int(previous.get("stuck_rounds") or 0)
    flat = int(previous.get("flat_rounds") or 0)
    parts: list[str] = []
    if stuck >= STUCK_ESCALATE:
        parts.append(
            f"你已经连续 {stuck} 轮卡住。本轮不要再重复前几轮的动作：换一种做法；"
            f"如果确实推进不了，直接以 {STUCK_MARK}<一句话说明缺什么才能继续> 回应。"
        )
    elif stuck >= 1:
        blocker = str(previous.get("last_blocker") or "").strip()
        tip = f"（{blocker[:100]}）" if blocker else ""
        parts.append(
            f"上一轮你没成功（卡住或执行失败{tip}）。本轮优先解决它；"
            f"解决不了就用 {STUCK_MARK} 说明需要什么。"
        )
    if flat >= FLAT_HINT:
        parts.append(
            f"已经连续 {flat} 次看到同样的观察结果。如果这是正常等待就照常继续；"
            "如果目标前提已经坏了（页面没了/账号失效/目标写错），"
            f"不要再空转，用 {STUCK_MARK} 说明。"
        )
    return ("\n" + "\n".join(parts)) if parts else ""
