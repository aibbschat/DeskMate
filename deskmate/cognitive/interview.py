"""深度访谈引擎（③b 闭环）——SocraticInterviewEngine。

基于认知画像薄弱点（open_gaps + questioned 条目 + 低置信 active 条目），
生成苏格拉底式访谈问题，回收用户回答后回灌画像（calibrate / 补来源 / 提取新认知），
消解认知盲区、提升置信度，让"懂我"迈向"像我"。

设计原则（延续③a）：
- 规则优先：问题生成与回答处理默认纯规则，离线可跑；
- fail-open：LLM 不可用/解析失败只记 warning，不崩聊天管线；
- 不 spawn 假 gap：deepen 模式直接给原条目补来源+升置信，绕开 add_stance 矛盾检测。
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Optional

from .interfaces import InterviewEngine
from .models import CognitiveProfile, SourceRef, HistoryEntry, Stance, _now
from .profile import get_profile_manager, resolve_profile_manager
from .store import CognitiveStore
from .extractor import get_extractor

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 呈现层：内部术语 → 对他说的话
# ----------------------------------------------------------------------
# 为什么需要（2026-09-22，萧哥反馈「访谈内容可能是你测试的问题，与我关系不大，
# 所以我点退出了」之后定位）：
#
# 素材**全是真的**（两条来自他日记、一条来自他 09-04 的访谈回答），但呈现层把
# 内部术语原样端了出去，读起来就像系统自检：
#   ① ``topic`` 是代码里的分类键——`emotion_extract` 的「情感触发·xxx」、
#      `conflict_gap` 的「画像冲突·xxx」、`emotion_profile` 的「情感·xxx」。
#      而气泡第一句就是 `想跟你聊聊「{questions[0].topic}」`（InterviewCard.tsx:48）
#      → 他第一眼看到的是「想跟你聊聊『画像冲突·回血方式』」。
#   ② ``gap.reason`` 是**第三人称**写的内部描述（源头见 conflict_gap._spawn_gaps
#      的「既有画像说他偏…是他想法变了」），却被原样贴进对着他说的问句里
#      → 对着他说话却用「他」，人称错位。
#   ③ ``content`` 是整段原文硬截 60 字（``content[:60]``）→ 留下「怎么…」这种半截话。
#
# 原则：**内部键照旧存**（去重/回灌/排查都靠它），**只洗呈现层**。
# `_display_topic` 洗标签，`_speakable` 洗正文；两者对存量数据同样生效，
# 所以不需要做数据迁移。
_TOPIC_PREFIX_RE = re.compile(r"^\s*(?:情感触发|情感|认知|写作|世界观|画像冲突)\s*[·・:：]\s*")
_UNCLOSED_TAIL_RE = re.compile(r"[（(][^（()）]{0,8}[）)]?\s*$")
_SENT_TAIL_RE = re.compile(r"[。！？!?…、，,；;：:\s]+$")
_SENT_BREAK = "。！？；，、!?;,：:"
# 旧版把**命中词**塞进括号当证据，于是出现「（约）」这种一个字的"证据"——
# 读者看不懂，也正是「像系统自检」的观感来源之一。≤2 字的括号一律清掉
# （「（如爽文）」这种 3 字的是有意义的补充，保留）。
_TINY_PAREN_RE = re.compile(r"[（(][^（()）]{1,2}[）)]")
# 提取器截断留下的省略号尾巴（「…怎么…」），连同它前面那个短分句一起剪掉，
# 免得句子读起来像坏数据。
_ELLIPSIS_TAIL_RE = re.compile(r"[，,、]?[^，,、。！？]{1,8}$")
# 旧的「画像冲突」reason 模板的存量兜底：那几个词是写给系统看的，不该出现在对他说
# 的句子里。源头（conflict_gap._spawn_gaps）已改成第二人称，这里只兜历史数据。
# ⚠ 不要图省事做**全局**「他→你」替换：正文里的「他」未必指萧盛（如日记里的
# 「尹逍青春期…对**他**又恨又无奈」指的是尹逍），一换就语义反转（2026-09-22 踩过）。
_PHRASE_FIXES = (
    ("既有画像说他偏", "我之前记的是你偏"),
    ("既有画像说他", "我之前记的是你偏"),
    ("既有画像说", "我之前记的是"),
    ("既有画像", "我之前记的"),
    # 旧模板这句的后半截是「（约）」这种单字命中，_TINY_PAREN_RE 清掉后会在
    # 「更像是」处留个断口，所以整体换成人话（别再拼「更像是」）。
    ("但近期的表达出现了相反的信号", "但最近的话让我觉得不太一样"),
    ("——是他想法变了，还是不同情境下不一样？", "——是你变了，还是分不同情境？"),
    ("——是他想法变了", "——是你变了"),
)


def _tidy(text: str) -> str:
    """文本卫生：压平空白 → 清掉旧模板的系统措辞、单字"证据"、截断尾巴。"""
    t = re.sub(r"\s+", "", str(text or ""))
    for bad, good in _PHRASE_FIXES:
        t = t.replace(bad, good)
    t = _TINY_PAREN_RE.sub("", t)
    if "…" in t:
        t = t[: t.rindex("…")]
        t = _ELLIPSIS_TAIL_RE.sub("", t)
    t = _UNCLOSED_TAIL_RE.sub("", t)
    # 截断可能砍掉后半截括号，补上闭合符（「烦（…又恨又无奈」→「…无奈）」），
    # 免得引用看起来像坏数据。
    for op, cl in (("（", "）"), ("(", ")")):
        gap = t.count(op) - t.count(cl)
        if gap > 0:
            t += cl * gap
    return t.strip("「」“”\"' 　")


def _display_topic(topic, fallback="") -> str:
    """把内部标签洗成人话短语（前端气泡/卡片直接显示它）。"""
    t = _TOPIC_PREFIX_RE.sub("", str(topic or "").strip()).strip(" ·・:：")
    if not t:
        # 标签全是前缀（或为空）时，退而用正文开头当话题名
        t = _SENT_TAIL_RE.sub("", _tidy(fallback))[:18]
    return t or "这件事"


def _speakable(text, limit: int = 64) -> str:
    """把内部描述洗成可以**直接说给萧盛听**的一句话（必要时截到完整句）。"""
    t = _tidy(text)
    if not t:
        return ""
    if len(t) > limit:
        cut = t[:limit]
        pos = max(cut.rfind(p) for p in _SENT_BREAK)
        t = cut[:pos] if pos >= limit // 2 else cut
    return _SENT_TAIL_RE.sub("", t)


# 苏格拉底式问题模板（按 mode）
# ⚠ 模板里**禁止**出现内部术语与第三人称：topic 只当自然指代，content 必须先过
# `_speakable`（见上）。气泡/卡片会把这两段直接显示给萧哥。
_Q_TEMPLATES = {
    "clarify": (
        "有件事我没对上：{content}。"
        "你实际是怎么想的？什么情况下会不一样？"
    ),
    "deepen": (
        "你之前说过：{content}。"
        "这背后更底层的判断是什么？为什么这对你这么重要？"
    ),
    "explore": (
        "关于「{topic}」，我想多懂你一点。"
        "能不能讲个具体的例子，说说你实际会怎么看、怎么做？"
    ),
}

_TRIGGER_GAP_THRESHOLD = 2          # open gap >= 此数才考虑主动发起
_TRIGGER_MIN_INTERVAL_DAYS = 3     # 距上次访谈至少 N 天才再主动
# 高优先级 gap（真矛盾 / 画像冲突，priority >= 此值）：**一个就值得问**，不必凑数。
# 探索型 gap（ensure_emotional_gaps 播的 0.7）仍需凑够 _TRIGGER_GAP_THRESHOLD 个——
# 「想多了解你」可以攒着一起问，「你说的跟以前不一样了」必须立刻澄清。
_HIGH_PRIORITY_GAP_PRIORITY = 0.8

# ---- 补送（重放「发起过但没被看见」的邀请）参数 -------------------------------
# 为什么需要（2026-09-22 二次定位，是「访谈永不出现」剩下一半的根因）：
# 冷却在 create_session 时就被扣掉，但「送达」没有任何凭证——前端邀请气泡只存在
# 60 秒，用户当时不在窗口前（他常在 Word 里写作）就无声消失；冷却却已经消耗，
# 之后每次 tick 只会被冷却挡住。补送把这张已发出的邀请重放出来，不受冷却约束。
_REDELIVER_MIN_INTERVAL_MINUTES = 30   # 同层两次补送的最小间隔（别每 15 秒顶一次气泡）
_REDELIVER_MAX_TIMES = 8               # 单个会话最多补送次数，超过即放弃
_REDELIVER_MAX_AGE_HOURS = 48          # 会话建立超过此时长仍无人回应 → 放弃并收敛


def _encode_question_id(related_item_id, related_gap_id, mode) -> str:
    """把问题元数据编码进 question_id，便于 process_response 无状态回查。"""
    segs = [f"q_{uuid.uuid4().hex[:10]}"]
    if related_item_id:
        segs.append(f"i={related_item_id}")
    if related_gap_id:
        segs.append(f"g={related_gap_id}")
    segs.append(f"m={mode}")
    return "__".join(segs)


def _decode_question_id(qid: str) -> dict:
    """从 question_id 还原元信息。"""
    d = {"related_item_id": None, "related_gap_id": None, "mode": "explore"}
    for seg in qid.split("__")[1:]:
        if seg.startswith("i="):
            d["related_item_id"] = seg[2:]
        elif seg.startswith("g="):
            d["related_gap_id"] = seg[2:]
        elif seg.startswith("m="):
            d["mode"] = seg[2:]
    return d


def _reconstruct_question_text(meta: dict, pm) -> str:
    """从 qid 元数据 + 画像反解问题文本（模板重建，无状态）。

    供 answer_interview 落库可追溯：访谈历史里能看到「问的什么 + 答的什么」，
    而不是只有答案。qid 编码了 mode + 关联条目，纯函数重建，无需额外存库。
    """
    mode = meta.get("mode", "explore")
    topic, content = "", ""
    if meta.get("related_item_id"):
        for it in (pm.profile.stances + pm.profile.value_rankings
                   + pm.profile.thinking_frameworks + pm.profile.argument_patterns):
            if getattr(it, "id", None) == meta["related_item_id"]:
                topic = getattr(it, "topic", None) or getattr(it, "domain", "") or ""
                content = it.content
                break
    elif meta.get("related_gap_id"):
        for g in pm.profile.cognitive_gaps:
            if g.id == meta["related_gap_id"]:
                topic = g.topic
                content = g.reason
                break
    tmpl = _Q_TEMPLATES.get(mode, _Q_TEMPLATES["explore"])
    return tmpl.format(topic=topic or "这个话题", content=(content or "")[:60])


class SocraticInterviewEngine(InterviewEngine):
    """苏格拉底式深度访谈引擎——③b 落地实现。"""

    def generate_questions(
        self, profile: CognitiveProfile, limit: int = 3, use_llm: bool = False
    ) -> list[dict]:
        """生成访谈问题，优先级：questioned 矛盾 → open gap → 低置信 active。

        use_llm 当前保留（规则优先）；LLM 润色为可选增强，待 provider 配置后接入。
        """
        questions: list[dict] = []

        # 1) questioned 条目（矛盾待澄清）——遍历五个维度（不只 stances）
        for it in (profile.stances + profile.value_rankings
                   + profile.thinking_frameworks + profile.argument_patterns):
            if getattr(it, "status", "") == "questioned":
                topic = getattr(it, "topic", None) or getattr(it, "domain", "") or ""
                questions.append(self._build(topic, it.content, "clarify", related_item_id=it.id))

        # 2) open gaps（有 related_items → 澄清；否则 → 探索新领域）
        # 先过 _gap_is_askable：关联条目已消失 / 内容为空的 gap 不问——
        # 真实库那两个 pytest 夹具 gap 就是这么被问出去的（见其 docstring）。
        gaps = sorted(profile.open_gaps(), key=lambda g: -g.priority)
        for g in gaps:
            if not _gap_is_askable(g, profile):
                logger.debug("跳过无据的 open gap（不提问）: %s / %s", g.id, g.topic)
                continue
            if g.related_items:
                questions.append(self._build(
                    g.topic, g.reason, "clarify",
                    related_item_id=g.related_items[0], related_gap_id=g.id))
            else:
                questions.append(self._build(g.topic, g.reason, "explore", related_gap_id=g.id))

        # 3) 低置信 active 条目（深化已知认知）
        for it in (profile.stances + profile.value_rankings
                   + profile.thinking_frameworks + profile.argument_patterns):
            if getattr(it, "status", "active") == "active" and getattr(it, "confidence", 1.0) < 0.6:
                topic = getattr(it, "topic", None) or getattr(it, "domain", "") or ""
                questions.append(self._build(topic, it.content, "deepen", related_item_id=it.id))

        # 去重：按**内部键**（topic_key）而不是呈现用的 topic——两个不同的标签
        # 洗成人话后可能同名（如「情感·回血方式」与「画像冲突·回血方式」都变
        # 「回血方式」），拿呈现值去重会把真问题悄悄吃掉。
        seen, uniq = set(), []
        for q in questions:
            key = q.get("topic_key") or q["topic"]
            if key not in seen:
                seen.add(key)
                uniq.append(q)
        return uniq[:limit]

    @staticmethod
    def _build(topic, content, mode, related_item_id=None, related_gap_id=None) -> dict:
        tmpl = _Q_TEMPLATES.get(mode, _Q_TEMPLATES["explore"])
        # 呈现层清洗：标签不露面、正文成人话（存量数据也照样洗，见文件头说明）
        disp = _display_topic(topic, content)
        quote = _speakable(content)
        q_text = tmpl.format(topic=disp, content=quote)
        return {
            "id": _encode_question_id(related_item_id, related_gap_id, mode),
            # ⚠ topic 是**给用户看的**（气泡首句、卡片标题都直接显示它），
            # 所以这里放洗过的人话版；内部原始标签走 topic_key（去重/排查用）。
            "topic": disp,
            "topic_key": topic or "",
            "question": q_text,
            "context": _speakable(content, limit=140),
            "related_item_id": related_item_id,
            "related_gap_id": related_gap_id,
            "priority": 0.8 if mode == "clarify" else 0.5,
            "mode": mode,
        }

    def process_response(
        self, profile: CognitiveProfile, question_id: str, response: str,
        layer: str = "cognitive",
    ) -> CognitiveProfile:
        """处理用户对某访谈问题的回答，回灌认知画像。

        返回更新后的 profile（manager 已 save）。fail-open：异常只记 warning。
        """
        meta = _decode_question_id(question_id)
        pm = get_profile_manager(layer=layer)
        response = (response or "").strip()
        if not response:
            logger.warning("访谈回答为空，跳过 q=%s", question_id)
            return profile
        try:
            if meta["related_item_id"] and meta["mode"] == "clarify":
                # 矛盾澄清：用回答纠正该条目（提置信 + 解关联 gap）
                pm.calibrate(meta["related_item_id"], response, feedback="访谈澄清")
            elif meta["related_item_id"] and meta["mode"] == "deepen":
                # 深化：直接给原条目补来源+升置信，绕开矛盾检测（不 spawn 假 gap）
                self._deepen_item(pm, meta["related_item_id"], response)
            if meta["related_gap_id"]:
                # 新领域：从回答提取认知 + 解该 gap
                self._extract_and_resolve_gap(pm, meta["related_gap_id"], response, layer=layer)
        except Exception as e:
            logger.warning("访谈回答处理失败（跳过）: %s", e)
        return pm.profile

    @staticmethod
    def _deepen_item(pm, item_id, response):
        """给已有条目补访谈来源并升置信（不经 add_stance，避免矛盾检测误 spawn gap）。"""
        item = None
        for it in (pm.profile.stances + pm.profile.value_rankings
                   + pm.profile.thinking_frameworks + pm.profile.argument_patterns):
            if getattr(it, "id", None) == item_id:
                item = it
                break
        if item is None:
            return
        src = SourceRef(type="interview", title="深度访谈", excerpt=response[:120], date=_now())
        item.add_source(src)  # 自动升置信（avg + 0.05*(n-1)，上限 0.95）
        item.history.append(HistoryEntry(
            action="deepened", old_value=item.content, new_value=item.content,
            reason="访谈深化，补充来源", source="深度访谈"))
        item.last_updated = _now()
        pm.save()

    @staticmethod
    def _absorb_response(pm, gap_id, response, layer="cognitive"):
        """把访谈回答作为认知信号并入画像。

        explore（探索新领域）问题的核心价值：用户针对未知盲区的直接表达。
        - cognitive 层：优先用 LLM 抽取四维度结构化认知（带 contexts 情境标签），
          LLM 不可用时降级为规则兜底（只落 stance）；
        - emotional 层：维持原逻辑（按 gap topic 推断情感维度 tag）。
        直接操作条目，绕开 add_stance 矛盾检测，避免误 spawn 假 gap。
        """
        text = (response or "").strip()
        if not text:
            return
        gap = next((g for g in pm.profile.cognitive_gaps if g.id == gap_id), None)
        raw_topic = (gap.topic if gap else "") or "深度访谈"
        # gap.topic 可能带"立场矛盾："/"认知盲区："等前缀，去掉前缀还原真实议题
        topic = raw_topic.split("：", 1)[-1].strip() or "深度访谈"

        if layer == "emotional":
            SocraticInterviewEngine._absorb_response_emotional(pm, topic, text)
            return

        # cognitive 层：LLM 抽取四维度（fail-open，仅 LLM 完全不可用才降级规则）
        try:
            from .extractor import extract_cognitive_items

            dep = extract_cognitive_items(
                text, source_type="interview", source_title="深度访谈",
                source_date=_now(), context_hint=topic, pm=pm, mode="merge",
            )
        except Exception as e:
            logger.warning("访谈 LLM 抽取失败（降级规则）: %s", e)
            dep = {"error": "exception"}
        if dep.get("error") == "llm_not_available":
            SocraticInterviewEngine._absorb_response_rule(pm, topic, text)
        # 其他情况（成功抽到 N 条 / 抽到 0 / 解析失败 / 调用异常）信任 LLM 路径，不重复落

    @staticmethod
    def _absorb_response_emotional(pm, topic, text):
        """情感层回灌：按 topic 推断维度 tag，落 stance（供情感子系统读取）。"""
        src = SourceRef(
            type="interview", title="深度访谈",
            excerpt=text[:120], date=_now(), confidence=0.6)
        emo_tags: list[str] = []
        if "关怀" in topic:
            emo_tags = ["情感层", "care_preference"]
        elif "回血" in topic or "恢复" in topic:
            emo_tags = ["情感层", "recovery_pattern"]
        else:
            emo_tags = ["情感层", "emotional_trigger"]
        for s in pm.profile.stances:
            if s.topic == topic and s.status == "active":
                for t in emo_tags:
                    if t not in (s.tags or []):
                        s.tags.append(t)
                s.add_source(src)
                s.history.append(HistoryEntry(
                    action="updated", old_value=s.content, new_value=s.content,
                    reason="访谈探索补充来源", source="深度访谈"))
                s.last_updated = _now()
                return
        stance = Stance(
            topic=topic, content=text[:200], category="访谈",
            tags=["访谈", "认知"] + emo_tags)
        stance.add_source(src)
        stance.history.append(HistoryEntry(
            action="created", new_value=text[:200],
            reason="访谈探索提取", source="深度访谈"))
        pm.profile.stances.append(stance)
        pm.profile.extraction_count += 1

    @staticmethod
    def _absorb_response_rule(pm, topic, text):
        """规则兜底（LLM 不可用）：把回答落为 stance 或补充来源。"""
        src = SourceRef(
            type="interview", title="深度访谈",
            excerpt=text[:120], date=_now(), confidence=0.6)
        for s in pm.profile.stances:
            if s.topic == topic and s.status == "active":
                s.add_source(src)
                s.history.append(HistoryEntry(
                    action="updated", old_value=s.content, new_value=s.content,
                    reason="访谈探索补充来源", source="深度访谈"))
                s.last_updated = _now()
                return
        stance = Stance(
            topic=topic, content=text[:200], category="访谈",
            tags=["访谈", "认知"])
        stance.add_source(src)
        stance.history.append(HistoryEntry(
            action="created", new_value=text[:200],
            reason="访谈探索提取", source="深度访谈"))
        pm.profile.stances.append(stance)
        pm.profile.extraction_count += 1

    @staticmethod
    def _extract_and_resolve_gap(pm, gap_id, response, layer="cognitive"):
        """从回答吸收新认知（规则优先）并消解该 gap。

        修复③b 遗留断点：原实现给 extract_from_text 传了不存在的 use_llm 参数，
        必然 TypeError 被 fail-open 吞掉 → explore 回答只消 gap 不进画像。
        现改为规则吸收（必达、离线可跑、响应快），不再同步调 LLM——实测当前
        chat() 需 ~9s，会阻塞回答接口；LLM 五维深度提取留给后续独立异步通道。
        """
        try:
            SocraticInterviewEngine._absorb_response(pm, gap_id, response, layer=layer)
        except Exception as e:
            logger.warning("访谈回答认知吸收失败（仍解 gap）: %s", e)
        for g in pm.profile.cognitive_gaps:
            if g.id == gap_id and g.status == "open":
                g.status = "resolved"
                g.resolved_at = _now()
        pm.save()

    def should_interview_now(
        self,
        profile: CognitiveProfile,
        layer: str = "cognitive",
        store: Optional[CognitiveStore] = None,
    ) -> bool:
        """保守主动触发：有值得问的东西 且 距上次访谈足够久。

        两个触发口径（避免"gap 生成了却触发不了"的死代码）：
        - 常规：open gap 数达 _TRIGGER_GAP_THRESHOLD（探索型问题攒够一起问）；
        - 高优：**存在 priority >= 0.8 的 gap 即可**，一个就问——真矛盾/画像冲突
          是高价值信号（"你说的跟以前不一样了"），不该为了凑数而搁置。

        store：注入隔离库（多 workspace / 测试）。此前无参 ``CognitiveStore()``
        永远读全局库——tick 明明持有 base_dir，冷却判定却看的是真实库时间戳
        （2026-09-22 收口）。
        冷却改按**秒**比较：原 `(now - last).days < 3` 靠整数截断，虽结果等价，
        但把「至少隔 3 天」写成了「日历天差 < 3」，语义含糊且易被未来改动踩错。
        """
        meta_key = f"last_interview_at:{layer}" if layer != "cognitive" else "last_interview_at"
        try:
            last = (store or CognitiveStore()).get_meta(meta_key)
        except Exception:
            last = None
        if last:
            try:
                from datetime import datetime
                elapsed = datetime.now() - datetime.fromisoformat(str(last))
                if elapsed.total_seconds() < _TRIGGER_MIN_INTERVAL_DAYS * 86_400:
                    return False
            except Exception:
                pass
        gaps = profile.open_gaps()
        if len(gaps) >= _TRIGGER_GAP_THRESHOLD:
            return True
        return any(getattr(g, "priority", 0) >= _HIGH_PRIORITY_GAP_PRIORITY for g in gaps)


# ----------------------------------------------------------------------
# 高层驱动 API（供后端路由 / proactivity 调度调用）
# ----------------------------------------------------------------------
def get_interview_engine() -> SocraticInterviewEngine:
    return SocraticInterviewEngine()


def start_interview(
    limit: int = 3,
    use_llm: bool = False,
    layer: str = "cognitive",
    base_dir=None,
    pm=None,
) -> dict:
    """发起一轮访谈：生成问题 + 建 session。返回 {session_id, questions, total}。

    若暂无可问薄弱点，返回 session_id=None（不建空 session）。
    layer='cognitive' 认知访谈；layer='emotional' 情感访谈（复用同一引擎/骨架）。
    base_dir：多 workspace / 测试隔离入口，按 <base_dir>/deskmate.db 解析。
    """
    if pm is None:
        pm = resolve_profile_manager(layer=layer, base_dir=base_dir)
    store = pm.store
    engine = get_interview_engine()
    questions = engine.generate_questions(pm.profile, limit=limit, use_llm=use_llm)
    if not questions:
        return {"session_id": None, "questions": [],
                "note": "暂无可问的薄弱点（无 open gap / 无低置信条目）"}
    qids = [q["id"] for q in questions]
    # 新一轮发起前先结掉同层旧的 active 会话：防僵尸堆积（真实库曾积 12 个）。
    # 放在 create_session **之前**——顺序反了就成「建完立刻把自己也 expire 掉」。
    try:
        store.expire_active_sessions(layer=layer)
    except Exception:
        logger.debug("清理旧访谈会话失败（不影响发起）", exc_info=True)
    # 复用 gap_ids 列存本次问题 id 列表（session 层语义：本次访谈涉及的目标 id）
    # create_session 内部已按 layer 写 last_interview_at:{layer} 抑制时间戳（防重复触发）
    sid = store.create_session(layer=layer, gap_ids=qids)
    return {"session_id": sid, "questions": questions, "total": len(questions)}


def answer_interview(session_id: str, question_id: str, response: str,
                     use_llm: bool = False, layer: str = "cognitive") -> dict:
    """提交一个访谈回答：回灌画像 + 落 qa（含问题文本）+ 判断是否问完。

    layer 解析优先级：session 记录里存的 layer 为准（session 创建时已定层，
    是单一事实来源）。前端可能不带 layer（旧版 dist 只传 3 参）——若 session
    能查到真实层，就按真实层回灌，避免情感访谈回答被错误写入认知层。
    查不到 session（如测试直调）才回退到调用方传入的 layer。
    """
    store = CognitiveStore()
    sess = store.get_session(session_id)
    real_layer = sess["layer"] if sess else (layer or "cognitive")
    pm = get_profile_manager(layer=real_layer)
    engine = get_interview_engine()
    engine.process_response(pm.profile, question_id, response, layer=real_layer)
    # 反解问题文本落库（qid 无状态可恢复），访谈历史可追溯"问了什么+答了什么"
    q_text = _reconstruct_question_text(_decode_question_id(question_id), pm)
    store.add_qa(session_id=session_id, question_id=question_id, question=q_text, answer=response)
    # F1：访谈回答**也是他亲口说的**，回流当日心情。
    # 此前回答只进认知画像，情感时间线看不到——他说"情绪低落时我会…"，
    # 系统只当那是条画像条目，并不知道他那天低落。画像回答"他是什么人"，
    # 时间线回答"他今天怎么样"，两条腿缺一条就走不动。
    # 纯认知表态（如"书稿出版后就是商品"）读不出情绪 → record_statement 自动
    # 不表态，不会把认知回答硬判成心情。失败不影响上面的画像回灌。
    try:
        from deskmate.growth.emotion_timeline import EmotionTimeline

        EmotionTimeline().record_statement(None, response)
    except Exception as e:
        logger.warning("访谈回答回流情感时间线失败（不影响画像回灌）: %s", e)
    # 判断是否问完：已答数 == 该 session 问题数
    total = len(sess["gap_ids"]) if sess else 0
    answered = len(store.get_qa(session_id))
    done = (total > 0 and answered >= total)
    if done:
        store.finish_session(session_id, summary=f"访谈完成：{answered}/{total} 问答")
    return {
        "ok": True, "session_id": session_id, "answered": answered, "total": total,
        "session_done": done, "open_gaps": len(pm.profile.open_gaps()), "layer": real_layer,
    }


def _known_item_ids(profile) -> set:
    """画像里现存的条目 id 集合（立场/价值/思维/论证四类）。"""
    return {
        getattr(it, "id", None)
        for it in (profile.stances + profile.value_rankings
                   + profile.thinking_frameworks + profile.argument_patterns)
    }


def _gap_is_askable(gap, profile) -> bool:
    """这个 gap 值不值得拿来提问——挡住「无据的空话」。

    真实库教训（2026-09-22）：库里长期躺着两个 **pytest 夹具 gap**
    （``test_interview.py`` 里的 ``topic="矛盾待澄清"/"新领域"``），其中一个的
    ``related_items`` 指向只存在于测试进程的 stance。``generate_questions``
    照单全收，于是访谈从 09-17 起反复问出「关于『矛盾待澄清』…（矛盾）」——
    用户看到只会觉得功能坏了，而根因是数据是测试残留。

    判定（保守，只挡确定无据的）：有 related_items 就必须**全都存在**，
    且 topic/reason 不能同时为空；另外**已经结案的 gap 不再问**——
    重建路径是按 id 反查的，qid 仍指向它，但 status 已经说明这件事
    澄清过（resolved）或被放弃（dismissed）了。
    """
    if getattr(gap, "status", "open") not in ("open", "in_interview"):
        return False
    if not (getattr(gap, "topic", "") or getattr(gap, "reason", "")):
        return False
    refs = getattr(gap, "related_items", None) or []
    known = _known_item_ids(profile)
    return all(r in known for r in refs)


def _profile_item_by_id(item_id, pm):
    """按 id 在五维里找条目（找不到返回 None）。"""
    for it in (pm.profile.stances + pm.profile.value_rankings
               + pm.profile.thinking_frameworks + pm.profile.argument_patterns):
        if getattr(it, "id", None) == item_id:
            return it
    return None


def _lookup_question_subject(meta: dict, pm) -> tuple:
    """从 qid 元数据反查 (topic, content)；无据可依时返回 ("", "")。

    顺序与 ``generate_questions`` 的 gap 分支一致：**先 gap，后条目**——
    gap 型问题（无论 clarify 还是 explore）首次的正文都取自 gap 自身的
    ``topic``/``reason``，qid 里的 ``i=`` 只是「关联条目」（供 clarify 回灌用），
    所以顺序反了会重建出与首次不同的问题。

    ``_gap_is_askable`` 是下游兜底：关联条目已消失（悬空引用）或内容为空时
    宁可不问，也不编一句空话。
    """
    gid = meta.get("related_gap_id")
    if gid:
        for g in pm.profile.cognitive_gaps:
            if getattr(g, "id", None) == gid:
                if not _gap_is_askable(g, pm.profile):
                    return "", ""
                return g.topic, g.reason
    iid = meta.get("related_item_id")
    if iid:
        it = _profile_item_by_id(iid, pm)
        if it is not None:
            topic = getattr(it, "topic", None) or getattr(it, "domain", "") or ""
            if topic or it.content:
                return topic, it.content
    return "", ""


def rebuild_questions_from_qids(qids, pm) -> list:
    """从 session 里的 qid 列表重建问题对象（纯函数、无状态）。

    为什么能重建：``_encode_question_id`` 把 mode 与关联条目/盲区 id 全编进了 qid，
    ``_decode_question_id`` 可反解，再套同一份 ``_Q_TEMPLATES`` 即得原文，
    因此不需要另存一份问题正文。关联对象已被清理（gap 消解、条目被删）的问题
    **直接跳过**——宁可不问，也不编一个无据的问题。
    重建出的 qid 覆盖回原值：同一 session 重放时问题 id 稳定（幂等）。
    """
    out: list = []
    for qid in qids or []:
        try:
            meta = _decode_question_id(qid)
            topic, content = _lookup_question_subject(meta, pm)
            if not topic and not content:
                continue
            q = SocraticInterviewEngine._build(
                topic, content, meta.get("mode", "explore"),
                related_item_id=meta.get("related_item_id"),
                related_gap_id=meta.get("related_gap_id"),
            )
            q["id"] = qid
            out.append(q)
        except Exception:
            logger.debug("重建访谈问题失败，跳过 q=%s", qid, exc_info=True)
    return out


def redeliver_pending_interview(layer: str = "cognitive", base_dir=None, pm=None) -> Optional[dict]:
    """补送「已发起、但用户从未回应过」的访谈邀请；没有挂起的邀请则返回 None。

    这是一次**重放**：不新建会话、不刷新 ``last_interview_at``，所以不受 3 天冷却
    约束——那 3 天是「问过他这件事」的节流，而这里的事他压根没看见。

    触发链路：用户切回 OpenWorker 窗口 → focus 事件 → 前端 tick →
    本函数命中 → 气泡重新出现。因此只要他没答，邀请就不会丢。
    """
    try:
        if pm is None:
            pm = resolve_profile_manager(layer=layer, base_dir=base_dir)
        store = pm.store
        sess = store.latest_active_session(layer)
        if not sess:
            return None
        sid = sess["id"]
        # 已答过至少一题 → 他见过这轮邀请，不再补送（剩下的题由问答流自己推进）
        if store.get_qa(sid):
            return None

        from datetime import datetime, timedelta

        # 时长兜底：挂太久的邀请不再补送，否则会变成「她永远在问同一件事」
        try:
            started = datetime.fromisoformat(str(sess.get("started_at") or ""))
            if datetime.now() - started > timedelta(hours=_REDELIVER_MAX_AGE_HOURS):
                store.expire_active_sessions(layer=layer)
                logger.info("访谈邀请 %s 已挂满 %d 小时无回应，收敛为 expired", sid, _REDELIVER_MAX_AGE_HOURS)
                return None
        except Exception:
            pass

        # 次数上限：避免无限重放
        cnt_key = f"interview_redeliver_count:{sid}"
        try:
            count = int(store.get_meta(cnt_key, 0) or 0)
        except Exception:
            count = 0
        if count >= _REDELIVER_MAX_TIMES:
            return None

        # 节流：前端 tick 最快 15s 一次，不节流会把气泡反复顶出来
        try:
            last = store.get_meta(f"last_interview_redeliver_at:{layer}")
            if last:
                if datetime.now() - datetime.fromisoformat(str(last)) < timedelta(
                    minutes=_REDELIVER_MIN_INTERVAL_MINUTES
                ):
                    return None
        except Exception:
            pass

        questions = rebuild_questions_from_qids(sess.get("gap_ids"), pm)
        if not questions:
            # 问题里的关联对象（gap / 立场）已全部消失或结案 → 这轮问题再也重建不出来。
            # 留着它只会白占 48h（等着时长兜底），把这一层**新的**邀请全挡在门外
            # （maybe_trigger_interview 见到挂起就只补送）。这种僵尸不必等，立刻收敛。
            try:
                store.expire_active_sessions(layer=layer)
                logger.info("访谈邀请 %s 的关联对象已失效、问题无法重建，收敛为 expired", sid)
            except Exception:
                logger.debug("收敛失效访谈邀请失败（不影响主流程）", exc_info=True)
            return None

        store.set_meta(f"last_interview_redeliver_at:{layer}", _now())
        store.set_meta(cnt_key, count + 1)
        logger.info("补送访谈邀请 %s（第 %d 次，layer=%s）", sid, count + 1, layer)
        return {
            "session_id": sid,
            "questions": questions,
            "total": len(questions),
            "redelivered": True,
        }
    except Exception as e:
        logger.debug("补送访谈邀请失败（不影响主流程）: %s", e, exc_info=True)
        return None


def maybe_trigger_interview(
    limit: int = 3,
    layer: str = "cognitive",
    base_dir=None,
) -> Optional[dict]:
    """保守主动触发钩子：满足 should_interview_now 才发起，否则返回 None。

    供 proactivity tick / 定时任务调用。

    ⚠ 调用方必须在**有 UI 呈现能力**的通道里调用（前端 proactive/tick、手动入口）。
    后台 scheduler tick 不要调：它的产物只有一条只认 suggestion 的 WS 广播，
    interview 送不到用户眼前，却已把 3 天冷却消耗掉——那会让前端永远收不到邀请
    （2026-09-22 定位的死循环）。后台请用 ``ProactiveEngine.tick(allow_interactive=False)``。

    顺序说明：先看有没有**挂起未回应**的邀请，有就只补送。既不能先过
    ``should_interview_now``（冷却会直接把补送挡死，用户永远等不到那张已发出的邀请），
    也不能绕过补送另开新会话（会继续堆僵尸 active，同一批 gap 被反复问）。
    """
    pm = resolve_profile_manager(layer=layer, base_dir=base_dir)
    store = pm.store
    pending = store.latest_active_session(layer)
    if pending and not store.get_qa(pending["id"]):
        redelivered = redeliver_pending_interview(layer=layer, pm=pm)
        if redelivered:
            return redelivered
        # 补送没产出：区分两种情形——
        # ①那条挂起邀请已被判「问题不可重建」而收敛（挂起已消失）→ 本次继续走正常发起路径，
        #   不让这一层白等 48 小时；
        # ②仍在挂起（节流窗口内 / 次数上限 / 还没到时长兜底）→ 什么都不做，下次 tick 再试。
        if store.latest_active_session(layer):
            return None
    if not get_interview_engine().should_interview_now(pm.profile, layer=layer, store=store):
        return None
    return start_interview(limit=limit, layer=layer, pm=pm)


def seed_missing_dimensions(layer: str = "cognitive", only_empty: bool = True, pm=None) -> dict:
    """从现有立场推导空维度的候选认知（标 questioned 待用户确认，不逼不灌噪声）。

    让 value / argument / thinking 三维也有内容起点；候选进访谈队列，
    用户澄清（process_response clarify 模式）后转 active。符合「观点长出来，不靠规则逼」。
    fail-open：LLM 不可用 / 解析失败 → 返回 {"seeded":0,"error":...}。

    ``pm`` 为 None 时读写默认层画像（供 proactivity tick 调用）；测试可传入隔离库。
    """
    from deskmate.memory.llm import chat, llm_available
    from .extractor import _extract_json

    if pm is None:
        pm = get_profile_manager(layer=layer)
    profile = pm.get()
    if only_empty:
        empty = [
            name for name, items in (
                ("value", profile.active_values()),
                ("thinking", profile.active_frameworks()),
                ("argument", profile.active_patterns()),
            ) if not items
        ]
    else:
        empty = ["value", "thinking", "argument"]
    if not empty:
        return {"seeded": 0, "note": "维度已非空，无需 seed"}
    if not llm_available():
        return {"seeded": 0, "error": "llm_not_available"}

    stance_lines = [f"- {s.topic}：{s.content[:80]}" for s in profile.active_stances()[:15]]
    if not stance_lines:
        return {"seeded": 0, "note": "无立场可推导"}

    system = (
        "你从一位作者的「立场图谱」推导他可能的其他认知维度特征。只做合理归纳，不编造无据内容。"
        "每条必须带 contexts（适用情境；普适则给空数组）。输出严格 JSON："
        '{"value_rankings":[{"domain":"","ranking":[],"content":"","tradeoff_notes":"","contexts":[]}],'
        '"thinking_frameworks":[{"domain":"","steps":[],"content":"","contexts":[]}],'
        '"argument_patterns":[{"content":"","pattern_type":"","evidence_preference":"","contexts":[]}]}'
    )
    user = "已有立场：\n" + "\n".join(stance_lines) + "\n\n请推导空维度候选（只输出 JSON）。"
    try:
        raw = chat(system=system, user=user, temperature=0.3, max_tokens=1200, timeout=120.0)
    except Exception as e:
        logger.warning("seed_missing_dimensions LLM 失败: %s", e)
        return {"seeded": 0, "error": f"llm_call_failed: {e}"}
    data = _extract_json(raw)
    if not data:
        return {"seeded": 0, "error": "json_parse_failed"}

    seeded = 0

    def _src():
        return SourceRef(type="derived", title="立场推导", excerpt="由立场推导", date=_now(), confidence=0.55)

    if "value" in empty:
        for v in data.get("value_rankings", [])[:2]:
            domain = (v.get("domain") or "").strip()
            content = (v.get("content") or "").strip()
            if not domain and not content:
                continue
            it, op = pm.add_value_ranking(
                domain=domain, ranking=v.get("ranking", []), content=content,
                source=_src(), tradeoff_notes=v.get("tradeoff_notes", ""),
                tags=["推导候选"], contexts=v.get("contexts") or [])
            if op == "created":
                it.mark_questioned(reason="由立场推导候选，待你确认")
                seeded += 1
    if "thinking" in empty:
        for f in data.get("thinking_frameworks", [])[:2]:
            domain = (f.get("domain") or "").strip()
            content = (f.get("content") or "").strip()
            if not domain and not content:
                continue
            it, op = pm.add_thinking_framework(
                domain=domain, steps=f.get("steps", []), content=content,
                source=_src(), tags=["推导候选"], contexts=f.get("contexts") or [])
            if op == "created":
                it.mark_questioned(reason="由立场推导候选，待你确认")
                seeded += 1
    if "argument" in empty:
        for p in data.get("argument_patterns", [])[:2]:
            content = (p.get("content") or "").strip()
            if not content:
                continue
            it, op = pm.add_argument_pattern(
                content=content, source=_src(),
                pattern_type=p.get("pattern_type", ""),
                evidence_preference=p.get("evidence_preference", ""),
                tags=["推导候选"], contexts=p.get("contexts") or [])
            if op == "created":
                it.mark_questioned(reason="由立场推导候选，待你确认")
                seeded += 1
    if seeded:
        pm.save()
    return {"seeded": seeded}
