"""小桔调用专家（2026-11-09）。

设计边界（萧哥拍板）：
- 小桔是主体，知道专家存在、可以派活；专家**看不到**小桔的记忆，小桔也**不读**专家笔记本；
- 同步执行、并行跑；简单问题 1 位，复杂/多问题 2 位，最多 3 位；
- 专家可读全书（只读 read_book），可写自己的笔记本；不写正文、不碰小桔记忆；
- 咨询记录落 ~/.deskmate/consultations/ 审计，不进小桔长期记忆；咨询轮不自动抽记忆。
"""
from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

MAX_EXPERTS = 3
DEFAULT_TIMEOUT = 240

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
- 调用前先对萧盛说一句「我请 X 看下，稍等」（让他知道正在调用专家）；结果回来再汇报。
- 专家只给参考。你综合后向萧盛汇报：标明是谁的判断，不要照搬，不要把专家原话当事实。
- 咨询结果不会自动进你的长期记忆；汇报后你可以把「咨询了谁 + 结论 + 萧盛采纳/驳回」记一条。
"""


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


async def _collect(engine: Any, prompt: str, timeout: int) -> str:
    from coworker.events import EventType

    text = ""

    async def consume() -> None:
        nonlocal text
        async for event in engine.run(prompt):
            if event.type == EventType.ASSISTANT_MESSAGE:
                chunk = (event.data or {}).get("text") or ""
                if chunk:
                    text = chunk

    try:
        await asyncio.wait_for(consume(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            engine.request_interrupt()
        except Exception:  # noqa: BLE001
            pass
        raise
    return text


def _run_one(
    expert_id: str,
    brief: str,
    book: str,
    provider: Any,
    model: str,
    shelf: Any,
    timeout: int,
) -> str:
    try:
        engine = _build_expert_engine(expert_id, provider, model, shelf)
        text = asyncio.run(_collect(engine, _prompt(expert_id, brief, book), timeout))
        return text.strip() or "（专家没有产出文字）"
    except asyncio.TimeoutError:
        return f"【{_NAME.get(expert_id, expert_id)}】调用超时（{timeout}s），本轮没有结果。"
    except Exception as exc:  # noqa: BLE001 —— 一位专家失败不拖垮整次咨询
        return f"【{_NAME.get(expert_id, expert_id)}】调用失败：{type(exc).__name__}: {exc}"


def _write_audit(
    results: list[tuple[str, str]],
    brief: str,
    book: str,
    base_dir: Optional[Path] = None,
) -> None:
    try:
        root = Path(
            base_dir
            or os.environ.get("DESKMATE_CONSULT_DIR")
            or (Path.home() / ".deskmate" / "consultations")
        )
        now = datetime.now()
        folder = root / now.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (now.strftime("%H%M%S") + "-" + "-".join(e for e, _ in results) + ".md")
        lines = [
            f"# 专家咨询 · {now.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"- 书：{book or '未指定'}",
            f"- 专家：{'、'.join(_NAME.get(e, e) for e, _ in results)}",
            "",
            "## 简报",
            "",
            brief,
            "",
        ]
        for expert_id, text in results:
            lines += [f"## {_NAME.get(expert_id, expert_id)}", "", text, ""]
        lines.append("> 专家意见仅供参考，不是小桔本人判断，也不自动进入小桔长期记忆。")
        path.write_text("\n".join(lines), encoding="utf-8")
    except Exception:  # noqa: BLE001 —— 审计落盘失败不能影响咨询
        pass


def make_consult_tool(
    *,
    provider: Any,
    model: str,
    timeout: int = DEFAULT_TIMEOUT,
    on_consulted: Optional[Callable[[], None]] = None,
    shelf: Any = None,
    audit_dir: Optional[Path] = None,
):
    """给小桔的 consult_experts 工具（同步、并行）。"""

    def consult_experts(experts: list, brief: str, book: str = "") -> str:
        """让专家独立看一个问题，返回带署名的专家意见。

        experts: 1-3 位专家 id 或名字，如 ["lit-editor", "brainstormer"]。
        brief: 自包含的任务简报（书/章节/卡点/要他们回答什么）。
        book: 书名（如 "夜不收"）；不填则专家自己查书架。
        专家看不到小桔的记忆；结果只作参考，由小桔综合后向萧盛汇报。
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
        if on_consulted is not None:
            try:
                on_consulted()
            except Exception:  # noqa: BLE001
                pass

        from deskmate.bots.reading import default_shelf

        sh = shelf or default_shelf()
        book_title = str(book or "").strip()
        if book_title:
            try:
                if sh.resolve(book_title) is None:
                    return f"找不到《{book_title}》这本书；先用 list_books 对一下书名。"
            except Exception:  # noqa: BLE001
                return f"找不到《{book_title}》这本书；先用 list_books 对一下书名。"

        started = time.time()
        results: list[tuple[str, str]] = []
        with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
            futures = {
                pool.submit(
                    _run_one, eid, clean, book_title, provider, model, sh, timeout
                ): eid
                for eid in chosen
            }
            for fut in as_completed(futures):
                eid = futures[fut]
                try:
                    results.append((eid, fut.result(timeout=timeout + 30)))
                except Exception as exc:  # noqa: BLE001
                    results.append((eid, f"【{_NAME.get(eid, eid)}】调用失败：{exc}"))
        # 按用户给的顺序排列，读起来稳定
        order = {eid: i for i, eid in enumerate(chosen)}
        results.sort(key=lambda pair: order.get(pair[0], 99))
        elapsed = time.time() - started
        _write_audit(results, clean, book_title, audit_dir)

        body = "\n\n".join(f"## {_NAME.get(eid, eid)}\n{text}" for eid, text in results)
        return (
            f"【专家咨询结果】已调用：{'、'.join(_NAME.get(e, e) for e, _ in results)}"
            f"（{elapsed:.0f}s）\n\n{body}\n\n"
            "（以上是专家意见，仅供参考。请综合后向萧盛汇报并标明是谁说的；"
            "不要照搬，不要把专家原话当事实。）"
        )

    return consult_experts
