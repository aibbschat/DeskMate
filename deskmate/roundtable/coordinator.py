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

from deskmate.memory.llm import chat_stream, llm_available
from deskmate.roundtable import trends
from deskmate.roundtable.personas import PERSONAS, Persona

logger = logging.getLogger(__name__)

# run_stream 内部驱动任务结束的哨兵（放到 out_q 末尾，消费者据此退出）
_STOP = object()

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
class StoryContext:
    """圆桌每轮注入的全局理解上下文 + 可观测的检索轨迹。

    对应 OpenViking 的 L0/L1/L2 分层加载思路，但为纯加法、不改动取数逻辑：
    - L0：一句话总览（圣经统计量 + 主线首句），零 LLM，永远注入；
    - L1：四份圣经结构化概览（即原 _bible_summary 行为，预算默认不变以保幻觉护栏）；
    - L2：按需加载（仅议题/分歧点明确提及的实体深度上下文），不提及则不取。
    trace 仅用于可观测（前端检索轨迹面板 / 调试），不影响 prompt 内容。
    """
    text: str = ""
    trace: dict = field(default_factory=dict)


@dataclass
class PeerSpec:
    name: str
    url: str
    # 是否具备读取本地文件的能力（如 Hermes 可自行通读稿件）。OpenCode 适配层只能消费
    # 传入的 prompt 文本、无法自读文件，故传 True 时我们会截断 prompt 以省远端开销，
    # 传 False 时必须把完整正文交给它。
    can_read_files: bool = True


