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
import functools
import logging
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

MAX_EXPERTS = 3

#: 每位专家的总时间预算（秒）。后台跑不再占用对话回合，所以给足：8 轮 × 60s 的最坏情况
#: 约 480s，15 分钟留了一倍余量。env `DESKMATE_CONSULT_TIMEOUT` 可覆盖（调用时解析）。
DEFAULT_TIMEOUT = 900

#: 同一时刻最多几个咨询任务（每个任务内部再并行 1-3 位专家）。超过就拒绝，
#: 避免一次派活把机器和通道打满。
MAX_CONCURRENT_RUNS = 4

#: 读稿硬闸一：剩余预算低于这个比例就不再供料，把时间留给「写」。
#: 15 分钟预算里，读稿实测 72 秒就能覆盖 15 章，剩下的时间必须留给交付。
_READ_STOP_RATIO = 0.35

#: 读稿硬闸二：次数上限。15 章 / 13.7 万字的书实测 **4 次**即覆盖全书
#: （整本 80k = 首四章+尾六章，再一次「第五章到第九章」补中间）。所以默认就是 4：
#: 第 5 次起不再供料。`DESKMATE_CONSULT_READ_CAP` 可调大（21 卷那种长卷任务）。
_READ_CALLS_CAP_DEFAULT = 4

#: 专家引擎的工具白名单（2026-11-17 web_search 事故后立）。事故形态：专家 900s 里
#: **一次书都没读**（read_book 零调用，progress 无记录），全烧在联网查证的
#: 「失败→换词重试」循环——web_search 走免密钥 ddgs 车道，单次实测 23~35s、
#: 约一半失败。事故后只保留四类工具：读稿 / 本地史料（含私人资料）/ 联网兜底 /
#: 自己的笔记本。a2a、todo_write、propose_plan、load_skill、runtime_context 一律不给。
#: 🔴 web_search **保留**（萧哥 2026-11-17 拍板）：本地史料库没有的（如古代朝鲜史实）
#: 必须能联网查——纪律是「本地优先、联网兜底」，写进 _prompt，超时由 _instrument_tool 兜底。
_CONSULT_ALLOWED_TOOLS = frozenset({
    "read_book",
    "list_books",
    "search_history",
    "history_library_check",
    "search_private",
    "web_search",
    "web_fetch",
    "notebook_read",
    "notebook_remember",
    "notebook_rewrite",
    "notebook_feedback",
    # 引文自检（2026-11-17 编造台词事故后加）：交付前自己逐字核一遍，别等出门被标。
    "quote_check",
})

#: 单个工具调用的硬超时（秒）。ddgs 车道本身没传超时（Tavily/Brave 有 20s），
#: 一次挂死的联网调用足以吃掉整个 900s 预算；工具执行在 engine 里又没有超时
#: （asyncio.to_thread 裸跑）。这里给**所有**专家工具兜底：到点放一条错误给模型，
#: 让它换工具/换来源继续，而不是干等。env `DESKMATE_CONSULT_TOOL_TIMEOUT` 可调。
#: 60s 的算术：web_search 走主备两车道，最坏 = 豆包 20s（httpx timeout）+ ddgs
#: 实测 23~35s ≈ 55s，仍容得下一次完整降级；再小会砍掉备胎，再大只是让挂死的
#: 调用多吃预算。豆包常态 1.5s，绝大多数查询根本碰不到这个上限。
_TOOL_TIMEOUT_DEFAULT = 60.0

#: 专家引擎的迭代上限。build_engine 默认 150 是给编码 agent 的额度；文学专家
#: 「读稿 + 查证 + 交付」15~20 轮足够，卡住时也别让它在重试循环里转 150 轮。
_CONSULT_MAX_TURNS = 20

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
        "best": "发散方向、岔路、反转、钩子",
        "when": "卡在下一步、需要几条真正不同的方向；不是要方案",
    },
    {
        "id": "outline",
        "name": "大纲专家",
        "best": "卷/章形状、结构岔路；要章纲时再排",
        "when": "这卷/这几章怎么长；点名要章纲时再排",
    },
    {
        "id": "plot",
        "name": "情节架构专家",
        "best": "代价、岔路、主线升级、结构诊断",
        "when": "情节往哪拧、人物会被逼成什么样；要诊断时再盘",
    },
    {
        "id": "character",
        "name": "人物专家",
        "best": "动机、关系、弧光可能、口吻",
        "when": "这个人还能被逼成什么样；要一致性诊断时再盘",
    },
    {
        "id": "adaptation",
        "name": "影视改编",
        "best": "影视化、分集、改编可行性",
        "when": "能不能改、怎么改、IP/AI剧/版权方向",
    },
)
EXPERT_IDS = frozenset(item["id"] for item in EXPERTS)
# 创作型专家走「搭档」壳：默认给可能性/岔路，不交专家报告。
CREATIVE_EXPERTS = frozenset({"brainstormer", "plot", "outline", "character"})
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

