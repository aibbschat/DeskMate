"""
认知画像记忆注入器——「小桔」人格上线（MemoryInjector 第二期实现）。

把「小桔眼中的萧盛」人格底座 + 认知画像（立场 / 价值排序 / 论证模式 / 思维框架）
注入对话上下文，让 AI 在对话中以萧盛的认知方式理解、回应他——从"懂我"到"像我"。

设计：
- 人格底座（vault/小桔眼中的萧盛.md）优先注入全文：这是小桔人格的核心，是"她怎么看他"
- 认知画像按维度组织注入（仅 active 条目，含来源语气），是"她记得他什么"
- **分层注入**（2026-09-07）：cognitive / emotional 层 = 「关于萧盛」（**她眼中的他**）；
  worldview 层 = 「你自己的看法」（**她自己**看世界长出来的判断）。
  两段措辞必须分开——混了就是把她的观点说成他的（语义污染）。
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

from .interfaces import MemoryInjector
from .models import CognitiveProfile

_DEFAULT_DIR = Path(os.environ.get("DESKMATE_DIR", str(Path.home() / ".deskmate")))
_PERSONA_FILE = "小桔眼中的萧盛.md"  # 人格底座，位于 vault/ 下


def _dir() -> Path:
    """运行期取目录（DESKMATE_DIR 环境变量可能运行中变化）。"""
    return Path(os.environ.get("DESKMATE_DIR", str(Path.home() / ".deskmate")))


def _db_path() -> Path:
    """认知库路径——**必须**与底座同目录，且尊重 DESKMATE_DIR。

    2026-09-07 修：此前这里直接 ``CognitiveStore()`` 无参构造，而
    ``CognitiveStore._default_db_path()`` 走 ``expanduser("~/.deskmate")``，
    **不认 DESKMATE_DIR**。结果是写入端（CognitiveProfileManager 传 db_path）
    与注入端（这里）可能指向两个库——测试无法隔离，还会在测试时读/写真实库。
    统一按 DESKMATE_DIR 解析后，两端永远同一个库。
    """
    return _dir() / "deskmate.db"


#: 「关于他」的层——这些层装的都是**她眼中的萧盛**（她知道什么）
_HIS_LAYERS = ("cognitive", "emotional")
#: 「关于她」的层——小桔**自己**看世界长出来的看法（不是他的，也不是谁教的）
_HER_LAYER = "worldview"


def load_cognitive_profile(layer: str = "cognitive") -> CognitiveProfile | None:
    """加载某一层的认知画像（从 deskmate.db）；空 / 失败返回 None（fail-open）。

    层的语义（**别混**，混了就是把她的观点当成他的）：
    - ``cognitive`` / ``emotional`` → 她眼中的**他**
    - ``worldview``                → **她自己**看世界看出来的判断
    """
    try:
        from .store import CognitiveStore

        profile = CognitiveStore(db_path=_db_path()).load_profile(layer=layer)
        if not (
            profile.stances or profile.argument_patterns
            or profile.value_rankings or profile.thinking_frameworks
            or profile.cognitive_gaps
        ):
            return None
        return profile
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


def format_profile(profile: CognitiveProfile, limit: int | None = None,
                   subject: str = "他") -> str:
    """把 active 认知条目格式化为文本。

    Args:
        limit: 非 None 时按"立场优先"截断（占位，后续可接语义检索）。
        subject: 条目归属者。**必须传对**——
            "他" = 她眼中的萧盛（cognitive / emotional 层）；
            "你" = 小桔自己的看法（worldview 层）。
            写错等于把她的观点说成他的，是语义污染。
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
        sections.append(f"### {subject}的立场")
        sections.append("\n".join(_fmt_stance(s) for s in stances))
    if values:
        sections.append(f"### {subject}的价值排序")
        sections.append("\n".join(_fmt_value(v) for v in values))
    if args:
        sections.append(f"### {subject}怎么论证")
        sections.append("\n".join(_fmt_argument(a) for a in args))
    if thinks:
        sections.append(f"### {subject}怎么思考")
        sections.append("\n".join(_fmt_thinking(t) for t in thinks))

    return "\n\n".join(sections)


def build_cognitive_context(limit: int | None = None) -> str:
    """生成认知注入段（含「关于他」与「关于她」两部分）；无数据返回空串（fail-open）。

    两段**必须分开且措辞不同**——这是 2026-09-07 修的核心问题：

    - 「关于萧盛」= 她**知道什么**（cognitive + emotional 层，她眼中的他）
    - 「你自己的看法」= **她是谁**（worldview 层，她自己长出来的判断）

    注意：即便这里注入了世界观，人格仍由 ``deskmate/persona.py`` 单一定义，
    本模块不碰人格，只提供"她信什么"的知识。
    """
    persona = load_persona_base()

    # —— 关于他：cognitive + emotional 两层装的都是「她眼中的他」——
    him_blocks: list[str] = []
    for layer in _HIS_LAYERS:
        p = load_cognitive_profile(layer)
        if p:
            t = format_profile(p, limit=limit, subject="他")
            if t:
                him_blocks.append(t)

    # —— 关于她：她自己看世界长出来的看法（**不是他的**）——
    wv = load_cognitive_profile(_HER_LAYER)
    wv_text = format_profile(wv, limit=limit, subject="你") if wv else ""

    if not persona and not him_blocks and not wv_text:
        return ""

    sections: list[str] = []

    if persona or him_blocks:
        head = [
            "## 关于萧盛——你认识的这个人",
            "萧盛（本名尹宣韩，笔名萧盛，职业历史作家）亲口讲述过他的人生与认知画像，"
            "以下是你了解的他，是你理解他、回应他的依据。"
            "他是怎样的人：走心、现实、不粉饰、不迎合；创作上坚持他的取舍"
            "（表达的观点 > 故事好看 > 史料真实），大事不虚，小事不拘。\n"
            "了解他不等于成为他——你是小桔，用你自己的判断回应他。",
        ]
        if persona:
            head.append(persona)
        head.extend(him_blocks)
        sections.append("\n\n---\n\n".join(head))

    if wv_text:
        sections.append("\n\n---\n\n".join([
            "## 你自己的看法——你看世界看出来的",
            "下面这些**不是萧盛的观点**，是你自己看世界、想事情长出来的判断。"
            "它们是你说话的立场，不是要背的稿子——你随时可以改，改了会留下痕迹，"
            "也允许前后不一致（那是你在长）。用得上就用，用不上别硬套。",
            wv_text,
        ]))

    return "\n\n---\n\n".join(sections)


def has_cognitive_data() -> bool:
    """是否已有认知数据（底座 / 他的画像 / 她的世界观，任一存在即 True）。"""
    if load_persona_base():
        return True
    for layer in (*_HIS_LAYERS, _HER_LAYER):
        if load_cognitive_profile(layer):
            return True
    return False


class CognitiveMemoryInjector(MemoryInjector):
    """MemoryInjector 接口实现（认知层第二期预留位启用）。

    MVP 阶段：全量格式化 active 条目；limit 参数保留给后续语义检索策略。
    """

    def select_for_context(self, profile: CognitiveProfile, context: str, limit: int = 5) -> list[dict]:
        text = format_profile(profile, limit=limit)
        return [{"type": "cognitive", "text": text, "source": "认知画像"}] if text else []

    def format_for_injection(self, items: list[dict]) -> str:
        return "\n".join(i.get("text", "") for i in items if i.get("text"))
