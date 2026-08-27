"""创作执行器：把「写大纲/写剧本/审」的具体生成从编排中解耦。

- ``StubExecutor``：不调 LLM，返回确定性占位文本，``review`` 默认通过（可注入一次
  拒绝用于验证打回链路）。开发/验证用。
- ``LLMExecutor``：复用 ``deskmate.memory.llm.chat`` 真实接入（阶段 1 留作清晰对接点，
  环境需配置 provider 凭证）。

两者都实现 ``CreativeExecutor`` 协议，pipeline 不关心背后是否真调 LLM。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReviewResult:
    approved: bool
    opinion: str


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
        from deskmate.memory.llm import chat, llm_available

        if not llm_available():
            return ""
        novel_block = (
            f"\n\n【原著参考（改编依据）】\n{novel_text}\n（请基于上述原著精神展开剧本，"
            "保持人物与核心情节一致。）" if novel_text else ""
        )
        return chat(
            system="你是编剧。依据给定大纲撰写剧本正文，注意台词节奏与可拍性。"
            "若提供原著参考，须基于原著精神改编。",
            user=outline + novel_block,
            max_tokens=4000,
        )

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
