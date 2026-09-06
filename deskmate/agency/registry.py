"""L2 任务白名单注册表 —— 动作类型的唯一真相源。

每个「自主动作类型」在此登记其**内禀风险等级**与描述。风险闸（risk.py）
只读这里，不在别处硬编码。白名单（settings.whitelist.actions）只决定
「这个动作类型当前开没开」；风险等级是动作本身的属性，用户不改。

风险分级（2026-09-05 拍板）：
- low（低风险，可自主执行）：搜索 / 总结 / 整理笔记 —— 只读或只写本地记忆
- high（高风险，必走提议卡）：写文件 / 发消息 / 删除 —— 有外部或不可逆 side effect
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ActionSpec:
    """一个自主动作类型的元信息。"""

    action_type: str
    risk: str            # "low" | "high"
    label: str
    description: str
    builtin: bool = True


# 全量动作类型登记表。新增自主行为只需在此加一行 + 在 engine.py 的调度里接上。
REGISTRY: dict[str, ActionSpec] = {
    # —— 低风险（可自主）——
    "world_search": ActionSpec(
        action_type="world_search",
        risk="low",
        label="主动信息摄入",
        description="按白名单主题域搜索近况，抽要点记入长期记忆（只读外部网络，写本地 learned_history）",
    ),
    "rss_digest": ActionSpec(
        action_type="rss_digest",
        risk="low",
        label="RSS 简报",
        description="拉取订阅的 RSS，生成领域简报（P1 预留）",
        builtin=False,
    ),
    "note_organize": ActionSpec(
        action_type="note_organize",
        risk="low",
        label="笔记整理",
        description="本地整理/归类笔记（P1 预留）",
        builtin=False,
    ),
    # —— 高风险（必走提议卡）——
    "write_file": ActionSpec(
        action_type="write_file",
        risk="high",
        label="写文件",
        description="在磁盘上创建/修改文件——有不可逆 side effect，必须经用户确认",
    ),
    "send_message": ActionSpec(
        action_type="send_message",
        risk="high",
        label="发消息",
        description="对外发送消息（微信/邮件等）——有外部副作用，必须经用户确认",
    ),
    "delete": ActionSpec(
        action_type="delete",
        risk="high",
        label="删除",
        description="删除文件/记忆/数据——不可逆，必须经用户确认",
    ),
}


def get_spec(action_type: str) -> ActionSpec | None:
    return REGISTRY.get(action_type)


def is_known(action_type: str) -> bool:
    return action_type in REGISTRY


def list_specs() -> list[ActionSpec]:
    return list(REGISTRY.values())


def describe(action_type: str) -> dict[str, Any]:
    spec = REGISTRY.get(action_type)
    if spec is None:
        return {"action_type": action_type, "risk": "unknown", "label": action_type, "description": ""}
    return {
        "action_type": spec.action_type,
        "risk": spec.risk,
        "label": spec.label,
        "description": spec.description,
    }
