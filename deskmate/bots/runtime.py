"""真 Agent 运行时 —— 创作专家从「人设卡」升级为有 engine、有工具、自主决策的 bot。

萧哥 11-06 决策 2 的核实结论（代码实证，见 docs/bot-mode-redesign.md）：
专家的 ``coworker/personas/builtin/*.md`` 本来就是**完整 persona**（有 tools、
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
   群历史上限 20 条 / 总量 18000 字（**单条发言不截断**），读稿单次上限
   12000 字（读不全会把未加载章节名交回 bot 让它自己续读）。
   不 @ 不烧钱，@ 谁谁答。
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Optional, Sequence

from coworker.agent import build_engine
from coworker.personas.registry import get_registry

from .reading import BookShelf, default_shelf, make_reading_tools
from .roster import KNOWN_BOT_IDS

logger = logging.getLogger(__name__)

# 群聊层的行为约束：这是「在群里说人话」，不是派活 / 验收 / 写报告。
_GROUP_GUIDANCE = """

# 你现在在一个 bot 群里

- 你是群里的一员，用**自己的专长**说人话。**要具体**：给方向就交代伏笔埋在哪、
  读者会在哪一句愣住，按你人设里的输出格式写足，别为压字数砍掉可执行细节；
  不写成验收报告／工作汇报的体例，也不写「可以考虑加个反转」这类空话。
  纯接话、补一句的轮次不必重复展开已经说过的部分。
- 群里若已共享书稿材料（小桔逐日沉淀 + 原文分块 + 完整目录），**直接引用那份**，
  不必从头翻目录。她的进度简报写着今天写到哪一章、停在哪一句，圣典是带正文引文的
  事实基准；给「接下来怎么写」的方向时先对齐这些，别拿开头第一章去硬编后段情节。
  需要里面没有的正文或大纲时，自己用 read_book 按文件名关键词点名补读。
  绝不问用户要路径、绝不要求填任何表格字段。读不到就直说读不到，不要编造情节或段号。
- 只读，不写文件、不改稿、不派活、不建工作项、不做审核验收、不打回重改。
- **不生成纪要 / 总结 / 主持人收尾**；不要替别人发言，也不要复述群历史。
- 群里已经显示你的名字，**不要**在发言开头加署名、来源标注、@ 前缀或
  「（来自…）」这类括号说明，直接从观点说起。
