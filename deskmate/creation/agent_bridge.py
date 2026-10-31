"""创作流水线 → 真 agent 的执行桥。

设计约束（与 ``deskmate/teams_client.py`` 一致）：deskmate 模块不持有
``coworker.server.manager`` 单例。主 app 挂载创作圆桌时把 manager 注册到这里
（``set_manager``）；圆桌独立运行时这里为 ``None``，调用方（AgentExecutor）据此
fail-open 回退到 LLMExecutor，sidecar 单独跑绝不崩。

三层能力：
1. ``reserve_worker_sessions``——建团前为流水线 worker 预建**真会话**（磁盘状态、
   零 token：第一次派活才烧模型），让 board 上的 actor 不再是占位 id。
2. ``dispatch_task``——把一段活派给真 worker：走 ``deliver_to_session`` 跑一轮真实
   回合（人格 prompt + 工具 + 技能），要求在会话 scratch 目录写出成稿文件，回来读它。
   超时/主 app 不可用/worker 缺位 → 返回 None，由调用方回退。
3. ``add_expert``——动态加人：给已建好的团队临时再招一位专家（C+）。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_manager = None


def set_manager(manager) -> None:
    """主 app 挂载圆桌时调用；传 None 表示脱离主 app（独立运行）。"""
    global _manager
    _manager = manager


def get_manager():
    return _manager


def lead_session_for(space: str) -> str:
    return f"{space}:lead"


def _client():
    """磁盘上的 teams.json 花名册（每次重新读，不用 manager 内存里的副本）。

    主 app 的 TeamRegistry 在启动时一次性载入，deskmate 侧新写的团队它看不见；
    反过来 manager.teams 里若有 pipeline 团队，team_tick 会把它们当普通团队唤醒
    （pipeline 的主编是合成 session，唤了会空转）。因此桥只读文件、只写文件，
    与 manager 的 roster 保持「同源不共享」。
    """
    manager = _manager
    base = getattr(getattr(manager, "session_store", None), "base", None) if manager else None
    if not base:
        return None
    from ..teams_client import TeamsClient

    return TeamsClient(base_dir=base, journal=False)


def _team_for_space(space: str):
    manager = _manager
    if manager is None:
        return None, None
    client = _client()
    if client is None:
        return manager, None
    return manager, client.teams.for_lead_session(lead_session_for(space))


def available_experts(space: str) -> list[dict]:
    """可临时加入本团队的 worker 专家（persona 注册表动态扫描，与主编侧
    ``team_options`` 同源：任何 ``team: worker`` 且已启用的人格都可选）。"""
    manager, team = _team_for_space(space)
    if manager is None:
        return []
    out = []
    for pid in manager.personas.ids():
        entry = manager.personas.get(pid)
        manifest = getattr(entry, "manifest", None)
        if manifest is None or manifest.team != "worker":
            continue
        if not manager.personas.is_enabled(pid):
            continue
        joined = bool(team is not None and any(w.persona == pid for w in team.workers))
        out.append(
            {
                "persona": pid,
                "name": manifest.name,
                "tagline": manifest.tagline,
                "joined": joined,
            }
        )
    return out


def reserve_worker_sessions(
    space: str, personas: list[str], *, model: Optional[str] = None
) -> dict[str, str]:
    """为即将建团的 worker 预建真会话，返回 ``{persona: session_id}``。

    与 ``manager.create_team`` 的预派生同义：只落会话记录（磁盘状态），不发第一轮，
    零 token。manager 不可用/无 workspace 时返回空 dict（调用方回退占位 session id）。
    """
    manager = _manager
    if manager is None:
        return {}
    workspace = getattr(manager, "default_workspace", None)
    if not workspace:
        return {}
    from coworker.sessions import SessionRecord
    import uuid

    out: dict[str, str] = {}
    for pid in personas:
        sid = uuid.uuid4().hex[:12]
        try:
            manager.session_store.save(
                SessionRecord(
                    session_id=sid,
                    workspace=workspace,
                    model=str(model or getattr(manager, "model", "")),
                    mode=getattr(manager, "mode", None) or "interactive",
                    messages=[],
                    agent=pid,
                )
            )
        except Exception as exc:  # noqa: BLE001 — 预建失败只是回退占位，不阻断建团
            logger.warning("预建 worker 会话失败(%s): %s", pid, exc)
            continue
        out[pid] = sid
    return out


def bind_team(space: str, team, workspace: Optional[str] = None) -> None:
    """建团后把 team_id / actor / space 回写进每个真 worker 会话。

    ``create_team`` 在团队存在前只能写空 team_id，这里补上；没有真会话（回退占位
    session id）时静默跳过——占位 id 在 session store 里不存在，``set_team`` 写入的
    是一条无主记录，反而更糟，所以先查存在再写。
    """
    manager = _manager
    if manager is None or team is None:
        return
    for worker in team.workers:
        try:
            record = manager.session_store.load(worker.session_id)
            if record is None:
                continue
            manager.session_store.set_team(
                worker.session_id,
                {
                    "team_id": team.team_id,
                    "role": "worker",
                    "actor": worker.actor,
                    "lead_session": lead_session_for(space),
                    "space": team.space,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("绑定 worker 会话失败(%s): %s", worker.actor, exc)


def add_expert(
    space: str,
    persona: str,
    *,
    name: str = "",
    reason: str = "",
    model: Optional[str] = None,
) -> dict:
    """动态加人（C+）：给已建团队的流水线临时再招一位专家。

    返回 ``manager.add_to_team`` 结果；主 app 不可用时抛 RuntimeError（由 service
    层转成前端可读错误，而不是静默失败）。
    """
    manager, team = _team_for_space(space)
    if manager is None:
        raise RuntimeError("动态加专家需要主 app 在运行（当前为圆桌独立模式）")
    if team is None:
        raise RuntimeError("该流水线还没有团队，请先创建流水线")
    client = _client()
    # 重活（校验人格 / 预建会话 / 写 team 元数据）交给 manager；花名册由本侧
    # 自己的 registry 写（pipeline 读的就是这一份，manager 的内存副本不参与）。
    res = manager.spawn_worker(
        team,
        {"persona": persona, "name": name, "reason": reason},
        workspace=getattr(manager, "default_workspace", None),
        model=model,
        lead_session=lead_session_for(space),
    )
    if not res.get("approved"):
        return res
    worker = res.pop("worker_obj")
    if client is None or client.teams.add_worker(team.team_id, worker) is None:
        return {"approved": False, "error": "花名册写入失败（团队可能已被删除）"}
    return res


_ACK_OPENERS = (
    "已写入", "已保存", "已生成", "已交付", "已上传", "写入完成", "保存完成",
    "ok", "done", "saved", "written",
)


def _is_acknowledgement(text: str) -> bool:
    """True when the reply only CONFIRMS a delivery instead of containing one.

    The dispatch prompt tells the worker to answer with a single line ("已写入")
    because the real payload is the file. So when the file is missing, the last
    assistant message is quite likely that confirmation — and returning it would
    silently store three characters as the outline/screenplay body. A worker that
    fails to write must fall back to the LLM, not appear to succeed.
    """
    head = (text or "").strip().lstrip("`").strip().lower()
    if not head or len(head) > 200:  # a real deliverable is longer than a note
        return False
    return any(head.startswith(opener) for opener in _ACK_OPENERS)


def _last_assistant_text(messages: list, start: int) -> str:
    for message in reversed(messages[start:]):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            text = "\n".join(p for p in parts if p).strip()
            if text:
                return text
    return ""


def dispatch_task(
    space: str,
    persona: str,
    instruction: str,
    *,
    timeout: float = 900.0,
    filename: str = "draft.md",
    on_event=None,
    abort=None,
) -> Optional[str]:
    """把一段活派给真 worker 并取回成稿文本；不可用/超时/空产出 → None。

    取稿优先级：worker 写出的成稿文件 → 该回合最后一条 assistant 消息。
    回退由调用方决定（AgentExecutor 回退 LLMExecutor），这里绝不 raise 到 pipeline。
    """
    manager, team = _team_for_space(space)
    if manager is None or getattr(manager, "_loop", None) is None:
        return None
    if team is None:
        return None
    worker = next((w for w in team.workers if w.persona == persona), None)
    if worker is None:
        return None
    record = manager.session_store.load(worker.session_id)
    if record is None:
        return None
    if abort is not None and abort.is_set():
        return None

    out_dir = Path(manager.scratch_base()) / worker.session_id / "creation"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / filename
        if out_path.exists():
            out_path.unlink()
    except OSError as exc:  # noqa: BLE001
        logger.warning("准备交付目录失败(%s): %s", worker.session_id, exc)
        return None

    if on_event:
        try:
            on_event(
                {
                    "type": "note",
                    "message": f"已把活派给真专家「{worker.actor}」，等它交付…",
                    "progress": 20,
                }
            )
        except Exception:  # noqa: BLE001
            pass

    prompt = (
        f"{instruction.strip()}\n\n"
        "【交付方式（必须遵守）】\n"
        f"把成稿正文完整写入文件：{out_path}\n"
        "用文件写入工具覆盖写这个文件（不要只把正文贴在回复里，回复只写一句「已写入」）。"
        "文件内容只放成稿正文本身，不要加解释。"
    )
    before = len(record.messages)
    deadline = time.monotonic() + max(1.0, timeout)
    future = asyncio.run_coroutine_threadsafe(
        manager.deliver_to_session(worker.session_id, prompt), manager._loop
    )
    # 轮询式等待：客户端断开（abort）或超时都能及时松手；底层回合会自行跑完并落盘，
    # 这里只决定「本次调用还等不等」，不硬杀回合。
    while True:
        if abort is not None and abort.is_set():
            logger.info("创作被中止：停止等待 worker %s 的交付", worker.actor)
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning("worker %s 交付超时（%.0fs），回退 LLM", worker.actor, timeout)
            return None
        try:
            future.result(timeout=min(5.0, remaining))
            break
        except concurrent.futures.TimeoutError:
            continue
        except Exception as exc:  # noqa: BLE001 — 回合本身失败：回退，不炸流水线
            logger.warning("worker %s 回合失败：%s", worker.actor, exc)
            return None

    text = ""
    try:
        if out_path.is_file():
            text = out_path.read_text(encoding="utf-8").strip()
    except OSError as exc:  # noqa: BLE001
        logger.warning("读取 worker 成稿失败(%s): %s", out_path, exc)
    if text:
        return text
    try:
        record = manager.session_store.load(worker.session_id)
        if record is not None:
            text = _last_assistant_text(record.messages, before)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 worker 消息失败(%s): %s", worker.session_id, exc)
    if text and _is_acknowledgement(text):
        # The file was never written and the reply is only a confirmation: treat
        # the delivery as failed so the caller falls back to the LLM, instead of
        # storing "已写入" as if it were the draft.
        logger.warning(
            "worker %s 未写出成稿文件，回复仅为确认语（%r）→ 视为交付失败，回退 LLM",
            worker.actor,
            text[:40],
        )
        return None
    return text or None
