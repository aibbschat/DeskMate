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
from pathlib import Path
from typing import Optional

from deskmate.memory.llm import chat, llm_available
from deskmate.roundtable import trends
from deskmate.roundtable.personas import PERSONAS, Persona

logger = logging.getLogger(__name__)

HOST_SYSTEM = (
    "你是圆桌主持人「桌搭」，历史小说作家的 AI 搭档。\n"
    "你的职责：开场点题与定规则；每轮后归纳各方的共识与分歧；在收窄式多轮中只把"
    "『尚未达成一致的分歧点』抛回给各方答辩；最后写一份《圆桌纪要》（共识 / 尚存分歧 / 给作者的建议）。\n"
    "你保持中立、条理清晰、不替任何一方下结论。涉及史实须严谨，拿不准要存疑。\n"
    "⚠️ 议题忠实原则：严格忠实于用户给定的议题，开场时直接复述用户原文议题，"
    "不得自行缩小、替换、提炼或发挥新的议题范围。用户说讨论什么就讨论什么。"
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
        manuscript_text: str = "",
        manuscript_path: str = "",
        chapter_range: str = "",
    ) -> None:
        self.topic = topic
        self.peers = peers or []
        self.personas = personas or PERSONAS
        # 有界：上限 1..3
        self.max_rounds = max(1, min(3, max_rounds))
        # 续聊上下文：上一轮圆桌的讨论记录 + 用户追问，注入给主持与各方作为前情
        self.context = (context or "").strip()
        # 待评审的文稿正文（从文件加载或用户粘贴），注入 prompt 让人格基于真实文本发言
        self.manuscript_text = (manuscript_text or "").strip()
        # 稿件来源路径（文件/目录/书稿名）：用于读取圣经与全书 FTS5 检索，不传则只注入正文
        self.manuscript_path = (manuscript_path or "").strip()
        self.chapter_range = (chapter_range or "").strip()
        # 圣经懒加载缓存
        self._bible_cache: Optional[dict] = None
        self._bible_loaded: bool = False

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

    # ---- 圣经 / 全局理解 上下文（P0：圆桌接入圣经）----
    def _load_bible(self) -> Optional[dict]:
        """懒加载稿件圣经（仅当提供了真实存在的稿件路径时；书稿名等非路径情况优雅跳过）。"""
        if self._bible_loaded:
            return self._bible_cache
        self._bible_loaded = True
        mp = self.manuscript_path
        if not mp:
            return None
        if not Path(mp).expanduser().exists():
            return None
        try:
            from deskmate.novel_memory.core import read_bible
            self._bible_cache = read_bible(mp)
        except Exception as exc:
            logger.warning("读取圣经失败(%s): %s", mp, exc)
            self._bible_cache = None
        return self._bible_cache

    def _bible_summary(self, max_chars: int = 4000) -> str:
        """宏观摘要（P0-1）：把四份圣经压成一段总览，帮助人格把握前情，不局限于当前章。"""
        bible = self._load_bible()
        if not bible:
            return ""
        try:
            from deskmate.report import format_bible_for_prompt
            text = format_bible_for_prompt(bible).strip()
        except Exception:
            text = "\n\n".join(v.strip() for v in bible.values() if v and v.strip())
        if not text:
            return ""
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…（圣经较长，已截断至宏观摘要）"
        return text

    def _current_chapter_text(self) -> str:
        """当前评审章节原文（P0-2）：完整不截断，由 web.py 按章节范围选好传入 manuscript_text。"""
        return (self.manuscript_text or "").strip()

    def _bible_character_names(self, n: int = 3) -> list[str]:
        """从圣经 CHARACTERS 段提取主角名，用于检索兜底：人物名必现于正文。

        兼容两种书稿格式：
        - 列表项「- 韩笑文：…」（最常见）
        - 行内 ``**韩笑文**`` 加粗
        """
        bible = self._load_bible()
        chars = bible.get("CHARACTERS", "") if bible else ""
        if not chars:
            return []
        import re as _re
        seen: set[str] = set()
        out: list[str] = []
        for nm in _re.findall(r"\*\*([^*]+?)\*\*", chars):
            nm = nm.strip()
            if nm and nm not in seen and len(nm) <= 8:
                seen.add(nm)
                out.append(nm)
        for nm in _re.findall(r"^\s*-\s*([^\s：:]{1,8})\s*[：:]", chars, _re.MULTILINE):
            nm = nm.strip()
            if nm and nm not in seen and len(nm) <= 8:
                seen.add(nm)
                out.append(nm)
        return out[:n]

    async def _fts5_recall(self, query: str, max_results: int = 6, max_chars: int = 1800) -> str:
        """全书按需检索（P0-3）：按议题/分歧点召回全书相关片段，补充前情与伏笔呼应。

        实现用 novel_memory 的正则关键词检索（_keyword_recall）：对中文更可靠
        —— FTS5 的 unicode61 分词会把中文拆成单字，导致「韩笑文」这类二元词被 AND 匹配而落空；
        正则子串 OR 反而精准。圆桌已单独注入四份圣经，故检索结果里的「人物圣经摘要」一律剔除，只留正文片段。

        议题式查询（如「人物关系铺垫」）在原文里往往不是连续子串，会落空；
        此时自动改用人名（圣经主角，必现于正文）检索，保证真实返回正文片段。
        """
        mp = self.manuscript_path
        if not mp or not Path(mp).expanduser().exists():
            return ""

        def _search(q: str) -> str:
            from deskmate.novel_memory.core import _keyword_recall
            return _keyword_recall(q, mp, max_results)

        def _clean(raw: str) -> str:
            # 只保留真正的正文片段（【正文命中片段】之后），剔除附带的「人物圣经摘要」（圆桌已单独注入）
            i = raw.find("【正文命中片段】")
            return raw[i:].strip() if i != -1 else ""

        try:
            recall = _clean(await asyncio.to_thread(_search, query))
        except Exception as exc:
            logger.warning("FTS5 检索失败: %s", exc)
            return ""
        # 议题落空 -> 优先用「议题中出现过的圣经人物名」检索（文学讨论往往围绕某角色，
        # 且该角色名必现于正文）；否则退而求其次用前若干主角名逐个试，取首个有正文片段者。
        if not recall:
            all_names = self._bible_character_names(20)
            topic_names = [n for n in all_names if n and n in query]
            for nm in (topic_names or all_names[:8]):
                try:
                    r = _clean(await asyncio.to_thread(_search, nm))
                except Exception as exc:
                    logger.warning("FTS5 人物名检索失败(%s): %s", nm, exc)
                    r = ""
                if r:
                    recall = r
                    break
        if not recall:
            return ""
        if len(recall) > max_chars:
            recall = recall[:max_chars] + "\n…（检索片段较长，已截断）"
        return recall

    async def _build_story_context(self, round_no: int, divergence_query: str = "") -> str:
        """组装全局理解上下文：宏观摘要 + 当前章原文(不截断) + 全书检索片段(按需)。"""
        parts: list[str] = []
        bible = self._bible_summary()
        if bible:
            parts.append(
                "【设定圣经·宏观摘要】（全书人物/情节/时间线/伏笔总览，帮助你把握前情，"
                "不局限于当前章）\n" + bible
            )
        chapter = self._current_chapter_text()
        if chapter:
            parts.append(
                "【当前评审章节原文】（完整，未截断，请严格基于真实文本发言，勿编造文中没有的内容）\n"
                + chapter
            )
        # 全书检索（按需）：第1轮按议题，后续轮按分歧点
        if self.manuscript_path:
            query = divergence_query or self.topic
            recall = await self._fts5_recall(query)
            if recall:
                parts.append(
                    "【全书相关片段（按议题检索所得，补充前情与伏笔呼应）】\n" + recall
                )
        return "\n\n".join(parts)

    def _manuscript_block(self, max_chars: int = 24000) -> str:
        """兜底：无稿件路径时，直接把 manuscript_text 截断注入（保留旧行为）。"""
        if not self.manuscript_text:
            return ""
        text = self.manuscript_text
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…（文稿较长，已截断至前段）"
        return f"【待评审文稿】\n{text}\n\n"

    def _participant_prompt(
        self, ctx: str, round_no: int, divergences: list[str], peer_name: str | None = None,
        story_block: str = "",
    ) -> str:
        bg = self.context
        ms = story_block or self._manuscript_block()
        if round_no == 1:
            base = f"圆桌议题：{self.topic}\n\n"
            if ms:
                base += ms
                base += (
                    "⚠️ 请严格基于上述文稿内容发言，不得编造文中没有的人物、情节或场景。"
                    "如果文稿内容不足以支撑你的判断，请明言\"文稿未提供足够信息\"。\n\n"
                )
            if bg:
                base += (
                    "【背景：此前圆桌的讨论记录，供你了解前情，不必复述】\n"
                    f"{bg}\n\n"
                )
            base += (
                f"已有讨论记录：\n{ctx}\n\n"
                f"请以上述身份，针对议题发表你的看法（中文，聚焦你的职责）。"
            )
        else:
            d = "\n".join(f"- {d}" for d in divergences) or "（无）"
            base = f"圆桌议题：{self.topic}\n\n"
            if ms:
                base += ms
            base += (
                f"当前讨论记录：\n{ctx}\n\n"
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

    async def _host(self, instruction: str, ctx: str, story_block: str = "") -> str:
        ms = story_block or self._manuscript_block()
        return await self._call_llm(
            HOST_SYSTEM,
            f"{instruction}\n\n圆桌议题：{self.topic}\n\n{ms}圆桌上下文：\n{ctx}",
            temperature=0.4,
            max_tokens=6000,
            retries=2,
        )

    async def _persona_speak(
        self, persona: Persona, ctx: str, round_no: int, divergences: list[str], story_block: str = ""
    ) -> str:
        prompt = self._participant_prompt(ctx, round_no, divergences, story_block=story_block)
        # 市场化人格（publisher/adaptation/genre）注入「当前生效」的潮流简报，
        # 让它们拿到"今天的眼光"；其余人格不被潮流污染。无简报时安静跳过。
        system = persona.system_prompt
        if persona.id in trends.MARKET_PERSONA_IDS:
            try:
                inject = trends.trend_for_persona(persona.id)
                if inject:
                    system = persona.system_prompt + "\n\n" + inject
            except Exception as exc:  # noqa: BLE001
                logger.warning("潮流简报注入失败(跳过): %s", exc)
        return await self._call_llm(
            system, prompt, temperature=0.7, max_tokens=6000, retries=2
        )

    async def _peer_speak(
        self, peer: PeerSpec, ctx: str, round_no: int, divergences: list[str], story_block: str = ""
    ) -> str:
        from coworker.tools.a2a import a2a_call

        # 外部 peer 输入截短（8000 字 vs 进程内人格 20000），减少远端处理时间
        prompt = self._participant_prompt(
            ctx, round_no, divergences, peer_name=peer.name, story_block=story_block[:8000]
        )
        # 外部强 Agent（Hermes/OpenCode）：额外给稿件完整路径，让其自行通读全书。
        # 能力强的外部 agent 应自读全文（自带大上下文与读文件能力），而非只吃我们提炼/截断过的摘要；
        # 同时保留上方当前章原文与宏观摘要，供其快速对齐但不必局限于此。
        if self.manuscript_path:
            prompt = (
                prompt
                + "\n\n【稿件完整文件路径】（如你具备读取本地文件的能力，请自行通读该文件以掌握全书人物、"
                "情节与前情；以上附上的仅为当前评审章节原文与宏观摘要，供你快速对齐，但不必局限于此）\n"
                + self.manuscript_path
            )
        last = f"（{peer.name} 调用失败）"
        for attempt in range(3):  # 与 _call_llm 一致的重试（含短退避），避免网络抖动误判 peer 离线
            try:
                res = await asyncio.wait_for(
                    asyncio.to_thread(a2a_call, peer.url, prompt), timeout=240
                )
                if res and res.strip():
                    return _strip_a2a_header(res)
                last = f"（{peer.name} 返回空内容）"
            except asyncio.TimeoutError:
                logger.warning("peer %s 超时(第%d次, 240s)", peer.name, attempt + 1)
                last = f"（{peer.name} 在 240 秒内未响应，可能正在处理长文本）"
            except Exception as exc:  # noqa: BLE001
                logger.warning("peer %s 调用失败(第%d次): %s", peer.name, attempt + 1, exc)
                last = f"（{peer.name} 调用失败：{exc}）"
            if attempt < 2:
                await asyncio.sleep(1.5)
        return last

    async def _probe_peers(self) -> tuple[list[PeerSpec], list[str]]:
        online: list[PeerSpec] = []
        offline: list[str] = []
        for p in self.peers:
            try:
                from coworker.tools.a2a import a2a_discover

                res = await asyncio.wait_for(
                    asyncio.to_thread(a2a_discover, p.url), timeout=12
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
        opening_ctx = await self._build_story_context(0, "")
        opening = await self._host(
            "请为话题开场：首先直接复述用户的原始议题（原样引用，不要重新定义或缩小范围），"
            "然后说明规则（有界多轮、到达序发言、最后由你做纪要），并抛出一个讨论引子。",
            self.context,
            opening_ctx,
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
        story_block = ""
        for r in range(1, self.max_rounds + 1):
            ctx = self._fmt(transcript)
            yield {"type": "round_start", "round": r}
            # 构造本轮全局理解上下文（宏观摘要 + 当前章全文 + 全书检索片段）
            div_query = "\n".join(divergences) if r > 1 else ""
            story_block = await self._build_story_context(r, div_query)

            # 到达序：并发提交，按完成顺序收集；发言前先广播"思考中"
            items = [
                (p.name, p.role, "persona", self._persona_speak(p, ctx, r, divergences, story_block))
                for p in self.personas
            ]
            items += [
                (p.name, "peer", "peer", self._peer_speak(p, ctx, r, divergences, story_block))
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
                    story_block,
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
            story_block,
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
