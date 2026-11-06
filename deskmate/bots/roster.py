"""bot 名册 —— **不新建存储层**，bot 就是官方 persona profile。

Hermes Bot Mode 的核心工程选择（本地 ~/.hermes/hermes-agent 源码核实）：
"a bot is just a Hermes profile"，插件只是 profile primitive 上面的一层 UI，
没有新 storage layer、没有后台 daemon。Grok Bot 同构：每个 bot 是有名字、
有持久状态的 AI 同事，跨会话记忆不归零。

这里同构落到：bot = ``coworker/personas/builtin/*/manifest.md`` 注册项。
身份 / 人格 / 记忆 / 模型都由官方 registry 与 pre-spawn session 承载，
本模块只做「窄化 + 稳定排序」的投影，绝不复制一份名册出来。

阶段 1 的生效范围是已在 registry 注册且标 ``roundtable: true`` 的创作专家
（右侧「我的伙伴」同源）。主编线 6 个 manifest 在阶段 2 标 ``bot: true`` 后
经 ``BOT_ORDER`` 扩入，无需改本模块的读取逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

# 与 deskmate/roundtable/personas.py:ROUNDTABLE_ORDER 对齐。此处自带一份稳定 id 序，
# 故意不 import 那个模块 —— 它有模块级 load_personas() 副作用（import 期扫 registry），
# 会违反「禁 import 期副作用」红线。阶段 2 统一名册时再收敛成单一来源。
BOT_ORDER: tuple[str, ...] = (
    "historian", "lit-editor", "brainstormer", "proofreader", "reader",
    "publisher", "adaptation", "genre",
)

# 阶段 1 允许被拉进群的 bot。未知 id 一律拒绝，避免把「临时人设」混进来。
KNOWN_BOT_IDS: frozenset[str] = frozenset(BOT_ORDER)


@dataclass(frozen=True)
class Bot:
    """一个常驻 bot 的投影。实体本体在官方 registry / 独立 session，不在这里。"""

    id: str
    name: str
    role: str
    system_prompt: str
    market: bool = False


def bots_from_entries(entries: Iterable[Mapping[str, Any]]) -> list[Bot]:
    """把 PersonaRegistry 的 list_all() 条目投影成 bot 名册（纯函数，无 I/O）。

    未标 ``roundtable`` 的 persona 不进创作群；排序按 BOT_ORDER 稳定，避免 UI 抖动。
    """
    out: list[Bot] = []
    for entry in entries or ():
        if not entry.get("roundtable"):
            continue
        pid = str(entry.get("id", "")).strip()
        if not pid:
            continue
        out.append(
            Bot(
                id=pid,
                name=str(entry.get("name") or pid),
                role=str(
                    entry.get("roundtable_role")
                    or entry.get("tagline")
                    or entry.get("name")
                    or ""
                ),
                system_prompt=str(entry.get("system_prompt") or ""),
                market=bool(entry.get("roundtable_market")),
            )
        )
    order = {pid: i for i, pid in enumerate(BOT_ORDER)}
    out.sort(key=lambda b: order.get(b.id, len(order)))
    return out


def list_bots() -> list[Bot]:
    """读官方 PersonaRegistry。取不到时返回空列表（fail-open，绝不伪造 bot）。"""
    try:
        from coworker.personas.registry import get_registry

        entries: Sequence[Mapping[str, Any]] = get_registry().list_all()
    except Exception:  # noqa: BLE001
        return []
    return bots_from_entries(entries)
