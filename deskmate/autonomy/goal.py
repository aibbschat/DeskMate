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

import time
from typing import Any, Iterable, Optional

NO_CHANGE = "[[NO_CHANGE]]"
STATE_MARK = "[[STATE]]"
_MAX_STATE = 200
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
    state = str(prev.get("state") or "").strip() or "（无，这是第一次检查）"
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
        "不要创建或修改任何定时任务。"
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