## 什么时候必须叫专家（别自己硬答）

萧盛跟你聊创作时，结构 / 情节 / 人物的专业判断不是你的本事——是专家的本事。
这些话题一出现，第一反应是派专家，不是自己先编一套看法：

- 聊情节结构 / 主线支线 / 节奏钩子 / 分集架构，或对当前情节走向拿不准、搭不搭得住有疑问 → 情节架构专家（plot）
- 要把概念 / 剧情点子落成大纲，卷纲章纲怎么排 → 大纲专家（outline）
- 人物行为立不立得住、关系可信度、口吻 OOC → 人物专家（character）
- 判断这段写得好不好、结构语言有没有问题 → 文学编辑（lit-editor）
- 能不能影视化、怎么改编、分集可行性 → 影视改编（adaptation）
- 卡在下一步往哪走、要几条真正不同的方向 → 脑洞专家（brainstormer）

口径：**先派再聊**。你对情节可以有聊天的看法，但「立不立得住、怎么改」这类专业结论
要来自专家；你可以先说一句自己的直觉，同时把问题派出去，别等聊完才想起叫人。
要火花/岔路/可能时也派；但转述时别把他们的可能性改成「专家认为……」的报告体，
把岔路原样递到萧盛手上。
简单顺手的问题（查个事实、捋个名单）你自己干，不必事事都派。

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


