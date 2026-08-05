"""创作圆桌协调器（有界多轮群聊）。

核心设计（与用户先前锁定的三决策一致）：
1. **发言顺序 = 到达序**：每轮把所有参与者（进程内人格 + 在线外部 A2A peer）的发言请求
   并发提交，用 `asyncio.as_completed` 按「谁先想好谁先冒泡」的顺序收集，而不是固定座次。
2. **桌搭主持串场**：主持人（桌搭）负责开场、每轮后归纳共识/分歧、最后写《圆桌纪要》。
3. **多轮 = 收窄式有界**：默认 2 轮、上限 1–3；第 1 轮自由发言，之后每轮只把
   「尚未达成一致的分歧点」抛回各方答辩，不无限循环；若无分歧则提前收尾。

进程内人格走 deskmate LLM（`deskmate.memory.llm.chat`）直调，不走 A2A；
外部 A2A peer（Hermes / opencode 适配层）通过 `coworker.tools.a2a` 调用，
探测到离线则跳过（圆桌不中断）。
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from deskmate.memory.llm import chat, llm_available
from deskmate.roundtable.personas import PERSONAS, Persona

logger = logging.getLogger(__name__)

HOST_SYSTEM = (
    "你是圆桌主持人「桌搭」，历史小说作家的 AI 搭档。\n"
    "你的职责：开场点题与定规则；每轮后归纳各方的共识与分歧；在收窄式多轮中只把"
    "『尚未达成一致的分歧点』抛回给各方答辩；最后写一份《圆桌纪要》（共识 / 尚存分歧 / 给作者的建议）。\n"
    "你保持中立、条理清晰、不替任何一方下结论。涉及史实须严谨，拿不准要存疑。"
)


@dataclass
class Turn:
    round: int
    speaker: str
    role: str
    text: str
    kind: str = "persona"  # persona | peer | host


@dataclass
class PeerSpec:
    name: str
    url: str


@dataclass
class RoundtableResult:
    topic: str
    turns: list[Turn]
    synthesis: str
    divergences: list[str]
    peers_online: list[str]
    peers_offline: list[str]
    rounds_held: int

    def to_markdown(self) -> str:
        out: list[str] = [f"# 创作圆桌纪要：{self.topic}", ""]
        out.append(f"- 实际轮次：{self.rounds_held} 轮（有界多轮）")
        out.append(f"- 外部 peer 在线：{', '.join(self.peers_online) or '无'}")
        if self.peers_offline:
            out.append(f"- 外部 peer 离线（已跳过）：{', '.join(self.peers_offline)}")
        if self.divergences:
            out.append(f"- 收尾时分歧点：{len(self.divergences)} 条")
        out.append("")
        for t in self.turns:
            out.append(f"## 第{t.round}轮 · {t.speaker}（{t.role}）")
            out.append("")
            out.append(t.text)
            out.append("")
        return "\n".join(out)


def _strip_a2a_header(text: str) -> str:
    if text.startswith("[A2A"):
        idx = text.find("\n")
        return text[idx + 1 :].strip() if idx != -1 else text
    return text.strip()


class Roundtable:
    def __init__(
        self,
        topic: str,
        peers: Optional[list[PeerSpec]] = None,
        personas: Optional[list[Persona]] = None,
        max_rounds: int = 2,
        context: str = "",
    ) -> None:
        self.topic = topic
        self.peers = peers or []
        self.personas = personas or PERSONAS
        # 有界：上限 1..3
        self.max_rounds = max(1, min(3, max_rounds))
        # 续聊上下文：上一轮圆桌的讨论记录 + 用户追问，注入给主持与各方作为前情
        self.context = (context or "").strip()

    # ---- 文本/解析辅助 ----
    def _fmt(self, turns: list[Turn]) -> str:
        return "\n".join(
            f"【第{t.round}轮 · {t.speaker}（{t.role}）】\n{t.text}" for t in turns
        )

    @staticmethod
    def _parse_divergences(text: str) -> list[str]:
        if re.search(r"无(重大)?分歧|高度一致|已一致|没有分歧|无分歧", text):
            return []
        out: list[str] = []
        for ln in text.splitlines():
            s = ln.strip()
            m = re.match(r"^([-*•]|\d+[.、])\s*(.+)", s)
            if m and len(m.group(2)) > 2:
                out.append(m.group(2).strip())
        return out[:3]

    def _participant_prompt(
        self, ctx: str, round_no: int, divergences: list[str], peer_name: str | None = None
    ) -> str:
        bg = self.context
        if round_no == 1:
            base = f"圆桌话题：{self.topic}\n\n"
            if bg:
                base += (
                    "【背景：此前圆桌的讨论记录，供你了解前情，不必复述】\n"
                    f"{bg}\n\n"
                )
            base += (
                f"已有讨论记录：\n{ctx}\n\n"
                f"请以上述身份，针对话题发表你的看法（中文，聚焦你的职责）。"
            )
        else:
            d = "\n".join(f"- {d}" for d in divergences) or "（无）"
            base = (
                f"圆桌话题：{self.topic}\n\n当前讨论记录：\n{ctx}\n\n"
                f"本轮为收窄式答辩，请【仅针对以下尚未达成一致的分歧点】发表你的补充或答辩，"
                f"不要重复已说过的内容：\n{d}"
            )
        if peer_name:
            base = f"你正作为外部 agent「{peer_name}」参与这场圆桌。\n" + base
        return base

    # ---- 各类发言 ----
    async def _call_llm(
        self, system: str, user: str, temperature: float = 0.5, max_tokens: int = 1200, retries: int = 3
    ) -> str:
        """带重试的 LLM 调用：空内容/异常时重试（含短退避），避免偶发空回复拖垮圆桌。"""
        if not llm_available():
            return "（LLM 未就绪）"
        last = "（LLM 未返回内容）"
        for attempt in range(retries + 1):
            try:
                out = await asyncio.to_thread(
                    chat, system, user, temperature=temperature, max_tokens=max_tokens
                )
                if out and out.strip():
                    return out.strip()
                last = "（LLM 返回空内容）"
            except Exception as exc:  # noqa: BLE001
                logger.exception("llm call failed")
                last = f"（调用失败：{exc}）"
            if attempt < retries:
                await asyncio.sleep(1.5)
        return last

    async def _host(self, instruction: str, ctx: str) -> str:
        return await self._call_llm(
            HOST_SYSTEM,
            f"{instruction}\n\n圆桌上下文：\n{ctx}",
            temperature=0.4,
            max_tokens=1500,
            retries=2,
        )

    async def _persona_speak(
        self, persona: Persona, ctx: str, round_no: int, divergences: list[str]
    ) -> str:
        prompt = self._participant_prompt(ctx, round_no, divergences)
        return await self._call_llm(
            persona.system_prompt, prompt, temperature=0.7, max_tokens=1200, retries=2
        )

    async def _peer_speak(
        self, peer: PeerSpec, ctx: str, round_no: int, divergences: list[str]
    ) -> str:
        try:
            from coworker.tools.a2a import a2a_call

            prompt = self._participant_prompt(ctx, round_no, divergences, peer_name=peer.name)
            res = await asyncio.wait_for(
                asyncio.to_thread(a2a_call, peer.url, prompt), timeout=120
            )
            return _strip_a2a_header(res)
        except Exception as exc:  # noqa: BLE001
            return f"（{peer.name} 调用失败：{exc}）"

    async def _probe_peers(self) -> tuple[list[PeerSpec], list[str]]:
        online: list[PeerSpec] = []
        offline: list[str] = []
        for p in self.peers:
            try:
                from coworker.tools.a2a import a2a_discover

                res = await asyncio.wait_for(
                    asyncio.to_thread(a2a_discover, p.url), timeout=6
                )
                if res.startswith("Error") or res.startswith("No Agent Card"):
                    offline.append(p.name)
                else:
                    online.append(p)
            except Exception:
                offline.append(p.name)
        return online, offline

    # ---- 主流程（流式：逐个事件吐出，供 SSE / UI 实时渲染）----
    async def run_stream(self):
        """异步生成器：边跑边 yield 事件 dict，供 SSE / UI 实时驱动。

        事件类型：
          - meta:     {topic, max_rounds}
          - peers:    {online:[name], offline:[name]}
          - speaking: {round, speaker, role, kind}            # 开始"思考"
          - turn:     {round, speaker, role, kind, text, phase?}  # 发言完成
          - round_start: {round}
          - info:     {text}
          - done:     {rounds_held, divergences, peers_online, peers_offline, synthesis}
        """
        online, offline = await self._probe_peers()
        yield {"type": "meta", "topic": self.topic, "max_rounds": self.max_rounds}
        yield {"type": "peers", "online": [p.name for p in online], "offline": offline}

        transcript: list[Turn] = []

        # 开场
        yield {"type": "speaking", "round": 0, "speaker": "桌搭（主持）", "role": "host", "kind": "host"}
        opening = await self._host(
            "请为话题开场：点明议题、说明规则（有界多轮、到达序发言、最后由你做纪要），"
            "并抛出一个讨论引子。",
            self.context,
        )
        yield {
            "type": "turn",
            "round": 0,
            "speaker": "桌搭（主持）",
            "role": "host",
            "kind": "host",
            "text": opening,
        }
        transcript.append(Turn(0, "桌搭（主持）", "host", opening, "host"))

        divergences: list[str] = []
        rounds_held = 0
        for r in range(1, self.max_rounds + 1):
            ctx = self._fmt(transcript)
            yield {"type": "round_start", "round": r}

            # 到达序：并发提交，按完成顺序收集；发言前先广播"思考中"
            items = [
                (p.name, p.role, "persona", self._persona_speak(p, ctx, r, divergences))
                for p in self.personas
            ]
            items += [
                (p.name, "peer", "peer", self._peer_speak(p, ctx, r, divergences))
                for p in online
            ]
            tasks = []
            for name, role, kind, coro in items:
                tasks.append((asyncio.create_task(coro), name, role, kind))
                yield {"type": "speaking", "round": r, "speaker": name, "role": role, "kind": kind}

            meta_by_task = {t: (name, role, kind) for (t, name, role, kind) in tasks}
            pending_set = {t for t, _, _, _ in tasks}
            while pending_set:
                done, pending_set = await asyncio.wait(
                    pending_set, return_when=asyncio.FIRST_COMPLETED
                )
                for fut in done:
                    name, role, kind = meta_by_task[fut]
                    text = fut.result()
                    turn = Turn(r, name, role, text, kind)
                    transcript.append(turn)
                    yield {
                        "type": "turn",
                        "round": r,
                        "speaker": name,
                        "role": role,
                        "kind": kind,
                        "text": text,
                    }

            rounds_held = r

            # 收窄轮：归纳分歧；若已一致则提前收尾
            if r < self.max_rounds:
                yield {"type": "speaking", "round": r, "speaker": "桌搭（主持）", "role": "host", "kind": "host"}
                div_text = await self._host(
                    "根据上面第 %d 轮讨论，提炼仍未达成一致的关键分歧点"
                    "（最多 3 条，每条一句话，用『- 』开头）。若已高度一致，请直接写『无重大分歧』。" % r,
                    (self.context + "\n\n" + self._fmt(transcript)).strip(),
                )
                divergences = self._parse_divergences(div_text)
                yield {
                    "type": "turn",
                    "round": r,
                    "speaker": "桌搭（主持）",
                    "role": "host",
                    "kind": "host",
                    "text": div_text,
                    "phase": "divergence",
                }
                if not divergences:
                    yield {"type": "info", "text": "各方已高度一致，圆桌提前收尾。"}
                    break

        # 纪要
        yield {"type": "speaking", "round": rounds_held + 1, "speaker": "桌搭（主持）", "role": "host", "kind": "host"}
        synthesis = await self._host(
            "圆桌结束。请基于全部讨论写一份《圆桌纪要》：先列共识，再列尚存分歧，"
            "最后给作者 3 条可执行的修改建议。",
            (self.context + "\n\n" + self._fmt(transcript)).strip(),
        )
        if not synthesis or synthesis.startswith("（"):
            # 兜底：纪要生成仍失败则给出基于发言的最小摘要，避免产出空纪要
            synthesis = (
                "（自动兜底纪要）以下为各轮发言概览，建议稍后重试以获得完整纪要：\n"
                + "\n".join(
                    f"- 第{t.round}轮 {t.speaker}：{t.text[:80]}…"
                    for t in transcript
                    if t.kind != "host"
                )
            )
        yield {
            "type": "turn",
            "round": rounds_held + 1,
            "speaker": "桌搭（主持）",
            "role": "host",
            "kind": "host",
            "text": synthesis,
            "phase": "synthesis",
        }
        yield {
            "type": "done",
            "rounds_held": rounds_held,
            "divergences": divergences,
            "peers_online": [p.name for p in online],
            "peers_offline": offline,
            "synthesis": synthesis,
        }

    async def run(self) -> RoundtableResult:
        """便捷同步式运行：消费 run_stream 汇总为结构化结果（保留原 CLI / 测试接口）。"""
        transcript: list[Turn] = []
        online: list[PeerSpec] = []
        offline: list[str] = []
        rounds_held = 0
        divergences: list[str] = []
        synthesis = ""
        async for ev in self.run_stream():
            t = ev["type"]
            if t == "turn":
                # 主持的"分歧归纳"轮不计入 transcript（与原始行为一致）
                if ev.get("phase") == "divergence":
                    continue
                transcript.append(
                    Turn(ev["round"], ev["speaker"], ev["role"], ev["text"], ev.get("kind", "persona"))
                )
            elif t == "peers":
                online = [PeerSpec(n, "") for n in ev["online"]]
                offline = ev["offline"]
            elif t == "done":
                rounds_held = ev["rounds_held"]
                divergences = ev["divergences"]
                online = [PeerSpec(n, "") for n in ev["peers_online"]]
                offline = ev["peers_offline"]
                synthesis = ev["synthesis"]
        return RoundtableResult(
            topic=self.topic,
            turns=transcript,
            synthesis=synthesis,
            divergences=divergences,
            peers_online=[p.name for p in online],
            peers_offline=offline,
            rounds_held=rounds_held,
        )


async def run_roundtable(
    topic: str,
    peers: Optional[list[PeerSpec]] = None,
    max_rounds: int = 2,
) -> RoundtableResult:
    """便捷入口：跑一场圆桌，返回结构化结果。"""
    return await Roundtable(topic, peers=peers, max_rounds=max_rounds).run()


def run_roundtable_sync(
    topic: str,
    peers: Optional[list[PeerSpec]] = None,
    max_rounds: int = 2,
) -> RoundtableResult:
    """同步便捷入口（供 CLI / 测试 / 非 async 上下文调用）。"""
    return asyncio.run(run_roundtable(topic, peers=peers, max_rounds=max_rounds))


if __name__ == "__main__":
    import os

    # 可通过环境变量注入外部 peer；不填则只有进程内 4 人格。
    peers: list[PeerSpec] = []
    if os.environ.get("HERMES_URL"):
        peers.append(PeerSpec("Hermes", os.environ["HERMES_URL"]))
    if os.environ.get("OPENCODE_URL"):
        peers.append(PeerSpec("OpenCode", os.environ["OPENCODE_URL"]))

    topic = os.environ.get("RT_TOPIC") or (
        "一段明代背景小说开篇：主角以锦衣卫身份出入诏狱，借『东厂』之名缉拿官绅，"
        "文中写『万历年间，东厂提督冯保秉笔直入内廷』。请各位从各自角度评审这一段。"
    )
    result = run_roundtable_sync(topic, peers=peers, max_rounds=int(os.environ.get("RT_ROUNDS", "2")))
    print(result.to_markdown())
