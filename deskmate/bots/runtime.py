"""真 Agent 运行时 —— 7 位专家从「人设卡」升级为有 engine、有工具、自主决策的 bot。

萧哥 11-06 决策 2 的核实结论（代码实证，见 docs/bot-mode-redesign.md）：
7 位专家的 ``coworker/personas/builtin/*.md`` 本来就是**完整 persona**（有 tools、
default_permission_mode、mcp 声明），跟小桔 deskmate.md 同构。旧圆桌之所以"不是
真 Agent"，是 ``roundtable/coordinator.py:585`` 只把 ``system_prompt`` 文本塞进
``chat_stream()`` 纯 prompt 注入 —— 没起 engine、没有工具、不能自己决定读稿。
这里改走官方 ``PersonaRegistry.agent()`` + ``build_engine()``，于是同一批 persona
变成能自主调用 ``read_book`` 的真 Agent。

三条硬约束：
1. **provider 不跟随主对话模型**：默认指向付费通道组（ark），否则会重演昨天
   「opencode-go 400 MissingSessionID」把每批抽取打挂的事故。
2. **工具面只读**：``dataclasses.replace`` 换掉 tool_factory，再收紧 registry，
   只留 ``read_book`` / ``list_books``；write_file / apply_patch / run_shell /
   save_skill / a2a_call / web_fetch 全部摘掉 —— 书稿绝不进入可写工具的射程。
3. **有界**：max_iterations 封顶，每次群里一句话每个 bot 只跑一个 turn，
   群历史上限 20 条，读稿上限 12000 字。不 @ 不烧钱，@ 谁谁答。
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional, Sequence

from coworker.agent import build_engine
from coworker.personas.registry import get_registry

from .reading import BookShelf, default_shelf, make_reading_tools
from .roster import KNOWN_BOT_IDS

logger = logging.getLogger(__name__)

# 群聊层的行为约束：这是「在群里说人话」，不是派活 / 验收 / 写报告。
_GROUP_GUIDANCE = """

# 你现在在一个 bot 群里

- 你是群里的一员，用**自己的专长**说人话，一次发言简短（100~300 字），不要长篇报告。
- 需要书稿背景时**自己调用 read_book**，书名从大家的话里找；绝不问用户要路径、
  绝不要求填任何表格字段。读不到就直说读不到，不要编造情节或段号。
- 只读，不写文件、不改稿、不派活、不建工作项、不做审核验收、不打回重改。
- **不生成纪要 / 总结 / 主持人收尾**；不要替别人发言，也不要复述群历史。
- 群里其他人的发言已经在上下文里，接话而不是重新开始。
"""

_HISTORY_LIMIT = 20
_DEFAULT_MAX_ITERATIONS = 4


def paid_credentials() -> tuple[str, str, str]:
    """取付费通道组里第一条可用通道的 ``(api_key, base_url, model)``。

    与 ``deskmate.memory.llm.chat_memory`` 同源：走 ``llm_channels.route(GROUP_PAID)``
    （已滤熔断），不绑死某一个厂商；凭证统一由 SecretStore 经 ``_creds_for`` 取。
    """
    from deskmate.llm_channels import GROUP_PAID, route
    from deskmate.memory.llm import _creds_for

    channel, _skipped = route(GROUP_PAID)
    if channel is None:
        raise RuntimeError("付费通道组无可用通道（凭证缺失或全组熔断），bot 群无法发言")
    api_key, base_url, model = _creds_for(channel.provider, channel.model)
    if not (api_key and base_url):
        raise RuntimeError("付费通道凭证不完整，bot 群无法发言（fail-fast）")
    return str(api_key), str(base_url), str(model)


def default_provider() -> Any:
    """付费通道组的 OpenAI 兼容 provider。

    **绝不**用 ``_resolve_provider()`` 的 default_model：主模型 opencode-go 网关
    要求 ``x-opencode-session`` 会话头，后台裸调用必得 400 MissingSessionID
    （见 2026-11-06 圣经抽取事故）。
    """
    from coworker.providers.openai_provider import OpenAIProvider

    api_key, base_url, model = paid_credentials()
    return OpenAIProvider(api_key=api_key, base_url=base_url, default_model=model)


def default_model() -> str:
    return paid_credentials()[2]


def _run_sync(coro) -> Any:
    """跑一个协程；已在 event loop 里时挪到独立线程，避免 asyncio.run 冲突。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def format_history(history: Sequence[dict] | None) -> str:
    """群历史 → 一段纯文本上下文（有上限，防上下文膨胀烧钱）。"""
    rows = list(history or [])[-_HISTORY_LIMIT:]
    if not rows:
        return ""
    lines = []
    for m in rows:
        who = str(m.get("author") or "?")
        role = str(m.get("author_role") or "")
        tag = "（我）" if role == "user" else f"（{role}）"
        text = str(m.get("text") or "").strip()
        if text:
            lines.append(f"{who}{tag}: {text[:600]}")
    return "群内已有发言：\n" + "\n".join(lines) if lines else ""


