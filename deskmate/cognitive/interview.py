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
import uuid
from typing import Optional

from .interfaces import InterviewEngine
from .models import CognitiveProfile, SourceRef, HistoryEntry, Stance, _now
from .profile import get_profile_manager, resolve_profile_manager
from .store import CognitiveStore
from .extractor import get_extractor

logger = logging.getLogger(__name__)

# 苏格拉底式问题模板（按 mode）
_Q_TEMPLATES = {
    "clarify": (
        "关于「{topic}」，你之前留下过有点矛盾的说法（{content}）。"
        "能跟我说说你真正的立场是什么吗？什么情况下会有不同？"
    ),
    "deepen": (
        "你提到「{topic}」时写了「{content}」。这背后更底层的判断是什么？"
        "为什么这对你这么重要？"
    ),
    "explore": (
        "我想更懂你在「{topic}」上的想法。能不能讲一个具体的例子，"
        "说明你实际会怎么看、怎么做？"
    ),
}

_TRIGGER_GAP_THRESHOLD = 2          # open gap >= 此数才考虑主动发起
_TRIGGER_MIN_INTERVAL_DAYS = 3     # 距上次访谈至少 N 天才再主动
# 高优先级 gap（真矛盾 / 画像冲突，priority >= 此值）：**一个就值得问**，不必凑数。
# 探索型 gap（ensure_emotional_gaps 播的 0.7）仍需凑够 _TRIGGER_GAP_THRESHOLD 个——
# 「想多了解你」可以攒着一起问，「你说的跟以前不一样了」必须立刻澄清。
_HIGH_PRIORITY_GAP_PRIORITY = 0.8


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
        gaps = sorted(profile.open_gaps(), key=lambda g: -g.priority)
        for g in gaps:
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

        # 去重（同 topic 只留一条）+ 截断
        seen, uniq = set(), []
        for q in questions:
            if q["topic"] not in seen:
                seen.add(q["topic"])
                uniq.append(q)
        return uniq[:limit]

    @staticmethod
    def _build(topic, content, mode, related_item_id=None, related_gap_id=None) -> dict:
        tmpl = _Q_TEMPLATES.get(mode, _Q_TEMPLATES["explore"])
        q_text = tmpl.format(topic=topic or "这个话题", content=(content or "")[:60])
        return {
            "id": _encode_question_id(related_item_id, related_gap_id, mode),
            "topic": topic,
            "question": q_text,
            "context": content,
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
    """
    pm = resolve_profile_manager(layer=layer, base_dir=base_dir)
    if not get_interview_engine().should_interview_now(pm.profile, layer=layer, store=pm.store):
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
