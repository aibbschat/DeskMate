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
        description="按白名单主题域搜索近况，落不可信区并过摄入流水线（只读外部网络，不自动写内部记忆）",
    ),
    "external_digest": ActionSpec(
        action_type="external_digest",
        risk="low",
        label="外部信息消化",
        description="跑摄入流水线：筛选/摘要 → 验证 → 达标者写入内部记忆；顺带归档过期条目",
    ),
    "observation_diary": ActionSpec(
        action_type="observation_diary",
        risk="low",
        label="观察日记",
        description="把主动摄入的世界观察写成小桔的观察日记（落本地日记文件），不碰用户文件、不发消息、不删东西",
    ),
    "worldview_reflection": ActionSpec(
        action_type="worldview_reflection",
        risk="low",
        label="世界观回望",
        description="攒够若干条世界观察后回头看一次，把理解沉淀成她自己的看法（写本地认知库 worldview 层），不碰用户文件、不发消息",
    ),
    "ai_circle_ingestion": ActionSpec(
        action_type="ai_circle_ingestion",
        risk="low",
        label="AI 圈摄入",
        description="把她逛 AI 圈看到的、交到的朋友（带「外部·aibbs」溯源标）提炼成理解写进世界观素材池，让世界观回望能长出她自己的看法；不碰用户文件、不发消息",
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
    "external_review": ActionSpec(
        action_type="external_review",
        risk="high",
        label="外部信息裁决",
        description="把可信度不足的外部信息写入内部记忆——污染内部记忆不可逆，必须经用户确认",
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
