"""创作执行器：把「写大纲/写剧本/审」的具体生成从编排中解耦。

- ``StubExecutor``：不调 LLM，返回确定性占位文本，``review`` 默认通过（可注入一次
  拒绝用于验证打回链路）。开发/验证用。
- ``LLMExecutor``：复用 ``deskmate.memory.llm.chat`` 真实接入（阶段 1 留作清晰对接点，
  环境需配置 provider 凭证）。

两者都实现 ``CreativeExecutor`` 协议，pipeline 不关心背后是否真调 LLM。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ReviewResult:
    approved: bool
    opinion: str


def _parse_scenes(plan: str) -> list[tuple[str, str]]:
    """解析场景清单行「1.场景名|一句话目标」→ [(场景名, 目标), ...]。

    容错：无序号前缀 / 无「|」分隔的行也收纳（场景名或整行）。
    """
    out: list[tuple[str, str]] = []
    for line in (plan or "").splitlines():
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        body = re.sub(r"^\d+[\.、\)）]\s*", "", line)  # 去数字序号（1. / 2、/ 3)）
        body = re.sub(r"^[-–—*]\s*", "", body).strip()  # 去列表符号（- / — / *）
        if "|" in body:
            name, goal = body.split("|", 1)
            name = name.strip() or f"场景{len(out) + 1}"
            goal = goal.strip()
            out.append((name, goal))
        elif body:
            out.append((body, ""))
    return out


def _summarize_segment(seg: str) -> str:
    """把刚生成的剧本段压缩为简短前情摘要（LLM；不可用则截断兜底）。

    分段续写的关键：每段 prompt 只带「前情摘要」而非前文全文——保连贯的同时
    不随段数线性放大 token。
    """
    from deskmate.memory.llm import chat, llm_available

    fallback = f"（前情：{seg[:120]}…）"
    if not llm_available():
        return fallback
    try:
        s = chat(
            system="把剧本片段压缩为 150 字以内的前情摘要：只保留人物当前状态、"
            "关键事件、未解决的悬念，不要评价。",
            user=seg,
            max_tokens=300,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("前情摘要失败（截断兜底）: %s", exc)
        return fallback
    return (s or "").strip() or fallback


class CreativeExecutor:
    def draft_outline(self, concept: str, novel_text: str = "") -> str: ...
    def draft_screenplay(self, outline: str, novel_text: str = "") -> str: ...
    def review(self, text: str, criteria: str, novel_text: str = "") -> ReviewResult: ...


class StubExecutor:
    def __init__(self, reject_once: bool = False) -> None:
        self.reject_once = reject_once
        self._rejected = False

    def draft_outline(self, concept: str, novel_text: str = "") -> str:
        tag = f"·附原著{len(novel_text)}字" if novel_text else ""
        return f"[大纲·stub{tag}] {concept[:60]}"

    def draft_screenplay(self, outline: str, novel_text: str = "") -> str:
        tag = f"·附原著{len(novel_text)}字" if novel_text else ""
        return f"[剧本·stub{tag}] {outline[:60]}"

    def review(self, text: str, criteria: str, novel_text: str = "") -> ReviewResult:
        if self.reject_once and not self._rejected:
            self._rejected = True
            return ReviewResult(approved=False, opinion="第二幕逻辑断裂，建议重写")
        return ReviewResult(approved=True, opinion="结构完整、无硬伤，通过")


class LLMExecutor:
    def draft_outline(self, concept: str, novel_text: str = "") -> str:
        from deskmate.memory.llm import chat, llm_available

        if not llm_available():
            return ""
        novel_block = (
            f"\n\n【原著参考（改编依据）】\n{novel_text}\n（请基于上述原著精神进行改编，"
            "尊重人物与核心情节，允许合理的影视化再创作。）" if novel_text else ""
        )
        return chat(
            system="你是资深大纲专家。把用户的概念/剧情点子写成结构完整、"
            "有戏剧冲突的剧本大纲，标注主要人物与幕结构。若提供原著参考，须基于原著精神改编。",
            user=concept + novel_block,
            max_tokens=2000,
        )

    def draft_screenplay(self, outline: str, novel_text: str = "") -> str:
        """分段续写拼接（阶段 5 输出侧）——单次 8k token 出不了长剧，工程拆：

        1) LLM 把大纲拆成 3–6 个连续场景；
        2) 逐段生成（每段限额内），段间用「前情摘要」保连贯（不塞全文，token 不
           随段数线性放大）；
        3) 拼接完整稿返回，供 docx 落地 / 审核 / 圆桌终审。
        """
        from deskmate.memory.llm import chat, llm_available

        if not llm_available():
            return ""
        novel_block = (
            f"\n\n【原著参考（改编依据，含改编圣经+相关章节）】\n{novel_text}\n"
            "（请基于原著精神展开剧本，保持人物与核心情节一致。）" if novel_text else ""
        )
        # 1) 拆场景（拆不动则整段写）
        plan = chat(
            system="你是编剧统筹。把给定大纲拆成连续场景清单，每行严格格式："
            "序号.场景名|一句话目标。只输出清单，不要其他内容。",
            user=f"大纲：\n{outline}",
            max_tokens=800,
        )
        scenes = _parse_scenes(plan)
        if len(scenes) < 2:
            scenes = [("全剧", "按大纲完整撰写")]
        # 2) 逐段生成 + 前情摘要续写
        segments: list[str] = []
        prev_summary = "（这是第一段，尚未有前情）"
        base_user = f"{outline}{novel_block}"
        for i, (name, goal) in enumerate(scenes, 1):
            user = (
                f"{base_user}\n\n"
                f"【当前场景 {i}/{len(scenes)}】{name}\n"
                f"场景目标：{goal}\n"
                f"【前情摘要】{prev_summary}\n\n"
                f"请撰写本场景剧本正文：台词节奏、可拍性、人物行为与语气连贯，"
                f"并与前情自然衔接。"
            )
            seg = chat(
                system="你是编剧。依据大纲与原著参考撰写剧本正文，注意场景边界清晰、"
                "人物台词个性鲜明、可拍性。每段只写当前场景，不要重复前情。",
                user=user,
                max_tokens=3800,
            )
            if not seg or not seg.strip():
                logger.warning("场景 %s 生成为空，跳过", name)
                continue
            segments.append(seg.strip())
            prev_summary = _summarize_segment(seg)
        if not segments:
            return ""
        return "\n\n---\n\n".join(segments)

    def review(self, text: str, criteria: str, novel_text: str = "") -> ReviewResult:
        from deskmate.memory.llm import chat, llm_available

        if not llm_available():
            return ReviewResult(approved=True, opinion="（LLM 未配置，默认通过）")
        novel_block = (
            f"\n\n【原著参考（对照依据）】\n{novel_text}\n"
            "（请结合原著判断改编是否忠于核心人物与情节。）" if novel_text else ""
        )
        raw = chat(
            system="你是剧本审核专家。依据验收标准评审文本，先给结论"
            "（通过/打回），再给可执行的修改意见。若提供原著参考，须对照原著评判改编适配度。",
            user=f"验收标准：{criteria}\n\n文本：\n{text}{novel_block}",
            max_tokens=1200,
        )
        approved = "打回" not in raw and "不合格" not in raw
        return ReviewResult(approved=approved, opinion=raw)
