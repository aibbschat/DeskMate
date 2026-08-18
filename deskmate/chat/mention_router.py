"""@ 角色召唤路由（通用，不钉死圆桌）。

解析用户消息里的 ``@<id>`` / ``@<name>``，生成 handoff 协议文本注入到最新 user message，
让主 agent 临时以被 @ 角色的身份 / 视角参与协作——即「@ 补位」语义（会话身份由新建会话时
选定的 persona 决定，@ 只做当轮临时补位，不改变会话归属）。

对标 Hermes ``bot_mode_probe.py`` 的 "Messaging other agents" 协议，但做轻量化：
用 prompt 注入而非真 fork 子 agent，契合桌搭轻量 persona 定位，且普通聊天 / 圆桌通用。

数据源可插拔：``parse_mentions(text, roster_items)`` 接受任何带有 ``id`` / ``name`` /
``role``(或 ``tagline``) 属性的对象（上游 ``PersonaManifest``、圆桌 ``Persona``、dict 均可）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

# @ 后跟：ASCII 字母数字下划线连字符，或中日韩汉字。
# 负向后顾 ``(?!\.[A-Za-z])`` 排除邮箱（user@example.com 的 "@example" 后面紧跟 "."，不匹配）。
_MENTION_RE = re.compile(r"@([A-Za-z0-9_\-一-鿿]+)(?!\.[A-Za-z])")

# 开启开关：默认开启；设 DESKMATE_MENTION=0 可关闭（异常回退用）。
def _mention_enabled() -> bool:
    return os.environ.get("DESKMATE_MENTION", "1") != "0"


@dataclass
class MentionHit:
    raw: str          # 命中的标识原文（不带 @）
    persona_id: str
    name: str
    role: str


def _get(p: Any, key: str, default: Any = None) -> Any:
    """兼容对象（Persona / PersonaManifest）与 dict（上游 list_all）。"""
    if isinstance(p, dict):
        return p.get(key, default)
    return getattr(p, key, default)


def _role_of(p: Any) -> str:
    """取角色短描述：优先 role，其次 roundtable_role，再次 tagline，最后 name。"""
    role = _get(p, "role")
    if role:
        return str(role)
    rr = _get(p, "roundtable_role")
    if rr:
        return str(rr)
    tagline = _get(p, "tagline")
    if tagline:
        return str(tagline)
    return str(_get(p, "name", "") or "")


def _name_of(p: Any) -> str:
    return str(_get(p, "name", "") or "")


def resolve_roster(items: Iterable[Any]) -> tuple[dict, dict]:
    """建 id→对象、name.lower()→对象 两份索引（后者用于 @名字 命中）。"""
    by_id: dict[str, Any] = {}
    by_name: dict[str, Any] = {}
    for p in items:
        pid = _get(p, "id", None)
        if pid is not None:
            by_id[str(pid)] = p
        nm = _name_of(p)
        if nm:
            by_name[nm.strip().lower()] = p
    return by_id, by_name


def parse_mentions(text: str, roster_items: Iterable[Any]) -> list[MentionHit]:
    """从文本解析 @ 命中。未命中返回空列表（不报错，绝不误伤邮箱）。"""
    if not text or "@" not in text:
        return []
    by_id, by_name = resolve_roster(roster_items)
    hits: list[MentionHit] = []
    seen: set[str] = set()
    for m in _MENTION_RE.findall(text):
        p: Optional[Any] = None
        if m in by_id:
            p = by_id[m]
        elif m.lower() in by_name:
            p = by_name[m.lower()]
        else:
            # 模糊匹配：某角色 name 含 m，或 m 含某角色 name
            for cand in by_id.values():
                nm = _name_of(cand).lower()
                if nm and (m.lower() in nm or nm in m.lower()):
                    p = cand
                    break
        if p is None:
            continue
        pid = str(_get(p, "id"))
        if pid in seen:
            continue
        seen.add(pid)
        hits.append(
            MentionHit(
                raw=m,
                persona_id=pid,
                name=_name_of(p) or m,
                role=_role_of(p),
            )
        )
    return hits


def build_handoff_context(
    default_persona_id: Optional[str],
    default_name: str,
    hits: list[MentionHit],
) -> str:
    """生成注入到 user message 的 handoff 协议文本（对标 Hermes「Messaging other agents」）。"""
    if not hits:
        return ""
    lines = ["[协作召唤 · 角色 @ 注入]"]
    if default_name:
        lines.append(f"本会话默认声线：{default_name}（id={default_persona_id}）。")
    lines.append("用户在本条消息中 @ 了以下角色，请按召唤参与本轮协作：")
    for h in hits:
        lines.append(f"- @{h.persona_id}（{h.name}）：{h.role}")
    lines.append("协作规则：")
    lines.append(
        "1. 收到 @ 即视为被该角色召唤，请以其身份 / 口吻 / 专长作答或补充；"
        "多个角色时分别署名、各自负责其专长部分。"
    )
    lines.append(
        "2. 史实 / 考据类角色须标注出处（正史 / 实录 / 方志 / 档案 / 学术论文）；"
        "存疑须明示，绝不杜撰或含糊带过。"
    )
    lines.append(
        "3. 仅在被 @ 的这一轮生效，不覆盖默认声线的整体定位；未 @ 的轮次恢复默认声线。"
    )
    lines.append(
        "4. 若需要其他未提及的角色，先在本轮说明，不要擅自切换整个会话的默认声线。"
    )
    lines.append("署名：以 @角色 身份回复时，开头标注「（来自 @{id} · {name}）」。")
    return "\n".join(lines)


def unified_roster() -> list[Any]:
    """「@ 召唤」花名册：官方 PersonaRegistry 全量（@ 可唤起任意角色，与新建会话选择器共用同一份）。

    圆桌专家已作为 builtin manifest 注册进官方 registry，故这里只取官方 registry 一份，
    按 id 去重，避免重复。
    """
    items: list[Any] = []
    try:
        from coworker.personas.registry import get_registry

        for e in get_registry().list_all():
            pid = e.get("id")
            if pid is not None and not any(str(_get(p, "id", "")) == str(pid) for p in items):
                items.append(e)
    except Exception:
        pass
    return items


def mention_context(messages: Optional[list[dict[str, Any]]], default_persona_id: Optional[str] = None) -> str:
    """每轮注入入口：取最后一条 user 消息，解析 @，命中则返回协议文本，否则返回空串。"""
    if not _mention_enabled():
        return ""
    if not messages:
        return ""
    last = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                last = content
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        last = part.get("text", "")
                        break
            break
    if not last or "@" not in last:
        return ""
    roster = unified_roster()
    hits = parse_mentions(last, roster)
    if not hits:
        return ""
    default_name = ""
    if default_persona_id:
        for p in roster:
            if str(_get(p, "id", "")) == str(default_persona_id):
                default_name = _name_of(p)
                break
    return build_handoff_context(default_persona_id, default_name, hits)
