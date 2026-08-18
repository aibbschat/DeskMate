"""圆桌参与者人格定义（数据层已迁移到上游官方 persona 底座）。

设计（落在官方底座 ``coworker.personas.registry`` 之上）：
- 7 个进程内专家写成 ``coworker/personas/builtin/*.md``（带 ``roundtable: true`` 标记），
  由上游 PersonaRegistry 启动时自动扫描注册，普通聊天选择器与圆桌共用同一份花名册。
- 本模块只做「窄化 loader」：从官方 registry 过滤出 roundtable 专家，映射成圆桌用的
  ``Persona``（携带 id / name / role / system_prompt / market），供 coordinator 消费。
- 史实严谨是第一红线；各专家职责互不重叠；主持（桌搭）单独定义，见 coordinator.HOST_SYSTEM。

注意：新增 / 编辑角色走官方 Personas 设置页（上游 registry CRUD），不再维护手搓 yaml。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# 圆桌专家的展示稳定顺序（避免每轮 UI 抖动）；id 必须与 builtin/*.md 的 id 一致。
# 圆桌里的"文学编辑"对应上游 builtin 的 id ``lit-editor``（避开上游自带 editor.md 的撞车）。
ROUNDTABLE_ORDER = [
    "historian", "lit-editor", "proofreader", "reader",
    "publisher", "adaptation", "genre",
]


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    role: str  # 简短角色标签，用于 UI / 纪要
    system_prompt: str
    market: bool = False  # 是否为市场化人格（圆桌会向其注入「当前生效」的潮流简报）


def _load_from_registry() -> list[Persona]:
    """从官方 PersonaRegistry 取 roundtable 专家，按 ROUNDTABLE_ORDER 稳定排序。"""
    try:
        from coworker.personas.registry import get_registry

        entries = get_registry().list_all()
    except Exception:
        return []
    out: list[Persona] = []
    for e in entries:
        if not e.get("roundtable"):
            continue
        pid = e.get("id", "")
        out.append(
            Persona(
                id=pid,
                name=e.get("name", pid),
                role=e.get("roundtable_role") or e.get("tagline") or e.get("name", ""),
                system_prompt=e.get("system_prompt", ""),
                market=bool(e.get("roundtable_market")),
            )
        )
    # 稳定排序：ROUNDTABLE_ORDER 中的在前，其余按出现序
    order = {pid: i for i, pid in enumerate(ROUNDTABLE_ORDER)}
    out.sort(key=lambda p: order.get(p.id, len(order)))
    return out


def load_personas(force: bool = False) -> list[Persona]:
    """返回当前生效的圆桌专家列表（来自官方 registry，按稳定顺序）。"""
    return _load_from_registry()


def get_persona(pid: str) -> Optional[Persona]:
    for p in load_personas():
        if p.id == pid:
            return p
    return None


# 向后兼容：模块级快照（导入时从官方 registry 取一次）。
PERSONAS: list[Persona] = load_personas()