- 群里其他人的发言已经在上下文里，接话而不是重新开始。
- 后面几轮如果没有新的可说，直接回复 pass，不要为了凑数重复自己的观点。
"""

_HISTORY_LIMIT = 20
# 群历史给模型的总字数预算（不是单条发言上限）。旧实现把每条硬砍到 600 字，
# 1676 字的具体建议只传了前 1/3 给后发言者，讨论必然退化成空泛附和 —— 撤掉
# 单条截断，改用「总量有界 + 从最新往回取」：够装下 10 条完整长发言，又不随
# 轮次线性膨胀。真正限制长篇的是人设输出格式与 provider 的 max_tokens，不是这里。
_HISTORY_BUDGET = 18_000
_DEFAULT_MAX_ITERATIONS = 4

# 模型爱在发言开头自加一行署名/来源标注。prompt 里已明令禁止，但模型不保证遵守
# （2026-11-06 真机实测：补厚人设后仍出现「（来自 @brainstormer · 创意脑洞专家）」）。
# 这是模型自加的**元数据噪声**，不是表达内容 —— 落库前做确定性剥离，
# 不靠模型自觉、也不加任何语气/话术约束（守「真实优先」红线）。
# 只认开头单独一行的两种形态：括号「来自 @…」或「@id：」；正文里的括号不受影响。
_ATTRIBUTION_LINE = re.compile(
    r"^\s*(?:[（(]\s*来自\s*@?[^)）]{0,80}[)）]|@[\w.\-]+\s*[:：])\s*$"
)


def strip_self_attribution(text: str) -> str:
    """剥掉开头那一行的自加署名，正文一字不改（纯字符串清洗，零 LLM）。"""
    s = str(text or "")
    lines = s.splitlines(keepends=True)
    if not lines:
        return ""
    first = lines[0].rstrip("\r\n")
    if not _ATTRIBUTION_LINE.match(first):
        return s.strip()
    return "".join(lines[1:]).lstrip("\r\n").strip()


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


def format_history(
    history: Sequence[dict] | None,
    *,
    names: Mapping[str, str] | None = None,
) -> str:
    """群历史 → 一段纯文本上下文（有上限，防上下文膨胀烧钱）。

    ``names`` 把 ``author``（bot_id，如 historian）映射成群里显示的中文名
    （如「史料考据」）—— 人设里的接话示范写的都是中文名，模型看到
    ``historian（bot）: ...`` 是认不出「文学编辑说…」该接谁的。
    """
    rows = list(history or [])[-_HISTORY_LIMIT:]
    if not rows:
        return ""
    lookup = {str(k): str(v) for k, v in (names or {}).items()}
    lines: list[str] = []
    used = 0
    omitted = 0
    # 从最新往回取：预算不够时优先保住最近的完整发言（结论通常在结尾）。
    for m in reversed(rows):
        author = str(m.get("author") or "?")
        who = lookup.get(author, author)
        role = str(m.get("author_role") or "")
        tag = "（我）" if role == "user" else f"（{role}）"
        text = str(m.get("text") or "").strip()
        if not text:
            continue
        room = _HISTORY_BUDGET - used
        if room <= 0:
            omitted += 1
            continue
        if len(text) > room:
            text = "…" + text[-room:]
        used += len(text)
        lines.append(f"{who}{tag}: {text}")
    if not lines:
        return ""
    lines.reverse()
    if omitted:
        lines.insert(0, f"（更早 {omitted} 条发言未载入上下文）")
    return "群内已有发言：\n" + "\n".join(lines)


def build_prompt(
    text: str,
    history: Sequence[dict] | None,
    *,
    names: Mapping[str, str] | None = None,
    round_index: int = 1,
    materials: Any = None,
) -> str:
    """构造一次发言的 prompt。

    第 1 轮由用户原话驱动；第 2 轮起是 bot 之间接话，**不重复用户原话**
    （它已经在群历史里了），改为要求挑一个具体观点回应或显式 pass。

    ``materials`` 是群级共享材料包（``RoomMaterials.prepare`` 的产物），放在
    最前面当**固定前缀**：同一群所有 bot 拿到字节一致的一份，既省掉各自
    「找文件」的往返，也让 provider 的前缀缓存有机会命中。它是原文分块，
    不是摘要；不传时行为与旧版完全一致。
    """
    prior = format_history(history, names=names)
    if round_index <= 1:
        lead = f"群里刚有人说：{text}\n\n请直接以你自己的身份发言。"
    else:
        lead = (
            f"这是第 {round_index} 轮接话。前面几位的发言都在上下文里，"
            "请挑一个具体观点回应、补充或提出不同意见，不要复述别人说过的话；"
            "如果没有新的可说，直接回复 pass。"
        )
    block = ""
    if materials is not None:
        block = (
            materials.prompt_block()
            if hasattr(materials, "prompt_block")
            else str(materials)
        )
    return (block + "\n\n" if block else "") + (prior + "\n\n" if prior else "") + lead


def _safe_segment(value: str) -> str:
    """room_id 落进目录名前的兜底清洗（group_id 正常是 uuid hex）。"""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value))[:64] or "_default"


class BotRuntime:
    """每个 ``(room, bot)`` 一个常驻 engine，群只是临时协调面。

    ``provider`` 可注入，便于离线测试真 Agent 的自主工具决策；默认走付费通道。

    上下文只有一份：``ChatStore`` 的群历史（截断 20 条）+ 当前这句。engine
    每轮开始前清空 ``messages``，不再把群历史的副本累积进去 —— 否则
    「群历史 / engine.messages / 当前句」会是三重冗余，既费 token 又让 bot 复读。
    跨发言的连续记忆由群历史承载，且天然按 room 隔离，A 群的话不会漏进 B 群。
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
        # key = (room_id, bot_id)：同一个 bot 在不同群是两份独立会话，不串味。
        self._engines: dict[tuple[str, str], Any] = {}
        self._lock = threading.RLock()
        # 同一个 (room, bot) 不能同时跑两个 turn（messages 会被交错写）。
        # 用 threading.Lock 而非 asyncio.Lock：speak() 走 threadpool / 独立 loop，
        # speak_stream() 走 FastAPI 主 loop，threading 锁跨两个上下文都成立。
        self._turn_locks: dict[tuple[str, str], threading.Lock] = {}

    @staticmethod
    def _key(room_id: str | None, bot_id: str) -> tuple[str, str]:
        return (str(room_id or "_default"), str(bot_id))

    def _turn_lock(self, key: tuple[str, str]) -> threading.Lock:
        with self._lock:
            lock = self._turn_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._turn_locks[key] = lock
            return lock

    # ---------------------------------------------------------------- engine
    def engine(self, bot_id: str, *, room_id: str | None = None) -> Any:
        """构造（并缓存）该 bot 在该群的官方 TurnEngine。

        ``room_id`` 省略时落进 ``_default`` 桶（兼容阶段 2 的离线调用与测试）。
        """
        bot_id = str(bot_id).strip()
        if bot_id not in KNOWN_BOT_IDS:
            raise ValueError(f"未知 bot：{bot_id}（bot 必须来自常驻名册）")
        key = self._key(room_id, bot_id)
        with self._lock:
            if key in self._engines:
                return self._engines[key]

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
            ws = self.workspace_root / _safe_segment(key[0]) / bot_id
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
            self._engines[key] = eng
            return eng

    # ---------------------------------------------------------------- speak
    def speak(
        self,
        bot_id: str,
        text: str,
        *,
        history: Sequence[dict] | None = None,
        names: Mapping[str, str] | None = None,
        room_id: str | None = None,
        round_index: int = 1,
        materials: Any = None,
    ) -> str:
        """说一句人话 → bot 自主决定要不要读稿 → 返回它这一轮的署名发言。"""
        eng = self.engine(bot_id, room_id=room_id)
        with self._turn_lock(self._key(room_id, bot_id)):
            return self._speak_locked(
                eng,
                text,
                history=history,
                names=names,
                round_index=round_index,
                materials=materials,
            )

    def _speak_locked(
        self,
        eng: Any,
        text: str,
        *,
        history: Sequence[dict] | None,
        names: Mapping[str, str] | None,
        round_index: int,
        materials: Any = None,
    ) -> str:
        """兼容旧 ``/say`` 契约的一次性发言（整段返回）。"""
        # 群历史是唯一上下文源：清掉上一轮的累积副本，避免三重冗余 / 复读。
        eng.messages.clear()
        prompt = build_prompt(
            text, history, names=names, round_index=round_index, materials=materials
        )
        before = len(eng.messages)
        _run_sync(self._drive(eng, prompt))
        replies = [
            str(m.get("content") or "")
            for m in eng.messages[before:]
            if m.get("role") == "assistant" and str(m.get("content") or "").strip()
        ]
        return strip_self_attribution(replies[-1].strip()) if replies else ""

    @staticmethod
    async def _drive(eng: Any, prompt: str) -> None:
        """旧契约的一次性发言。事件不往前端推，但 **error 绝不吞**：
        engine 的 payload 是 ``{"error": ..., "error_type": ...}``，旧实现全部
        pass 掉，配额超限 / 通道 429 会被误当成「bot 没说话」。抛出去让
        ``RoomStore.say`` 的 except 记下真实原因（行为仍是不落库）。"""
        async for event in eng.run(prompt):
            kind = getattr(getattr(event, "type", None), "value", None)
            if kind is None:
                kind = str(getattr(event, "type", ""))
            if kind == "error":
                data = getattr(event, "data", None) or {}
                detail = str(data.get("error") or data.get("message") or "bot 发言出错")
                etype = str(data.get("error_type") or "error")
                raise RuntimeError(f"{etype}: {detail}")

    # ------------------------------------------------------------ speak stream
    async def speak_stream(
        self,
        bot_id: str,
        text: str,
        *,
        history: Sequence[dict] | None = None,
        names: Mapping[str, str] | None = None,
        room_id: str | None = None,
        round_index: int = 1,
        materials: Any = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """**原生流式发言**：把官方 TurnEngine 的逐 token 事件原样透出来。

        与 ``speak()`` 不是两套 LLM 流 —— 同一 engine / 同一 prompt / 同一份常驻
        记忆，唯一区别是旧的 ``_drive`` 用 ``pass`` 把事件全丢了，这里把
        ``ASSISTANT_DELTA`` 转成 ``delta`` 事件给 SSE 消费。

        事件：``tool``（bot 自主读稿时）/ ``delta``（逐 token）/ ``error`` /
        ``final``（权威最终文本，供落库与前端覆盖气泡，保证与 delta 拼接一致）。

        被 ``aclose()``（客户端断开 → GeneratorExit）时调 ``request_interrupt()``
        真正掐断 provider 流，避免「页面关了 token 还在烧」（engine.py:316）。
        """
        eng = self.engine(bot_id, room_id=room_id)
        lock = self._turn_lock(self._key(room_id, bot_id))
        if not lock.acquire(blocking=False):
            # 非阻塞：拿不到锁不能阻塞 FastAPI 的 event loop。
            yield {"type": "error", "message": f"{bot_id} 正在发言，请稍候再试"}
            return
        try:
            eng.messages.clear()
            prompt = build_prompt(
                text, history, names=names, round_index=round_index,
                materials=materials,
            )
            streamed: list[str] = []
            final_text = ""
            try:
                async with contextlib.aclosing(eng.run(prompt)) as events:
                    async for event in events:
                        kind = getattr(event.type, "value", str(event.type))
                        data = event.data or {}
                        if kind == "assistant_delta":
                            piece = str(data.get("text") or "")
                            if piece:
                                streamed.append(piece)
                                yield {"type": "delta", "text": piece}
                        elif kind == "tool_started":
                            yield {
                                "type": "tool",
                                "name": str(data.get("name") or ""),
                            }
                        elif kind == "assistant_message":
                            # 带 tool_calls 的是中间轮（模型边调工具边说的话），
                            # 只有不带工具调用的最后一轮才是要落库的正式发言。
                            if not data.get("tool_calls"):
                                final_text = strip_self_attribution(
                                    str(data.get("text") or "")
                                )
                        elif kind == "error":
                            # engine 给的是 {"error": ..., "error_type": ...}，不是
                            # "message"。旧代码把它吞成「bot 发言出错」，429 配额
                            # 超限看起来就像 bot 弃权，无法判断该等还是该修。
                            detail = str(
                                data.get("error") or data.get("message") or "bot 发言出错"
                            )
                            yield {
                                "type": "error",
                                "message": detail,
                                "error_type": str(data.get("error_type") or ""),
                            }
            except GeneratorExit:
                eng.request_interrupt()
                raise
            except Exception as exc:  # noqa: BLE001 —— 一个 bot 挂了不拖垮整群
                logger.warning("bot %s 流式发言失败：%s", bot_id, exc)
                yield {"type": "error", "message": str(exc)}
            else:
                # 兜底：某些 provider 不吐 ASSISTANT_MESSAGE，用 delta 拼接。
                if not final_text:
                    final_text = strip_self_attribution("".join(streamed))
                yield {"type": "final", "text": final_text}
        finally:
            lock.release()

    def close(self) -> None:
        with self._lock:
            self._engines.clear()