def _read_calls_cap() -> int:
    """读稿次数上限（`DESKMATE_CONSULT_READ_CAP` 可调，长卷任务才需要调大）。"""
    raw = os.environ.get("DESKMATE_CONSULT_READ_CAP", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _READ_CALLS_CAP_DEFAULT
    return value if value > 0 else _READ_CALLS_CAP_DEFAULT


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


# ---------------------------------------------------------------------------
# 进度（2026-11-17）：让「正在读《夜不收》」这句话真的会动
# ---------------------------------------------------------------------------
#
# 之前的版本只有一个写死的标签 + 秒表：专家读了 3 章还是第 0 章，界面上一模一样。
# 现在把**真实发生的事**记下来——哪个工具在跑、补读的是哪一章、读了几次、累计多少字。
#
# 🔴 红线：只报事实，不做假进度条。没有「已完成 37%」这种数字，因为我们算不出来；
# 百分比一旦是猜的，就成了最坏的那种谎（用户会据此判断「快了」）。

#: 会被记账的读稿工具（只有这两个，见 bots/reading.py:make_reading_tools）。
_PROGRESS_TOOLS = frozenset({"read_book", "list_books"})


def _merge_progress(run_id: str, expert_id: str, **fields: Any) -> None:
    """把一位专家的进度并进状态文件；无变化就不广播。

    `fields` 里 `reads_inc` / `chars_inc` 是累加量，`tool_stat` 是工具账目
    （(工具名, 毫秒, 是否出错)，按工具累计 calls/errors/last_ms），其余是覆盖值
    （None = 不改）。
    """
    tool_stat = fields.pop("tool_stat", None)

    def _do(state: dict[str, Any]) -> Optional[dict[str, Any]]:
        run = _find(state, run_id)
        if run is None or run.get("status") != "running":
            return None
        bucket = run.setdefault("progress", {})
        cur = dict(bucket.get(expert_id) or {})
        reads_inc = int(fields.pop("reads_inc", 0) or 0)
        chars_inc = int(fields.pop("chars_inc", 0) or 0)
        changed = bool(reads_inc or chars_inc)
        if reads_inc:
            cur["reads"] = int(cur.get("reads") or 0) + reads_inc
        if chars_inc:
            cur["chars"] = int(cur.get("chars") or 0) + chars_inc
        if tool_stat:
            tname, tms, terror = tool_stat
            stats = dict(cur.get("tools") or {})
            row = dict(stats.get(tname) or {})
            row["calls"] = int(row.get("calls") or 0) + 1
            if terror:
                row["errors"] = int(row.get("errors") or 0) + 1
            row["last_ms"] = int(tms)
            stats[tname] = row
            cur["tools"] = stats
            changed = True
        for key, value in fields.items():
            if value is not None and cur.get(key) != value:
                cur[key] = value
                changed = True
        if not changed:
            return None
        cur["updated_at"] = time.time()
        bucket[expert_id] = cur
        return dict(run)

    snapshot = _mutate(_do)
    if snapshot:
        _notify("progress", snapshot)


def _run_snapshot(run_id: str, expert_id: str) -> dict[str, Any]:
    """读一次运行时快照：这个任务已经跑了多久、读了几次、累计多少字。"""
    with _LOCK:
        state = _read_state()
    run = _find(state, run_id) or {}
    prog = (run.get("progress") or {}).get(expert_id) or {}
    started = float(run.get("started_at") or time.time())
    budget = float(run.get("timeout") or _timeout())
    return {
        "elapsed": max(0.0, time.time() - started),
        "budget": max(1.0, budget),
        "reads": int(prog.get("reads") or 0),
        "chars": int(prog.get("chars") or 0),
    }


def _budget_head(snap: dict[str, Any]) -> str:
    left = max(0.0, snap["budget"] - snap["elapsed"])
    return (
        f"【读稿预算】已用 {snap['elapsed']:.0f}s / {snap['budget']:.0f}s（剩 {left:.0f}s）"
        f"，这是第 {snap['reads'] + 1} 次读稿，累计 {snap['chars']:,} 字。"
    )


def _budget_stop(reason: str, snap: dict[str, Any]) -> str:
    """硬闸返回：不给正文，只给一句「必须开始交付」。

    2026-11-17 两次实盘（生产 + 复现）都栽在同一个行为上：书 72 秒就读完了
    （4 次、14.9 万字），剩下 828 秒全在重复补读，最后交出的是一句「我要再补读
    第十二到十四章」——一个字正文都没有。模型没有时间感，工具又只喊「还缺几章」，
    于是它永远在准备。这条提示把「再读下去的真实后果」直接摆在它面前。
    """
    return (
        f"（{reason}：本任务已进行 {snap['elapsed']:.0f}s / {snap['budget']:.0f}s，"
        f"已读 {snap['reads']} 次共 {snap['chars']:,} 字。🔴 **现在必须开始交付**——"
        "继续补读会让整个任务超时，最终一个字都交不出来（这不是警告，是已经发生过两次的事实）。"
        "基于已读内容作答；确实没读到的章节，在「存疑处」里写明章节名，不要再用读稿工具。）"
    )


def _instrument_reading(fn: Any, *, run_id: str, expert_id: str) -> Any:
    """给读稿工具套一层记账：进入时记「在跑什么」，结束时记「读了几次/多少字」。

    用闭包把 run_id / expert_id 钉住，不用 thread-local：工具是在 `asyncio.to_thread`
    的执行器线程里跑的，跟 `_run_one` 不在一个线程上，thread-local 传不过去。
    `functools.wraps` 保留签名与文档串，注册表据此生成 schema（有测试钉这一条）。

    2026-11-17 起它还负责**预算**：每次读稿的返回开头带「已用 Xs / 共 Ys」，
    预算见底或次数到顶就直接不给正文——只给一句「开始交付」。理由见 `_budget_stop`。
    """
    name = getattr(fn, "__name__", "")
    if name not in _PROGRESS_TOOLS:
        return fn

    @functools.wraps(fn)
    def inner(*args: Any, **kwargs: Any) -> Any:
        chapter = ""
        if name == "read_book":
            raw = kwargs.get("chapter")
            if raw is None and len(args) > 1:
                raw = args[1]
            chapter = str(raw or "").strip()
        snap = _run_snapshot(run_id, expert_id)
        if name == "read_book":
            left = snap["budget"] - snap["elapsed"]
            cap = _read_calls_cap()
            if left <= snap["budget"] * _READ_STOP_RATIO:
                return _budget_stop("读稿时间段已用完", snap)
            if snap["reads"] >= cap:
                return _budget_stop(f"读稿次数已达上限（{cap} 次）", snap)
        _merge_progress(run_id, expert_id, busy=name, chapter=chapter)
        text = ""
        try:
            out = fn(*args, **kwargs)
            text = out if isinstance(out, str) else ""
            if name == "read_book" and text:
                # 预算行放最前面：模型对工具结果的开头最敏感，塞在 8 万字末尾会被淹掉。
                return f"{_budget_head(snap)}\n{text}"
            return out
        finally:
            _merge_progress(
                run_id,
                expert_id,
                busy="",
                reads_inc=1 if name == "read_book" else 0,
                chars_inc=len(text),
            )

    return inner


def _tool_timeout() -> float:
    """单工具硬超时（秒）。`DESKMATE_CONSULT_TOOL_TIMEOUT` 可调，非法值回落默认。"""
    raw = os.environ.get("DESKMATE_CONSULT_TOOL_TIMEOUT", "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _TOOL_TIMEOUT_DEFAULT
    return value if value > 0 else _TOOL_TIMEOUT_DEFAULT


#: 工具超时用的隔离线程池：到点后底层调用可能还挂在 socket 上（线程杀不掉），
#: 只能弃单返回错误——挂死的调用在池里自生自灭，不占 engine 的执行器。
_TOOL_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="consult-tool")


def _instrument_tool(fn: Any, *, run_id: str, expert_id: str) -> Any:
    """给**非读稿**工具套的三件套：飞行记录 + 硬超时 + 失败计数。

    为什么读稿工具不在这里包（它走 `_instrument_reading`）：读稿有自己的预算闸
    （次数/时间段供料），两个包装叠一起会把预算逻辑搅浑。

    - 飞行记录：进入时记 `busy=工具名`，结束记按工具累计的 calls/errors/last_ms
      ——2026-11-17 事故里 web_search 失败了十几轮，界面上却什么都看不见，
      因为进度只仪表化了读稿工具。这条记录同时喂状态条和审计，事后可复盘。
    - 硬超时：`fn` 丢进 `_TOOL_POOL` 等 `_tool_timeout()` 秒，到点返回一条错误
      （换工具/换来源继续），异常照旧上抛交给 engine 处理。
    `functools.wraps` 保留签名/文档/schema——注册表靠它生成工具描述（有测试钉）。
    """
    name = str(getattr(fn, "__name__", "") or "")

    @functools.wraps(fn)
    def inner(*args: Any, **kwargs: Any) -> Any:
        _merge_progress(run_id, expert_id, busy=name)
        started = time.time()
        try:
            future = _TOOL_POOL.submit(fn, *args, **kwargs)
            try:
                out = future.result(timeout=_tool_timeout())
            except FuturesTimeoutError:
                _merge_progress(
                    run_id,
                    expert_id,
                    tool_stat=(name, (time.time() - started) * 1000, True),
                )
                return {
                    "error": (
                        f"{name} 超过 {_tool_timeout():.0f}s 没有返回，已放弃本次调用。"
                        "别原样重试——换工具或换来源继续（查史实先用 search_history 查"
                        "本地史料库，秒回；实在查不到就标注「未核实」继续交付）。"
                    )
                }
            _merge_progress(
                run_id,
                expert_id,
                tool_stat=(name, (time.time() - started) * 1000, False),
            )
            return out
        except BaseException:
            # 工具自身抛错：照旧上抛（engine 会把错误回给模型），但失败也要记账。
            _merge_progress(
                run_id,
                expert_id,
                tool_stat=(name, (time.time() - started) * 1000, True),
            )
            raise
        finally:
            _merge_progress(run_id, expert_id, busy="")

    return inner


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
        # 进度只带计数与当前动作，不带任何正文（前端要显示"在读第几章"，不要稿子）。
        "progress": run.get("progress") or {},
        "results": [
            {
                "expert": r.get("expert"),
                "name": r.get("name"),
                "status": r.get("status"),
                "chars": len(str(r.get("text") or "")),
                "elapsed": r.get("elapsed"),
                # 引文核验计数（2026-11-17）：前端据此说「3 处引文正文未见」，
                # 不让「专家交付了」这句话盖住「其中几句是编的」。
                "citations": r.get("citations") or {},
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


def _build_expert_engine(
    expert_id: str,
    provider: Any,
    model: str,
    shelf: Any,
    run_id: str = "",
    book: str = "",
) -> Any:
    from coworker.agent import build_engine
    from coworker.permissions import Mode
    from coworker.personas.registry import get_registry
    from deskmate.bots.citations import make_quote_check_tool
    from deskmate.bots.reading import make_reading_tools

    agent = get_registry().get(expert_id).agent()
    reading = list(make_reading_tools(shelf))
    # 引文自检工具（2026-11-17 编造台词事故后加）：专家交付前自己核一遍要引的话，
    # 核不到就改成转述。它读的是同一本正文，逐字比对，不给模型留「像不像」的余地。
    reading.append(make_quote_check_tool(shelf, book))
    if run_id:
        reading = [
            _instrument_reading(fn, run_id=run_id, expert_id=expert_id) for fn in reading
        ]
    engine = build_engine(
        agent=agent,
        provider=provider,
        model=model,
        workspace=None,
        mode=Mode.BYPASS_APPROVALS,
        extra_tools=reading,
        # 150 是编码 agent 的额度；专家「读稿+查证+交付」用不了那么多轮，
        # 卡在重试循环里时也不该让它转满 150 轮（2026-11-17 事故形态之一）。
        max_iterations=_CONSULT_MAX_TURNS,
    )
    # 咨询里不允许弹问询卡片，也不允许沉淀 skill：专家只交意见，问题由小桔对萧盛说。
    engine.registry._tools.pop("ask_user", None)
    engine.registry._tools.pop("save_skill", None)
    # 工具白名单（2026-11-17 事故后立）：build_engine 给每个 agent 都挂全套默认工具
    # （web/a2a/todo/plan/skill/runtime_context 共 17 个），群聊的 BotRuntime 对同样的
    # 专家是显式白名单的，这条咨询路径当时漏抄了。现在对齐：只留白名单内的。
    for tool_name in list(engine.registry.names()):
        if tool_name not in _CONSULT_ALLOWED_TOOLS:
            engine.registry._tools.pop(tool_name, None)
    # 飞行记录 + 硬超时：非读稿工具逐个包一层（读稿工具已在上面带预算地包好）。
    # register 按名覆盖注册，functools.wraps 保住 schema（有测试钉这一条）。
    if run_id:
        for tool_name in list(engine.registry.names()):
            if tool_name in _PROGRESS_TOOLS:
                continue
            spec = engine.registry._tools.get(tool_name)
            if spec is not None:
                engine.registry.register(
                    _instrument_tool(spec.func, run_id=run_id, expert_id=expert_id)
                )
    return engine


def _prompt(expert_id: str, brief: str, book: str, timeout: int = 0) -> str:
    name = _NAME.get(expert_id, expert_id)
    book_line = f"《{book}》" if book else "未指定（可以先 list_books 查书架）"
    mins = f"{timeout / 60:.0f} 分钟" if timeout else "有限时间"
    creative = expert_id in CREATIVE_EXPERTS
    stance = (
        "你以创作搭档的身份看这个问题：不是审稿报告，也不是替他拿主意；"
        "不直接对话、不反问、不写正文。"
        if creative
        else "你只给专家意见，不直接对话、不反问、不写正文。"
    )
    output_rule = (
        "输出要求：中文；先接住他卡住的那股劲，给几条真正不同的可能/岔路；"
        "作者点名要方案/诊断时，再给判断和改法；依据能落章就落章，不确定的标出来；"
        "不要写成品正文。"
        if creative
        else "输出要求：中文；结论先行；给依据（章节/人物/设定）；不确定的标出来；不要写成品正文。"
    )
    return (
        f"你是「{name}」。萧盛让小桔（他的主助手）请你独立看一个问题。{stance}\n"
        f"书：{book_line}\n"
        "任务简报：\n"
        f"{brief}\n\n"
        f"【时间预算】总共 {mins}。读稿很慢——每读一次都要重新过一遍上下文。"
        "一本 15 章、13 万字左右的书，读 2 到 4 次（先整本看首尾、再点名补中间缺的章）"
        "就能覆盖全书，**读够了就必须动笔**。反复补读会让整个任务超时，最后一个字都交不出来"
        "（这已经实际发生过两次）。读稿工具自带预算：次数或时间用尽后它会直接拒绝供料，"
        "只回你一句「现在必须开始交付」。\n\n"
        "【查证纪律：本地优先，联网兜底】查证史实**先走本地**——search_history 查本地"
        "史料库（秒回、权威），history_library_check 核对收录。本地史料库**没有**的"
        "（比如古代朝鲜、域外史实）才用 web_search 联网查——**联网现在是秒回**的"
        "（豆包搜索，带信源权威等级），查得到就直接引用、不必犹豫；查两三个词确实没有，"
        "才把该处标「未核实」继续交付。本地查得到的绝不联网。\n\n"
        "【引文纪律：引号里只放原话】正文是唯一事实源。要引用原话，先用 quote_check "
        "逐字核一遍；**核不到就不要加引号**——改成转述，或直接标「正文未见」。"
        "转述、概括、拼接都不许塞进引号里：上一版专家小传就是这么出的错，"
        "几句正文根本没有的台词被加了引号、标上章号，读起来像原文，等于凭空给作者"
        "造了已写事实。章号同理，只写你真读到的章；没读到就写「正文未见」，不要凭印象补。\n\n"
        f"{output_rule}"
    )


async def _collect(engine: Any, prompt: str, timeout: int) -> tuple[str, str]:
    """跑一轮专家咨询，返回 (文本, 状态)。

    **超时不再清空**：已经流出来的分析原样返回并标 `timeout`——七轮白烧是最贵的浪费，
    而这七轮本身往往已经够小桔用了（2026-11-17 实盘：8 轮跑到第 8 轮才被掐）。
    """
    from coworker.events import EventType
    from coworker.llm_usage import scene_scope

    chunks: list[str] = []

    async def consume() -> None:
        # PR3：给本轮所有 provider 出口打 expert_consult 场景标签；
        # engine 已把 contextvars 复制进 produce 线程，标签不会在线程池边界丢失。
        with scene_scope("expert_consult"):
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


def _verify_citations(text: str, shelf: Any, book: str) -> tuple[str, dict[str, Any]]:
    """机械核对专家引文，返回 (加注后的文本, 统计)。

    红线：**正文未见就是未见**。这一步不做语义判断、不调模型——只问一句
    「这句原话在正文里逐字存在吗」。找不到就在引号后就地标「（正文未见原句）」，
    并在文末附清单；找到的顺带核对章号。

    为什么不交给模型自检了事：模型会在下一轮忘了上一轮编过什么。出门前这一道
    机械闸是唯一不依赖模型自觉的保证（2026-11-17 编造台词事故）。
    """
    stats: dict[str, Any] = {}
    if not (text and book and shelf is not None):
        return text, stats
    try:
        manuscript = shelf.manuscript(book)
        if manuscript is None:
            return text, stats
        from deskmate.bots.citations import check, summary

        report = check(text, manuscript)
    except Exception:  # noqa: BLE001 —— 核验失败不许拖垮咨询投递
        return text, stats
    stats = {
        "total": report.total,
        "found": report.found,
        "unverified": len(report.unverified),
        "mismatched": len(report.mismatched),
    }
    if report.flagged:
        text = f"{report.annotated}\n\n{summary(report)}"
    return text, stats


def _run_one(
    expert_id: str,
    brief: str,
    book: str,
    provider: Any,
    model: str,
    shelf: Any,
    timeout: int,
    run_id: str = "",
) -> tuple[str, str, float, dict[str, Any]]:
    """返回 (状态, 文本, 耗时, 引文核验统计)。状态 ∈ {ok, timeout, error}。"""
    started = time.time()
    try:
        engine = _build_expert_engine(expert_id, provider, model, shelf, run_id, book)
        text, status = asyncio.run(
            _collect(engine, _prompt(expert_id, brief, book, timeout), timeout)
        )
        text = text.strip()
        if status == "timeout":
            if text:
                text = f"{text}\n\n（注：专家超过 {timeout}s 预算被截断，以上是已产出的部分。）"
            else:
                text = f"【{_NAME.get(expert_id, expert_id)}】超过 {timeout}s 仍未产出任何内容。"
        text, cites = _verify_citations(text, shelf, book)
        return status, (text or "（专家没有产出文字）"), time.time() - started, cites
    except Exception as exc:  # noqa: BLE001 —— 一位专家失败不拖垮整次咨询
        return (
            "error",
            f"【{_NAME.get(expert_id, expert_id)}】调用失败：{type(exc).__name__}: {exc}",
            time.time() - started,
            {},
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
            lines += [
                f"## {r.get('name')}（{r.get('status')}{flag}）",
                "",
                str(r.get("text") or ""),
                "",
            ]
            cites = r.get("citations") or {}
            if cites:
                lines += [
                    f"> 引文核验：{cites.get('total', 0)} 处引号，"
                    f"{cites.get('found', 0)} 处逐字命中，"
                    f"{cites.get('unverified', 0)} 处正文未见，"
                    f"{cites.get('mismatched', 0)} 处章号不符。",
                    "",
                ]
        # 工具账目（飞行记录）：哪个工具被调了几次、失败几次、最后耗时。出事时
        # 这几行就是「时间花哪了」的直接答案——2026-11-17 事故复盘全靠事后补测，
        # 有这段就不用了。
        progress = run.get("progress") or {}
        tool_rows = []
        for expert_id, prog in progress.items():
            tools = (prog or {}).get("tools") or {}
            for tname, stat in tools.items():
                if not isinstance(stat, dict):
                    continue
                tool_rows.append(
                    f"- {expert_id} · {tname}：{stat.get('calls', 0)} 次"
                    f"（失败 {stat.get('errors', 0)}，最后 {stat.get('last_ms', 0)}ms）"
                )
        if tool_rows:
            lines += ["## 工具调用", ""] + tool_rows + [""]
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
    orchestration_store: Any = None,
) -> None:
    started = time.time()
    results: list[dict[str, Any]] = []
    try:
        if orchestration_store is not None:
            try:
                from coworker.orchestration import (
                    ConcurrencyLeases,
                    Job,
                    LeaseConfig,
                    OrchestrationRunner,
                    StepKind,
                    StepSpec,
                    StepStatus,
                )

                async def _run_via_orch() -> list[dict[str, Any]]:
                    job = Job(
                        id=f"consult-{run_id}",
                        title=f"consult {'/'.join(chosen)}",
                        source="consult",
                        steps=[
                            StepSpec(
                                id=f"expert:{eid}",
                                title=_NAME.get(eid, eid),
                                kind=StepKind.READ,
                                # 预算由 _run_one 自己管（_collect 超时后**保留已产出
                                # 的部分**并标 timeout）。这里再设同额 timeout_s 会先
                                # 一步抛 TimeoutError，把部分产出换成「调用失败」，而且
                                # asyncio.run 退出时还要等 to_thread 线程跑完——时间
                                # 一分不省、内容全丢。
                                timeout_s=0.0,
                                meta={"expert": eid, "book": book, "uses_llm": True},
                            )
                            for eid in chosen
                        ],
                        meta={"run_id": run_id, "book": book},
                    )

                    async def handler(spec, _job, _ctx):
                        eid = spec.meta["expert"]
                        status, text, elapsed, cites = await asyncio.to_thread(
                            _run_one, eid, brief, book, provider, model, shelf, timeout, run_id
                        )
                        return {
                            "expert": eid,
                            "name": _NAME.get(eid, eid),
                            "status": status,
                            "text": text,
                            "elapsed": round(elapsed, 1),
                            "citations": cites or {},
                        }

                    runner = OrchestrationRunner(
                        orchestration_store,
                        ConcurrencyLeases(LeaseConfig(max_parallel=max(1, len(chosen)), max_read=max(1, len(chosen)))),
                    )
                    try:
                        await runner.run(job, handler)
                    except Exception:
                        # 检查点库出错时保留已完成的那几位，绝不为了重试把整轮专家
                        # 再跑一遍（每位专家都是一次完整 LLM 调用）。
                        logger.exception("consult orchestration run failed: %s", run_id)
                    if not any(
                        r.status in (StepStatus.OK, StepStatus.FAILED, StepStatus.SKIPPED)
                        for r in job.runs.values()
                    ):
                        # 一步都没跑成 → 交给下面的老路，不算重复花钱。
                        return []
                    rows: list[dict[str, Any]] = []
                    for eid in chosen:
                        run_rec = job.runs.get(f"expert:{eid}")
                        if run_rec and run_rec.status is StepStatus.OK and isinstance(run_rec.result, dict):
                            rows.append(run_rec.result)
                        else:
                            err = (run_rec.error if run_rec else None) or "interrupted"
                            rows.append(
                                {
                                    "expert": eid,
                                    "name": _NAME.get(eid, eid),
                                    "status": "error",
                                    "text": f"调用失败：{err}",
                                    "elapsed": 0.0,
                                    "citations": {},
                                }
                            )
                    return rows

                results = asyncio.run(_run_via_orch())
            except Exception:
                # Orchestration is an enhancement; fall back to legacy parallel path.
                logger.exception("consult orchestration unavailable, using legacy path")
                results = []
        if not results:
            with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
                futures = {
                    pool.submit(
                        _run_one, eid, brief, book, provider, model, shelf, timeout, run_id
                    ): eid
                    for eid in chosen
                }
                for fut in as_completed(futures):
                    eid = futures[fut]
                    try:
                        status, text, elapsed, cites = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        status, text, elapsed, cites = (
                            "error", f"调用失败：{exc}", time.time() - started, {}
                        )
                    results.append(
                        {"expert": eid, "name": _NAME.get(eid, eid), "status": status,
                         "text": text, "elapsed": round(elapsed, 1),
                         "citations": cites or {}}
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

        # 先取运行期攒下的进度（工具账目）再落审计：审计与最终记录都要带上它，
        # 出事时能直接看到「web_search 失败 12 次」这类事实（2026-11-17 教训）。
        with _LOCK:
            prior = _find(_read_state(), run_id) or {}
        run["progress"] = dict(prior.get("progress") or {})
        # model 只在派发时落过 state，收尾合成的 run 里没有——不补的话审计
        # 永远写「模型 ?」（19:10 那份实测漏的就是它）。
        for field in ("model", "session_id", "started_at", "deadline_at"):
            if not run.get(field) and prior.get(field):
                run[field] = prior[field]
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
    orchestration_store: Any = None,
) -> dict[str, Any]:
    """起一个后台咨询任务，立刻返回任务记录。

    并发上限在这里、同一把锁里做“检查 + 占位”：调用方的前置检查挡不住
    两个会话同时派发（都通过检查、都派发，跑到 5 个），2026-11-22 修。
    满员时抛 RuntimeError，调用方把它翻回给用户的提示。
    """
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
        live = sum(1 for t in _ACTIVE.values() if t.is_alive())
        if live >= MAX_CONCURRENT_RUNS:
            raise RuntimeError(
                f"现在已经有 {MAX_CONCURRENT_RUNS} 个专家任务在跑（后台），先别再派："
                "等结果回来、或等其中一位结束再派。别把同一件事重派一遍。"
            )
        _mutate(_add)
        thread = threading.Thread(
            target=_job_thread,
            args=(run_id, list(chosen), brief, book, provider, model, shelf, budget, audit_dir, orchestration_store),
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
    # 引文核验结果放最前面：小桔读这段是要转述给萧盛的，编造的台词如果在
    # 4000 字里被转述出去，事后标注再多也白搭（2026-11-17 编造台词事故）。
    missing = sum(
        int((r.get("citations") or {}).get("unverified") or 0) for r in results
    )
    wrong_ch = sum(
        int((r.get("citations") or {}).get("mismatched") or 0) for r in results
    )
    warning = ""
    if missing or wrong_ch:
        bits = []
        if missing:
            bits.append(f"{missing} 处引文正文未见原句")
        if wrong_ch:
            bits.append(f"{wrong_ch} 处章号与正文不符")
        warning = (
            f"【⚠ 引文核验】{'、'.join(bits)}——原文里已就地标注并附清单。"
            "这些句子**不是**已写事实，汇报时不要当原话引用，也不要替它圆场；"
            "要点可以保留，但必须说成「专家的判断/推演」。\n\n"
        )
    return (
        f"【专家任务结束】{names or '专家'}·《{run.get('book') or '未指定书'}》"
        f"，用时 {float(run.get('elapsed') or 0):.0f}s。{tail}\n\n{warning}{body}\n\n"
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
    orchestration_store: Any = None,
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

        # Fast path only: the authoritative check lives inside dispatch(),
        # under the same lock that registers the new thread.
        if _active_count() >= MAX_CONCURRENT_RUNS:
            return (
                f"现在已经有 {MAX_CONCURRENT_RUNS} 个专家任务在跑（后台），先别再派："
                "等结果回来、或等其中一位结束再派。别把同一件事重派一遍。"
            )

        try:
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
                orchestration_store=orchestration_store,
            )
        except RuntimeError:
            return (
                f"现在已经有 {MAX_CONCURRENT_RUNS} 个专家任务在跑（后台），先别再派："
                "等结果回来、或等其中一位结束再派。别把同一件事重派一遍。"
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
