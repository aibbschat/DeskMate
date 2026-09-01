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

from .interfaces import InterviewEngine, register_engine
from .models import CognitiveProfile, SourceRef, HistoryEntry, _now
from .profile import get_profile_manager
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


class SocraticInterviewEngine(InterviewEngine):
    """苏格拉底式深度访谈引擎——③b 落地实现。"""

    def generate_questions(
        self, profile: CognitiveProfile, limit: int = 3, use_llm: bool = False
    ) -> list[dict]:
        """生成访谈问题，优先级：questioned 矛盾 → open gap → 低置信 active。

        use_llm 当前保留（规则优先）；LLM 润色为可选增强，待 provider 配置后接入。
        """
        questions: list[dict] = []

        # 1) questioned 条目（矛盾待澄清）
        for s in profile.stances:
            if s.status == "questioned":
                questions.append(self._build(s.topic, s.content, "clarify", related_item_id=s.id))

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
        self, profile: CognitiveProfile, question_id: str, response: str
    ) -> CognitiveProfile:
        """处理用户对某访谈问题的回答，回灌认知画像。

        返回更新后的 profile（manager 已 save）。fail-open：异常只记 warning。
        """
        meta = _decode_question_id(question_id)
        pm = get_profile_manager()
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
                self._extract_and_resolve_gap(pm, meta["related_gap_id"], response)
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
    def _extract_and_resolve_gap(pm, gap_id, response):
        """从回答提取新认知（规则优先）并消解该 gap。"""
        try:
            get_extractor().extract_from_text(
                text=response, source_title="深度访谈", source_type="interview",
                source_date=_now(), use_llm=False)
        except Exception as e:
            logger.warning("访谈回答认知提取失败（仍解 gap）: %s", e)
        for g in pm.profile.cognitive_gaps:
            if g.id == gap_id and g.status == "open":
                g.status = "resolved"
                g.resolved_at = _now()
        pm.save()

    def should_interview_now(self, profile: CognitiveProfile) -> bool:
        """保守主动触发：open gap 数达阈值 且 距上次访谈足够久。"""
        try:
            last = CognitiveStore().get_meta("last_interview_at")
        except Exception:
            last = None
        if last:
            try:
                from datetime import datetime
                days = (datetime.now() - datetime.fromisoformat(last)).days
                if days < _TRIGGER_MIN_INTERVAL_DAYS:
                    return False
            except Exception:
                pass
        return len(profile.open_gaps()) >= _TRIGGER_GAP_THRESHOLD


# ----------------------------------------------------------------------
# 高层驱动 API（供后端路由 / proactivity 调度调用）
# ----------------------------------------------------------------------
def get_interview_engine() -> SocraticInterviewEngine:
    return SocraticInterviewEngine()


def start_interview(limit: int = 3, use_llm: bool = False) -> dict:
    """发起一轮访谈：生成问题 + 建 session。返回 {session_id, questions, total}。

    若暂无可问薄弱点，返回 session_id=None（不建空 session）。
    """
    pm = get_profile_manager()
    engine = get_interview_engine()
    questions = engine.generate_questions(pm.profile, limit=limit, use_llm=use_llm)
    if not questions:
        return {"session_id": None, "questions": [],
                "note": "暂无可问的认知薄弱点（无 open gap / 无低置信条目）"}
    qids = [q["id"] for q in questions]
    store = CognitiveStore()
    # 复用 gap_ids 列存本次问题 id 列表（session 层语义：本次访谈涉及的目标 id）
    sid = store.create_session(gap_ids=qids)
    return {"session_id": sid, "questions": questions, "total": len(questions)}


def answer_interview(session_id: str, question_id: str, response: str, use_llm: bool = False) -> dict:
    """提交一个访谈回答：回灌画像 + 落 qa + 判断是否问完。"""
    pm = get_profile_manager()
    engine = get_interview_engine()
    engine.process_response(pm.profile, question_id, response)
    store = CognitiveStore()
    store.add_qa(session_id=session_id, question_id=question_id, question="", answer=response)
    # 判断是否问完：已答数 == 该 session 问题数
    sessions = [s for s in store.list_sessions() if s["id"] == session_id]
    total = len(sessions[0]["gap_ids"]) if sessions else 0
    answered = len(store.get_qa(session_id))
    done = (total > 0 and answered >= total)
    if done:
        store.finish_session(session_id, summary=f"访谈完成：{answered}/{total} 问答")
    return {
        "ok": True, "session_id": session_id, "answered": answered, "total": total,
        "session_done": done, "open_gaps": len(pm.profile.open_gaps()),
    }


def maybe_trigger_interview(limit: int = 3) -> Optional[dict]:
    """保守主动触发钩子：满足 should_interview_now 才发起，否则返回 None。

    供 proactivity tick / 定时任务调用；本轮不自动挂 UI（避免唠叨，前端访谈页后续做）。
    """
    pm = get_profile_manager()
    if not get_interview_engine().should_interview_now(pm.profile):
        return None
    return start_interview(limit=limit)


# 注册（导入即生效）——registry["interview_engine"] 不再是 None
register_engine("interview_engine", SocraticInterviewEngine)
