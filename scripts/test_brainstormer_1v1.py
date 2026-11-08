"""1v1 专家模式验证：创意脑洞专家（火山付费模型 + 两阶段作业法 + read_book 真 Agent）。

与 bot 群聊的唯一结构差异：
1. 不拼 _GROUP_GUIDANCE（没有 8 人群体规则、没有审查人格在场）
2. 模型走火山 Agent Plan 付费通道，不用 opencode-go flash
其余装配与 runtime.BotRuntime.engine() 保持一致（同 persona、同 read_book 工具、12 轮预算）。
"""
import asyncio
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coworker.agent import build_engine
from coworker.personas.registry import get_registry
from coworker.providers.router import ProviderRouter
from coworker.secrets import SecretStore
from deskmate.bots.reading import default_shelf, make_reading_tools

MODEL = "ark-agent-plan-cn:doubao-seed-2.1-turbo"  # 与 Hermes 脑洞 bot 同档
BOT_ID = "brainstormer"
WORKSPACE = Path.home() / ".config/coworker/bot_workspaces/1v1-brainstormer-test"

QUESTION = (
    "我现在在写的是 /Users/xiaocheng/Documents/已出版/16.夜不收/夜不收：绝命救援.docx，"
    "你可以先读原稿，然后帮我想一下，在龙山仓铁板一块的情况下，怎么才能完成任务。"
    "我需要脑洞大开的安排和设计。"
)


async def main() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    secrets = SecretStore()
    provider = ProviderRouter(secrets=secrets, default_provider="ark-agent-plan-cn")

    entry = get_registry().get(BOT_ID)
    assert entry is not None, "brainstormer persona 未注册"
    agent = entry.agent()
    tools = make_reading_tools(default_shelf())
    # 关键差异：system_prompt 原样使用，不拼 _GROUP_GUIDANCE
    agent = dataclasses.replace(
        agent,
        tool_factory=lambda _ctx: tools,
        requires_folder=False,
        scheduling=False,
        messaging=False,
        connectors=False,
        team=None,
    )

    eng = build_engine(
        agent=agent,
        provider=provider,
        model=MODEL,
        workspace=str(WORKSPACE),
        max_iterations=12,
    )
    eng._post_turn_hook = None  # 与群 bot 一致：不回写共享记忆
    allowed = {"read_book", "list_books"}
    eng.registry._tools = {
        name: spec for name, spec in eng.registry._tools.items() if name in allowed
    }

    tool_calls: list[str] = []
    final_text = ""
    async for event in eng.run(QUESTION):
        kind = getattr(getattr(event, "type", None), "value", None) or str(getattr(event, "type", ""))
        data = getattr(event, "data", None) or {}
        if kind == "tool_started":
            name = data.get("name") or data.get("tool") or (data.get("call") or {}).get("name") or "?"
            tool_calls.append(str(name))
        if kind == "error":
            raise RuntimeError(f"engine error: {data}")
        if kind == "assistant_delta":
            final_text += str(data.get("text") or "")
        if kind == "assistant_message":
            c = data.get("content") or data.get("text")
            if isinstance(c, str) and c.strip():
                final_text = c

    print("=" * 30, "工具调用序列", "=" * 30)
    print(tool_calls if tool_calls else "(模型未调用工具，直接作答)")
    print("=" * 30, "专家回答", "=" * 30)
    print(final_text.strip() or "(空回答)")


if __name__ == "__main__":
    asyncio.run(main())
