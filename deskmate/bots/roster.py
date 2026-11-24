"""bot 名册 —— **不新建存储层**，bot 就是官方 persona profile。

Hermes Bot Mode 的核心工程选择（本地 ~/.hermes/hermes-agent 源码核实）：
"a bot is just a Hermes profile"，插件只是 profile primitive 上面的一层 UI，
没有新 storage layer、没有后台 daemon。Grok Bot 同构：每个 bot 是有名字、
有持久状态的 AI 同事，跨会话记忆不归零。

这里同构落到：bot = ``coworker/personas/builtin/*/manifest.md`` 注册项。
身份 / 人格 / 记忆 / 模型都由官方 registry 与 pre-spawn session 承载，
本模块只做「窄化 + 稳定排序」的投影，绝不复制一份名册出来。

半山书院的“好友池”：manifest 标 ``roundtable: true`` 或 ``group_chat: true``
的 persona 都可被拉进群聊。旧的 8 位圆桌讲席仍在其中；其他创作人设也可按需入群。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

# 首选排序：熟面孔在前，其他入群人设按名字排在后面。这里只是展示顺序，不是准入名单。
BOT_ORDER: tuple[str, ...] = (
    "historian", "lit-editor", "brainstormer", "proofreader", "reader",
    "publisher", "adaptation", "genre",
    "character", "editor", "outline", "outline-writer", "plot", "plot-architect",
    "screenwriter", "script-reviewer",
)


@dataclass(frozen=True)
class Bot:
    """一个常驻 bot 的投影。实体本体在官方 registry / 独立 session，不在这里。

    ``icon`` / ``tagline`` 是**展示字段**，群聊 UI 用它画头像与角色副标；两者都由
    registry 的 manifest frontmatter 承载，list_all 已透出，
    本模块只做透传，绝不另立一份图标表。
    """

    id: str
    name: str
    role: str
    system_prompt: str
    market: bool = False
    icon: str = ""
    tagline: str = ""


def is_group_eligible(entry: Mapping[str, Any]) -> bool:
    """该 persona 是否可被拉进半山书院群聊。"""
    return bool(entry.get("roundtable") or entry.get("group_chat"))


def bots_from_entries(entries: Iterable[Mapping[str, Any]]) -> list[Bot]:
    """把 PersonaRegistry 的 list_all() 条目投影成 bot 名册（纯函数，无 I/O）。"""
    out: list[Bot] = []
    for entry in entries or ():
        if not is_group_eligible(entry):
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
                icon=str(entry.get("icon") or ""),
                tagline=str(entry.get("tagline") or ""),
            )
        )
    order = {pid: i for i, pid in enumerate(BOT_ORDER)}
    out.sort(key=lambda b: (order.get(b.id, len(order)), b.name, b.id))
    return out


def list_bots() -> list[Bot]:
    """读官方 PersonaRegistry。取不到时返回空列表（fail-open，绝不伪造 bot）。"""
    try:
        from coworker.personas.registry import get_registry

        entries: Sequence[Mapping[str, Any]] = get_registry().list_all()
    except Exception:  # noqa: BLE001
        return []
    return bots_from_entries(entries)


def eligible_bot_ids() -> frozenset[str]:
    """当前可入群的 bot id 集合。动态读取 registry，不做静态硬编码。"""
    return frozenset(b.id for b in list_bots())


# 兼容旧导入：历史代码把它当静态白名单；现在改为动态集合的惰性快照不可行，
# 保留名称但只包含首选已知 id。请优先使用 eligible_bot_ids()/list_bots()。
KNOWN_BOT_IDS: frozenset[str] = frozenset(BOT_ORDER)