class BotRuntime:
    """每个 bot 一个常驻 engine（跨发言记忆不归零），群只是临时协调面。

    ``provider`` 可注入，便于离线测试真 Agent 的自主工具决策；默认走付费通道。
    """

    def __init__(
        self,
        *,
        books_root: str | Path | None = None,
        workspace_root: str | Path | None = None,
        provider: Any = None,
        model: str | None = None,
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    ) -> None:
        self.shelf: BookShelf = (
            BookShelf(Path(books_root)) if books_root is not None else default_shelf()
        )
        self.workspace_root = Path(
            workspace_root
            or os.environ.get("DESKMATE_BOT_WORKSPACES")
            or Path("~/.config/coworker/bot_workspaces")
        ).expanduser()
        self.provider = provider
        self.model = model or ""
        self.max_iterations = max(2, int(max_iterations))
        self._engines: dict[str, Any] = {}
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- engine
    def engine(self, bot_id: str) -> Any:
        """构造（并缓存）一个 bot 的官方 TurnEngine。"""
        bot_id = str(bot_id).strip()
        if bot_id not in KNOWN_BOT_IDS:
            raise ValueError(f"未知 bot：{bot_id}（bot 必须来自常驻名册）")
        with self._lock:
            if bot_id in self._engines:
                return self._engines[bot_id]

            entry = get_registry().get(bot_id)
            if entry is None:
                raise ValueError(f"persona '{bot_id}' 未注册，无法起真 Agent")
            agent = entry.agent()
            tools = make_reading_tools(self.shelf)
            agent = dataclasses.replace(
                agent,
                tool_factory=lambda _ctx: tools,
                system_prompt=(agent.system_prompt or "") + _GROUP_GUIDANCE,
                # 群 bot 不需要文件夹门禁 / 定时唤醒 / 消息外发 / 连接器
                requires_folder=False,
                scheduling=False,
                messaging=False,
                connectors=False,
                team=None,
            )
            ws = self.workspace_root / bot_id
            ws.mkdir(parents=True, exist_ok=True)
            provider, model = self.provider, self.model
            if provider is None or not model:
                # 一次解析，避免 provider / model 各自路由到不同通道
                api_key, base_url, channel_model = paid_credentials()
                if provider is None:
                    from coworker.providers.openai_provider import OpenAIProvider

                    provider = OpenAIProvider(
                        api_key=api_key, base_url=base_url, default_model=channel_model
                    )
                if not model:
                    model = channel_model
            eng = build_engine(
                agent=agent,
                provider=provider,
                model=model,
                workspace=str(ws),
                max_iterations=self.max_iterations,
            )
            # 收紧工具面：build_engine 还会固定注册一堆 web/a2a/skill 工具，
            # 群 bot 只保留两个只读读稿工具（防出网、防写、防上下文爆炸）。
            allowed = {"read_book", "list_books"}
            eng.registry._tools = {
                name: spec
                for name, spec in eng.registry._tools.items()
                if name in allowed
            }
            self._engines[bot_id] = eng
            return eng

    # ---------------------------------------------------------------- speak
    def speak(
        self, bot_id: str, text: str, *, history: Sequence[dict] | None = None
    ) -> str:
        """说一句人话 → bot 自主决定要不要读稿 → 返回它这一轮的署名发言。"""
        eng = self.engine(bot_id)
        prior = format_history(history)
        prompt = (
            (prior + "\n\n" if prior else "")
            + f"群里刚有人说：{text}\n\n请直接以你自己的身份发言。"
        )
        before = len(eng.messages)
        _run_sync(self._drive(eng, prompt))
        replies = [
            str(m.get("content") or "")
            for m in eng.messages[before:]
            if m.get("role") == "assistant" and str(m.get("content") or "").strip()
        ]
        reply = replies[-1].strip() if replies else ""
        # 常驻记忆也要有界，否则一个活跃的群会把上下文越滚越大。
        if len(eng.messages) > _HISTORY_LIMIT * 3:
            eng.messages[:] = eng.messages[-(_HISTORY_LIMIT * 3) :]
        return reply

    @staticmethod
    async def _drive(eng: Any, prompt: str) -> None:
        async for _event in eng.run(prompt):
            pass

    def close(self) -> None:
        with self._lock:
            self._engines.clear()