@dataclass
class RoundtableResult:
    topic: str
    turns: list[Turn]
    synthesis: str
    divergences: list[str]
    peers_online: list[str]
    peers_offline: list[str]
    rounds_held: int
    traces: list[dict] = field(default_factory=list)

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
        if self.traces:
            out.append("")
            out.append("## 检索轨迹（可观测）")
            for tr in self.traces:
                layers = "+".join(l.get("layer", "") for l in tr.get("layers", [])) or "-"
                l2 = "、".join(tr.get("l2_entities", []))
                line = f"- R{tr.get('round')}：层级 {layers}"
                if tr.get("fts5_query"):
                    q = tr["fts5_query"]
                    line += f"；FTS5「{q[:30]}{'…' if len(q) > 30 else ''}」"
                if tr.get("fts5_fallback"):
                    line += f"（兜底：{('、'.join(tr['fts5_fallback']))}）"
                if l2:
                    line += f"；按需 L2：{l2}"
                out.append(line)
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
        # L1 预算（即原 _bible_summary 截断长度，默认 4000 与旧行为一致，保幻觉护栏）。
        # 想更激进省 token 可调小，但会削弱「全书前情概览」覆盖，属权衡项、非默认。
        self.bible_l1_budget: int = 4000
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
        # 明确圣经的效力边界：它是「已核验事实的全集」，不是「可以据此发挥的素材」。
        # 圣经每条事实都附正文引证（未逐字命中者标【待确认】），凡未收录者即视为
        # 正文中不存在——这道声明是防止人格把暗示当情节、顺嘴编出不存在的人物线。
        guard = (
            "（以下设定圣经由稿件正文自动抽取并逐条做过原文引证核验。"
            "请严格以此为准：标注【待确认】的条目不得当作既定事实；"
            "圣经与当前章原文中都没有出现的人物、关系、情节，一律视为不存在，"
            "严禁脑补或据传闻发挥。"
            "另：圣经「人物（本作角色）」段才是本作出场角色；"
            "「对话／典故中提及的历史人物」段仅为角色对白里的举例或引用，"
            "并非本作人物，请勿将其作为本作角色展开分析或编造其情节。）"
        )
        return f"{guard}\n\n{text}"

    def _current_chapter_text(self) -> str:
        """当前评审章节原文（P0-2）：完整不截断，由 web.py 按章节范围选好传入 manuscript_text。"""
        return (self.manuscript_text or "").strip()

    def _bible_character_names(self, n: int = 3) -> list[str]:
        """从圣经 CHARACTERS 段提取主角名，用于检索兜底：人物名必现于正文。

        只从「人物（本作角色）」小节提取，**排除「对话／典故中提及的历史人物」小节**
        ——后者（如嬴政、王安石）只是角色对白里的举例引用，并非本作角色，不能被当成
        本作主角去做全书 FTS5 检索，否则会把对白里的典故片段误当成角色剧情上下文。

        兼容两种书稿格式：
        - 列表项「- 韩笑文：…」（最常见）
        - 行内 ``**韩笑文**`` 加粗
        """
        bible = self._load_bible()
        chars = bible.get("CHARACTERS", "") if bible else ""
        if not chars:
            return []
        import re as _re
        # 优先只取「本作角色」小节；旧格式/无分组标记时退回整段（向后兼容）
        m = _re.search(
            r"^###\s*人物（本作角色）\s*$(.*?)(?=^### |\Z)",
            chars, _re.MULTILINE | _re.DOTALL,
        )
        block = m.group(1) if m else chars
        seen: set[str] = set()
        out: list[str] = []
        for nm in _re.findall(r"\*\*([^*]+?)\*\*", block):
            nm = nm.strip()
            if nm and nm not in seen and len(nm) <= 8:
                seen.add(nm)
                out.append(nm)
        for nm in _re.findall(r"^\s*-\s*([^\s：:]{1,8})\s*[：:]", block, _re.MULTILINE):
            nm = nm.strip()
            if nm and nm not in seen and len(nm) <= 8:
                seen.add(nm)
                out.append(nm)
        return out[:n]

    def _bible_l0_header(self) -> str:
        """L0 层：一句话总览（结构化圣经统计量 + 主线首句）。零 LLM、永远注入。

        作用：给人格一个「这本书大概讲什么、有哪些主要人物关系」的入场锚点，
        比直接铺开四份圣经更易把握前情；同时不花 token 做生成。
        """
        bible = self._load_bible()
        if not bible:
            return ""
        # 用实体库统计（已核验/待确认），不重新解析 markdown
        try:
            from deskmate.novel_memory.core import bible_stats
            st = bible_stats(self.manuscript_path)
        except Exception:
            st = {}
        nc = st.get("character.confirmed", 0) + st.get("character.pending", 0)
        nr = st.get("relation.confirmed", 0) + st.get("relation.pending", 0)
        outline = (bible.get("OUTLINE") or "").strip()
        first = ""
        for ln in outline.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and not ln.startswith("<!--"):
                first = ln.lstrip("- ").strip()
                if first:
                    break
        head = f"📚 设定圣经（结构化·逐条引证核验）：本作角色 {nc} 人、关系 {nr} 条"
        if first:
            head += f"；主线：{first}"
        return head + "。"

    def _character_bible_entry(self, name: str) -> str:
        """从圣经 CHARACTERS 段提取某角色的整条概要行（含引证子项），用于 L2 按需加载。"""
        bible = self._load_bible()
        if not bible:
            return ""
        chars = bible.get("CHARACTERS", "")
        m = re.search(
            r"^###\s*人物（本作角色）\s*$(.*?)(?=^### |\Z)",
            chars, re.MULTILINE | re.DOTALL,
        )
        block = m.group(1) if m else chars
        for ln in block.splitlines():
            s = ln.strip()
            if s.startswith(f"- **{name}**") or s.startswith(f"- {name}：") \
                    or (f"**{name}**" in s and s.startswith("-")):
                return s
        return ""

    async def _bible_l2_for(self, query: str) -> tuple[str, list[str]]:
        """L2 层：按需加载——仅当议题/分歧点明确提及某个已知实体时，取其深度上下文。

        返回 (l2_text, loaded_names)。不提及任何已知实体则返回 ("", [])，此时不取任何
        深度内容（对应 OpenViking「不提及就不加载 L2」的省 token 思路）。

        loaded_names 供检索轨迹观测。深度上下文 = 该角色在圣经里的整条概要 + 全书相关片段。
        """
        bible = self._load_bible()
        if not bible or not query:
            return "", []
        names = self._bible_character_names(20)
        hits = [n for n in names if n and n in query]
        if not hits:
            return "", []
        parts: list[str] = []
        loaded: list[str] = []
        for n in hits[:3]:  # 至多 3 个实体，避免上下文膨胀
            entry = self._character_bible_entry(n)
            try:
                frag, _ = await self._fts5_recall(n, max_results=3, max_chars=1200)
            except Exception:
                frag = ""
            block_lines = []
            if entry:
                block_lines.append(entry)
            if frag:
                block_lines.append(f"【{n} 全书相关片段】\n{frag}")
            if block_lines:
                parts.append("\n".join(block_lines))
                loaded.append(n)
        if not parts:
            return "", []
        text = (
            "【L2 深度上下文·按需加载】（仅议题/分歧点明确提及的实体，避免无差别铺开全书）\n"
            + "\n\n".join(parts)
        )
        return text, loaded

    async def _fts5_recall(self, query: str, max_results: int = 6, max_chars: int = 1800) -> tuple[str, list[str]]:
        """全书按需检索（P0-3）：按议题/分歧点召回全书相关片段，补充前情与伏笔呼应。

        实现用 novel_memory 的正则关键词检索（_keyword_recall）：对中文更可靠
        —— FTS5 的 unicode61 分词会把中文拆成单字，导致「韩笑文」这类二元词被 AND 匹配而落空；
        正则子串 OR 反而精准。圆桌已单独注入四份圣经，故检索结果里的「人物圣经摘要」一律剔除，只留正文片段。

        议题式查询（如「人物关系铺垫」）在原文里往往不是连续子串，会落空；
        此时自动改用人名（圣经主角，必现于正文）检索，保证真实返回正文片段。

        Returns:
            (recall_text, fallback_names)：recall_text 为正文片段（可能为空串）；
            fallback_names 为「议题落空、改用主角名检索兜底」用到的名字列表（供检索轨迹观测）。
        """
        mp = self.manuscript_path
        if not mp or not Path(mp).expanduser().exists():
            return "", []

        def _search(q: str) -> str:
            from deskmate.novel_memory.core import keyword_recall
            return keyword_recall(q, mp, max_results)

        def _clean(raw: str) -> str:
            # 只保留真正的正文片段（【正文命中片段】之后），剔除附带的「人物圣经摘要」（圆桌已单独注入）
            i = raw.find("【正文命中片段】")
            return raw[i:].strip() if i != -1 else ""

        try:
            recall = _clean(await asyncio.to_thread(_search, query))
        except Exception as exc:
            logger.warning("FTS5 检索失败: %s", exc)
            return "", []
        # 议题落空 -> 优先用「议题中出现过的圣经人物名」检索（文学讨论往往围绕某角色，
        # 且该角色名必现于正文）；否则退而求其次用前若干主角名逐个试，取首个有正文片段者。
        fallback: list[str] = []
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
                    fallback.append(nm)
                    break
        if not recall:
            return "", []
        if len(recall) > max_chars:
            recall = recall[:max_chars] + "\n…（检索片段较长，已截断）"
        return recall, fallback

    async def _build_story_context(self, round_no: int, divergence_query: str = "") -> "StoryContext":
        """组装全局理解上下文（L0/L1/L2 分层），并返回可观测的检索轨迹。

        - L0：一句话总览（_bible_l0_header，零 LLM，永远注入）；
        - L1：四份圣经结构化概览（即原 _bible_summary 行为，预算默认不变以保幻觉护栏）；
        - 当前章原文（不截断）；
        - 全书检索（按需）：第1轮按议题，后续轮按分歧点；
        - L2：按需加载（仅议题/分歧点明确提及的实体深度上下文，_bible_l2_for）。
        """
        parts: list[str] = []
        trace_layers: list[dict] = []

        # L0：一句话总览（结构化圣经统计量 + 主线首句），纯加法、零 LLM
        l0 = self._bible_l0_header()
        if l0:
            parts.append(l0)
            trace_layers.append({
                "layer": "L0", "kind": "bible_header", "chars": len(l0),
                "note": "一句话总览：本作角色数/关系数/主线",
            })

        # L1：四份圣经结构化概览（现有行为，默认截断预算不变，保幻觉护栏）
        bible = self._bible_summary(self.bible_l1_budget)
        if bible:
            parts.append(
                "【设定圣经·宏观摘要】（全书人物/情节/时间线/伏笔总览，帮助你把握前情，"
                "不局限于当前章）\n" + bible
            )
            trace_layers.append({
                "layer": "L1", "kind": "bible_summary", "chars": len(bible),
                "note": f"四份圣经概览（已截断至 {self.bible_l1_budget} 字，含防幻觉护栏声明）",
            })

        # 当前章原文（不截断）
        chapter = self._current_chapter_text()
        if chapter:
            parts.append(
                "【当前评审章节原文】（完整，未截断，请严格基于真实文本发言，勿编造文中没有的内容）\n"
                + chapter
            )

        # 全书检索（按需）：第1轮按议题，后续轮按分歧点
        fts5_query = ""
        fts5_chars = 0
        fts5_fallback: list[str] = []
        if self.manuscript_path:
            fts5_query = divergence_query or self.topic
            recall, fts5_fallback = await self._fts5_recall(fts5_query)
            if recall:
                parts.append(
                    "【全书相关片段（按议题检索所得，补充前情与伏笔呼应）】\n" + recall
                )
                fts5_chars = len(recall)

        # L2：按需加载（仅议题/分歧点明确提及的实体，深度上下文）
        l2_text, l2_names = await self._bible_l2_for(divergence_query or self.topic)
        if l2_text:
            parts.append(l2_text)
            trace_layers.append({
                "layer": "L2", "kind": "entity_deep", "chars": len(l2_text),
                "note": "按需加载：议题/分歧点提及的实体深度上下文（圣经条目+全书片段）",
            })

        text = "\n\n".join(parts)
        trace = {
            "round": round_no,
            "layers": trace_layers,
            "fts5_query": fts5_query,
            "fts5_chars": fts5_chars,
            "fts5_fallback": fts5_fallback,
            "l2_entities": l2_names,
        }
        return StoryContext(text=text, trace=trace)

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

    # ---- 各类发言（原生流式：逐 token 产出，供 SSE 实时渲染）----

    async def _stream_persona_speak(
        self, persona: Persona, ctx: str, round_no: int, divergences: list[str], story_block: str = ""
    ):
        """进程内人格流式发言：async generator，逐块 yield 文本片段。"""
        if not llm_available():
            yield "（LLM 未就绪）"
            return
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
        async for delta in chat_stream(system, prompt, temperature=0.7, max_tokens=6000):
            yield delta

    async def _stream_host(self, instruction: str, ctx: str, story_block: str = ""):
        """主持流式发言：async generator，逐块 yield 文本片段。"""
        if not llm_available():
            yield "（LLM 未就绪）"
            return
        ms = story_block or self._manuscript_block()
        async for delta in chat_stream(
            HOST_SYSTEM,
            f"{instruction}\n\n圆桌议题：{self.topic}\n\n{ms}圆桌上下文：\n{ctx}",
            temperature=0.4,
            max_tokens=6000,
        ):
            yield delta

    async def _stream_turn(
        self, out_q, *, turn_id: str, round_no: int, speaker: str, role: str, kind: str,
        gen_factory, phase: str | None = None, fallback_text: str | None = None, retries: int = 2,
    ) -> Turn:
        """驱动单个流式发言：发 turn_start → 逐块 turn_delta → 发 turn_end（含完整文本）。

        gen_factory 是 0 参数可调用，每次调用返回一个新的 async generator（便于空结果重试）。
        返回完整 Turn，供调用方记入 transcript。
        """
        await out_q.put({
            "type": "turn_start", "turn_id": turn_id, "round": round_no,
            "speaker": speaker, "role": role, "kind": kind, "phase": phase,
        })
        parts: list[str] = []
        for attempt in range(retries + 1):
            parts = []
            try:
                async for delta in gen_factory():
                    parts.append(delta)
                    await out_q.put({"type": "turn_delta", "turn_id": turn_id, "text": delta})
                full = "".join(parts).strip()
                if full:
                    break
                logger.warning("流式发言返回空（%s，第%d次重试）", speaker, attempt + 1)
            except Exception as exc:  # noqa: BLE001
                logger.exception("流式发言失败(%s)", speaker)
                parts = [f"（调用失败：{exc}）"]
                break
            if attempt < retries:
                await asyncio.sleep(1.5)
        full = "".join(parts).strip()
        if not full or full.startswith("（"):
            if fallback_text:
                full = fallback_text
            elif not full:
                full = "（LLM 未返回内容）"
        await out_q.put({
            "type": "turn_end", "turn_id": turn_id, "round": round_no,
            "speaker": speaker, "role": role, "kind": kind, "phase": phase, "text": full,
        })
        return Turn(round_no, speaker, role, full, kind)

    async def _peer_turn(self, out_q, *, round_no: int, peer: PeerSpec, ctx: str, divergences: list[str], story_block: str = ""):
        """外部 A2A peer：拿不到 token 流，保持整段返回（沿用原 turn 事件）。"""
        text = await self._peer_speak(peer, ctx, round_no, divergences, story_block)
        await out_q.put({
            "type": "turn", "round": round_no, "speaker": peer.name, "role": "peer", "kind": "peer", "text": text,
        })
        return Turn(round_no, peer.name, "peer", text, "peer")

    async def _peer_speak(
        self, peer: PeerSpec, ctx: str, round_no: int, divergences: list[str], story_block: str = ""
    ) -> str:
        from coworker.tools.a2a import a2a_call

        # 外部 peer 输入截短：仅对「能自读文件」的 peer（如 Hermes）截断到 8000 字以省远端开销；
        # 对无文件读取能力的 peer（如 OpenCode 适配层）必须传完整正文，否则会被截丢本章内容。
        effective_block = story_block if not peer.can_read_files else story_block[:8000]
        prompt = self._participant_prompt(
            ctx, round_no, divergences, peer_name=peer.name, story_block=effective_block
        )
        # 外部强 Agent（Hermes 等具备读文件能力）：额外给稿件完整路径，让其自行通读全书。
        # 能力强的外部 agent 应自读全文（自带大上下文与读文件能力），而非只吃我们提炼/截断过的摘要；
        # 同时保留上方当前章原文与宏观摘要，供其快速对齐但不必局限于此。
        # 注意：对无文件读取能力的 peer（如 OpenCode）不附加此提示，避免误导其去读不存在的本地文件。
        if self.manuscript_path and peer.can_read_files:
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

        事件类型（新增 turn_start / turn_delta / turn_end 以支持原生逐字流式；
        进程内人格与主持走流式，外部 A2A peer 仍整段返回 turn）：
          - meta / peers / round_start / info / done：与旧版一致
          - speaking:    {round, speaker, role, kind}                        # 开始"思考"（3 点气泡）
          - turn_start:  {turn_id, round, speaker, role, kind, phase?}       # 创建可生长的发言气泡
          - turn_delta:  {turn_id, text}                                    # 追加一段文本（逐字）
          - turn_end:    {turn_id, round, speaker, role, kind, phase?, text}# 收尾（含完整文本）
          - turn:        {round, speaker, role, kind, text, phase?}          # 仅外部 peer 用（整段）
        """
        out_q: asyncio.Queue = asyncio.Queue()
        driver = asyncio.ensure_future(self._drive(out_q))
        try:
            while True:
                ev = await out_q.get()
                if ev is _STOP:
                    break
                yield ev
        finally:
            # 客户端断开 / 生成器被关闭时，取消仍在跑的驱动任务，避免泄漏
            if not driver.done():
                driver.cancel()
                try:
                    await driver
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def _drive(self, out_q: asyncio.Queue) -> None:
        """圆桌编排驱动：把全部事件推入 out_q，末尾推 _STOP 哨兵。

        进程内人格与主持走 _stream_turn（原生逐字流式）；外部 A2A peer 走 _peer_turn（整段）。
        内部用 asyncio.gather 并发多发言者，各发言者的 token 流按 turn_id 自然交错推给前端。
        """
        online, offline = await self._probe_peers()
        await out_q.put({"type": "meta", "topic": self.topic, "max_rounds": self.max_rounds})
        await out_q.put({"type": "peers", "online": [p.name for p in online], "offline": offline})

        transcript: list[Turn] = []
        traces: list[dict] = []  # 各轮 context_trace（L0/L1/L2 + FTS5 轨迹），用于纪要落盘与可观测

        # 开场（主持流式）
        await out_q.put({"type": "speaking", "round": 0, "speaker": "桌搭（主持）", "role": "host", "kind": "host"})
        sc0 = await self._build_story_context(0, "")
        await out_q.put({"type": "context_trace", "round": 0, "trace": sc0.trace})
        traces.append(sc0.trace)
        opening_ctx = sc0.text
        opening = await self._stream_turn(
            out_q, turn_id="host-open", round_no=0, speaker="桌搭（主持）", role="host", kind="host",
            gen_factory=lambda: self._stream_host(
                "请为话题开场：首先直接复述用户的原始议题（原样引用，不要重新定义或缩小范围），"
                "然后说明规则（有界多轮、到达序发言、最后由你做纪要），并抛出一个讨论引子。",
                self.context, opening_ctx,
            ),
        )
        transcript.append(opening)

        divergences: list[str] = []
        rounds_held = 0
        story_block = ""
        for r in range(1, self.max_rounds + 1):
            ctx = self._fmt(transcript)
            await out_q.put({"type": "round_start", "round": r})
            # 构造本轮全局理解上下文（宏观摘要 + 当前章全文 + 全书检索片段）
            div_query = "\n".join(divergences) if r > 1 else ""
            sc = await self._build_story_context(r, div_query)
            await out_q.put({"type": "context_trace", "round": r, "trace": sc.trace})
            traces.append(sc.trace)
            story_block = sc.text

            # 发言前先广播"思考中"
            for p in self.personas:
                await out_q.put({"type": "speaking", "round": r, "speaker": p.name, "role": p.role, "kind": "persona"})
            for p in online:
                await out_q.put({"type": "speaking", "round": r, "speaker": p.name, "role": "peer", "kind": "peer"})

            # 并发提交：进程内人格走流式，外部 peer 走整段；统一 gather 收集
            tasks = []
            for i, p in enumerate(self.personas):
                tasks.append(asyncio.ensure_future(self._stream_turn(
                    out_q, turn_id=f"p{i}-r{r}", round_no=r, speaker=p.name, role=p.role, kind="persona",
                    gen_factory=lambda p=p: self._stream_persona_speak(p, ctx, r, divergences, story_block),
                )))
            for p in online:
                tasks.append(asyncio.ensure_future(self._peer_turn(
                    out_q, round_no=r, peer=p, ctx=ctx, divergences=divergences, story_block=story_block,
                )))
            for t in await asyncio.gather(*tasks):
                transcript.append(t)

            rounds_held = r

            # 收窄轮：归纳分歧；若已一致则提前收尾
            if r < self.max_rounds:
                await out_q.put({"type": "speaking", "round": r, "speaker": "桌搭（主持）", "role": "host", "kind": "host"})
                div_text = await self._stream_turn(
                    out_q, turn_id=f"host-div-{r}", round_no=r, speaker="桌搭（主持）", role="host", kind="host",
                    gen_factory=lambda r=r, ctx=ctx: self._stream_host(
                        "根据上面第 %d 轮讨论，提炼仍未达成一致的关键分歧点"
                        "（最多 3 条，每条一句话，用『- 』开头）。若已高度一致，请直接写『无重大分歧』。" % r,
                        (self.context + "\n\n" + ctx).strip(), story_block,
                    ),
                    phase="divergence",
                )
                divergences = self._parse_divergences(div_text.text)
                if not divergences:
                    await out_q.put({"type": "info", "text": "各方已高度一致，圆桌提前收尾。"})
                    break

        # 纪要（主持流式）；失败兜底由 fallback_text 注入
        await out_q.put({"type": "speaking", "round": rounds_held + 1, "speaker": "桌搭（主持）", "role": "host", "kind": "host"})
        synthesis_ctx = (self.context + "\n\n" + self._fmt(transcript)).strip()
        synthesis_fallback = (
            "（自动兜底纪要）以下为各轮发言概览，建议稍后重试以获得完整纪要：\n"
            + "\n".join(
                f"- 第{t.round}轮 {t.speaker}：{t.text[:80]}…"
                for t in transcript
                if t.kind != "host"
            )
        )
        synthesis = await self._stream_turn(
            out_q, turn_id="host-synth", round_no=rounds_held + 1, speaker="桌搭（主持）", role="host", kind="host",
            gen_factory=lambda: self._stream_host(
                "圆桌结束。请基于全部讨论写一份《圆桌纪要》：先列共识，再列尚存分歧，"
                "最后给作者 3 条可执行的修改建议。",
                synthesis_ctx, story_block,
            ),
            phase="synthesis",
            fallback_text=synthesis_fallback,
        )

        # 构造结构化结果：完整纪要（to_markdown）随 done 事件一并下发，
        # 供 web 层落盘进主 app 会话库（进入左侧历史对话栏）。
        result = RoundtableResult(
            topic=self.topic,
            turns=transcript,
            synthesis=synthesis.text,
            divergences=divergences,
            peers_online=[p.name for p in online],
            peers_offline=offline,
            rounds_held=rounds_held,
            traces=traces,
        )
        await out_q.put({
            "type": "done",
            "rounds_held": rounds_held,
            "divergences": divergences,
            "peers_online": [p.name for p in online],
            "peers_offline": offline,
            "synthesis": synthesis.text,
            "topic": self.topic,
            "markdown": result.to_markdown(),
            "traces": traces,
        })
        await out_q.put(_STOP)

    async def run(self) -> RoundtableResult:
        """便捷同步式运行：消费 run_stream 汇总为结构化结果（保留原 CLI / 测试接口）。"""
        transcript: list[Turn] = []
        online: list[PeerSpec] = []
        offline: list[str] = []
        rounds_held = 0
        divergences: list[str] = []
        synthesis = ""
        traces: list[dict] = []
        async for ev in self.run_stream():
            t = ev["type"]
            if t == "turn":
                transcript.append(
                    Turn(ev["round"], ev["speaker"], ev["role"], ev["text"], ev.get("kind", "persona"))
                )
            elif t == "turn_end":
                transcript.append(
                    Turn(ev["round"], ev["speaker"], ev["role"], ev["text"], ev.get("kind", "persona"))
                )
                if ev.get("phase") == "synthesis":
                    synthesis = ev["text"]
            elif t == "peers":
                online = [PeerSpec(n, "") for n in ev["online"]]
                offline = ev["offline"]
            elif t == "context_trace":
                traces.append(ev["trace"])
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
            traces=traces,
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
        # OpenCode 适配层无法读取本地文件，必须传完整正文
        peers.append(PeerSpec("OpenCode", os.environ["OPENCODE_URL"], can_read_files=False))

    topic = os.environ.get("RT_TOPIC") or (
        "一段明代背景小说开篇：主角以锦衣卫身份出入诏狱，借『东厂』之名缉拿官绅，"
        "文中写『万历年间，东厂提督冯保秉笔直入内廷』。请各位从各自角度评审这一段。"
    )
    result = run_roundtable_sync(topic, peers=peers, max_rounds=int(os.environ.get("RT_ROUNDS", "2")))
    print(result.to_markdown())
