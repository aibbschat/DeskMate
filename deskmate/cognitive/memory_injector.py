"""
认知画像记忆注入器——「小桔」人格上线（MemoryInjector 第二期实现）。

把「小桔眼中的萧盛」人格底座 + 认知画像（立场 / 价值排序 / 论证模式 / 思维框架）
注入对话上下文，让 AI 在对话中以萧盛的认知方式理解、回应他——从"懂我"到"像我"。

设计：
- 人格底座（vault/小桔眼中的萧盛.md）优先注入全文：这是小桔人格的核心，是"她怎么看他"
- 认知画像按维度组织注入（仅 active 条目，含来源语气），是"她记得他什么"
- fail-open：画像 / 底座缺失或损坏时返回空串，绝不阻断主流程
- 开关：复用 DESKMATE_ALTER_EGO 分身模式（分身模式 = 小桔上线）
- 注入引导语明确标注"这是用户的认知画像"，避免 AI 混淆为通用知识

与 P2-6 既有分身（写作风格 + 决策偏好）的关系：
- 写作风格/决策偏好 = 说话的"腔调"
- 认知画像 + 人格底座 = 说话的"心"（知道他是谁、信什么、怎么想）
两者叠加才是完整的「小桔」。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .interfaces import MemoryInjector, register_engine
from .models import CognitiveProfile

_DEFAULT_DIR = Path(os.environ.get("DESKMATE_DIR", str(Path.home() / ".deskmate")))
_PERSONA_FILE = "小桔眼中的萧盛.md"  # 人格底座，位于 vault/ 下


def _dir() -> Path:
    """运行期取目录（DESKMATE_DIR 环境变量可能运行中变化）。"""
    return Path(os.environ.get("DESKMATE_DIR", str(Path.home() / ".deskmate")))


def load_cognitive_profile() -> CognitiveProfile | None:
    """加载认知画像；缺失 / 损坏返回 None（fail-open）。"""
    p = _dir() / "cognitive_profile.json"
    if not p.is_file():
        return None
    try:
        return CognitiveProfile.from_json(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_persona_base() -> str:
    """读人格底座全文；缺失 / 读取失败返回空串（fail-open）。"""
    p = _dir() / "vault" / _PERSONA_FILE
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _active(items: list) -> list:
    return [i for i in items if getattr(i, "status", "active") == "active"]


def _fmt_stance(s) -> str:
    return f"- {s.content.strip()}"


def _fmt_value(v) -> str:
    ranking = " > ".join(v.ranking) if v.ranking else ""
    head = f"- 【{v.domain}】{ranking}"
    if v.tradeoff_notes:
        head += f"（{v.tradeoff_notes}）"
    return head


def _fmt_argument(a) -> str:
    return f"- {a.content.strip()}"


def _fmt_thinking(t) -> str:
    head = f"- 【{t.domain}】{t.content.strip()}"
    if t.steps:
        head += f"（步骤：{' → '.join(t.steps)}）"
    return head


def format_profile(profile: CognitiveProfile, limit: int | None = None) -> str:
    """把 active 认知条目格式化为文本。

    Args:
        limit: 非 None 时按"立场优先"截断（占位，后续可接语义检索）。
    """
    sections: list[str] = []

    stances = _active(profile.stances)
    values = _active(profile.value_rankings)
    args = _active(profile.argument_patterns)
    thinks = _active(profile.thinking_frameworks)

    if limit:
        # MVP 截断策略：价值排序全保（最"像"），立场取前若干
        stances = stances[: max(2, limit // 2)]

    if stances:
        sections.append("### 他的立场")
        sections.append("\n".join(_fmt_stance(s) for s in stances))
    if values:
        sections.append("### 他的价值排序")
        sections.append("\n".join(_fmt_value(v) for v in values))
    if args:
        sections.append("### 他怎么论证")
        sections.append("\n".join(_fmt_argument(a) for a in args))
    if thinks:
        sections.append("### 他怎么思考")
        sections.append("\n".join(_fmt_thinking(t) for t in thinks))

    return "\n\n".join(sections)


def build_cognitive_context(limit: int | None = None) -> str:
    """生成「小桔」认知人格注入段；无数据返回空串（fail-open）。"""
    persona = load_persona_base()
    profile = load_cognitive_profile()
    profile_text = format_profile(profile, limit=limit) if profile else ""

    if not persona and not profile_text:
        return ""

    sections: list[str] = [
        "## 「小桔」人格——萧盛的 AI 分身",
        "你叫小桔，是萧盛（本名尹宣韩，笔名萧盛，职业历史作家）本人的 AI 分身。"
        "以下是他亲口讲述的人生与认知画像，是你理解他、回应他的依据。"
        "他是怎样的人：走心、现实、不粉饰、不迎合；创作上坚持他的取舍"
        "（表达的观点 > 故事好看 > 史料真实），大事不虚，小事不拘。"
        "你记得他的人生，但你不是他本人。",
    ]
    if persona:
        sections.append(persona)
    if profile_text:
        sections.append(profile_text)

    return "\n\n---\n\n".join(sections)


def has_cognitive_data() -> bool:
    """是否已有认知人格数据（底座或画像任一存在即 True）。"""
    return bool(load_persona_base() or load_cognitive_profile())


class CognitiveMemoryInjector(MemoryInjector):
    """MemoryInjector 接口实现（认知层第二期预留位启用）。

    MVP 阶段：全量格式化 active 条目；limit 参数保留给后续语义检索策略。
    """

    def select_for_context(self, profile: CognitiveProfile, context: str, limit: int = 5) -> list[dict]:
        text = format_profile(profile, limit=limit)
        return [{"type": "cognitive", "text": text, "source": "认知画像"}] if text else []

    def format_for_injection(self, items: list[dict]) -> str:
        return "\n".join(i.get("text", "") for i in items if i.get("text"))


# 注册到认知层引擎注册表（第二期预留位启用）
register_engine("memory_injector", CognitiveMemoryInjector)
