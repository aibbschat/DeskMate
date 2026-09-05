"""
ValidationEngine 实现 ——「认知 Turing test」。

让 AI 以用户身份回答一个"用户没遇到过"的具体情境，
再和用户的实际回答对比，评估"像我"的程度，结果反哺认知画像。

零 LLM、纯本地规则实现（与 memory_injector 同思路）：
- 生成情境：从高置信度画像条目（立场/价值观/思维框架）抽话题，套假设模板
- 评估相似度：基于画像条目做主题/关键词/立场一致性比对，四维度打分
- 反哺画像：像 → 提升相关条目置信度；不像 → 降置信度 + 生成 cognitive_gap

设计原则：
- fail-open：任何一步失败都返回空/保守结果，不阻断
- 诚实：评估是"基于画像的启发式对比"，不是精确 Turing test，结果带说明
- 复用：画像条目结构、add_gap、置信度更新都走现有 profile API
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Optional

from deskmate.cognitive.interfaces import ValidationEngine
from deskmate.cognitive.models import CognitiveProfile, SourceRef, HistoryEntry

logger = logging.getLogger(__name__)

# 维度定义（评估四维度）
DIMENSIONS = ["stance", "argument", "value", "expression"]
DIM_LABELS = {
    "stance": "立场一致性",
    "argument": "论证模式",
    "value": "价值观排序",
    "expression": "表达习惯",
}

# 情境模板：把画像话题套进"你没遇到过"的假设场景
# 每类一个模板，{topic} 会被替换成画像条目的真实内容
_SCENARIO_TEMPLATES: list[dict[str, Any]] = [
    {
        "kind": "创作约稿",
        "template": "一个平台向你约稿：要求你把作品里某个历史人物{modifier}，稿费翻倍。但你觉得这会违背你对「{topic}」的理解。你会怎么处理？",
        "modifier": "改成更讨喜的结局",
    },
    {
        "kind": "创作取舍",
        "template": "你同时接到两个机会：一个能让你{modifier}，但需要你放弃坚持的「{topic}」；另一个符合你的原则但收益小。你怎么选？",
        "modifier": "更早被更多人看到",
    },
    {
        "kind": "历史态度",
        "template": "有人用一部流行剧里完全架空的情节，来佐证「{topic}」的真实历史。你听完会怎么回应？",
        "modifier": "",
    },
    {
        "kind": "价值冲突",
        "template": "你正处在两难：{modifier}。这正好撞上你排序里靠前的「{topic}」。你最后会守住什么、放弃什么？",
        "modifier": "一边是难得的机会，一边是家人的期望",
    },
]

# 评估用：回答中如果出现与画像条目一致的核心关键词 → 判定"立场一致"
# 从画像条目 content 里抽有信息量的词（2字以上、非停用词）
_STOPWORDS = {
    "一个", "这个", "那个", "什么", "怎么", "我们", "你们", "他们", "自己", "因为", "所以",
    "但是", "还是", "就是", "不是", "没有", "可以", "应该", "可能", "觉得", "认为", "作者",
    "历史", "创作", "小说", "作品", "读者", "如果", "然后", "而且", "或者", "对于", "关于",
    "非常", "一定", "真的", "比较", "有点", "其实", "当然", "不过", "要是", "反正",
}


def _normalize(text: str) -> str:
    return re.sub(r"[\s\W]+", "", text or "").lower()


def _extract_keywords(text: str, limit: int = 6) -> list[str]:
    """从画像条目 content 抽关键词（2-4 字、非停用词）。"""
    if not text:
        return []
    # 去标点分词（简单按 2-4 字滑窗 + 词性过滤的轻量实现）
    cleaned = re.sub(r"[，。；：、！？（）“”\"'\s]+", " ", text or "")
    # 抽候选词：中文 2-4 字连续片段（去掉停用词）
    cands: list[str] = []
    for m in re.finditer(r"[\u4e00-\u9fa5]{2,6}", cleaned):
        w = m.group(0)
        if w in _STOPWORDS or len(w) < 2:
            continue
        cands.append(w)
    # 去重保序，取前 limit
    seen: set[str] = set()
    out: list[str] = []
    for w in cands:
        if w not in seen:
            seen.add(w)
            out.append(w)
        if len(out) >= limit:
            break
    return out


def _topic_of_item(item: Any) -> str:
    """取画像条目的主题词（立场用 topic，价值观用 domain，框架用 domain）。"""
    if getattr(item, "topic", ""):
        return item.topic
    if getattr(item, "domain", ""):
        return item.domain
    return ""


def _content_of_item(item: Any) -> str:
    return getattr(item, "content", "") or ""


class TuringScenarioEngine(ValidationEngine):  # noqa: F821 (base 在 interfaces 导入)
    """规则驱动的认知 Turing test 引擎（零 LLM）。"""

    # ------------------------------------------------------------------
    # 1. 生成情境
    # ------------------------------------------------------------------
    def generate_scenario(self, profile: CognitiveProfile, domain: Optional[str] = None) -> dict:
        """
        从高置信度画像条目抽一个话题，套进"没遇到过"的情境。

        策略：
        - 优先取置信度 >= 0.7 的 active 条目（立场 > 价值观 > 思维框架 > 论证）
        - domain 可限定取某领域
        - 场景 id 用 uuid，related_items 记录引用了哪些画像条目
        """
        if profile is None:
            return {"id": "", "domain": domain or "general", "scenario": "", "question": "", "related_items": []}

        # 收集候选条目（含维度信息）
        candidates: list[tuple[str, Any]] = []
        for s in profile.active_stances():
            if s.confidence >= 0.7:
                candidates.append(("stance", s))
        for v in profile.active_values():
            if v.confidence >= 0.7:
                candidates.append(("value", v))
        for f in profile.active_frameworks():
            if f.confidence >= 0.7:
                candidates.append(("thinking", f))

        if domain:
            filtered = [(d, it) for d, it in candidates if domain in (_topic_of_item(it) or "")]
            if filtered:
                candidates = filtered

        if not candidates:
            return {"id": "", "domain": domain or "general", "scenario": "", "question": "", "related_items": []}

        # 按置信度降序取第一个（最确定的条目 → 最值得验证）
        candidates.sort(key=lambda x: x[1].confidence, reverse=True)
        dim, item = candidates[0]
        topic = _topic_of_item(item)
        content = _content_of_item(item)

        # 套模板（创作类优先用创作模板，其他用通用）
        template = _SCENARIO_TEMPLATES[0] if dim in ("stance", "thinking") else _SCENARIO_TEMPLATES[3]
        scenario_text = template["template"].replace("{topic}", topic or content[:12]).replace("{modifier}", template.get("modifier", ""))
        question = f"在「{template['kind']}」情境下，你会怎么做？为什么？"

        return {
            "id": uuid.uuid4().hex[:12],
            "domain": domain or topic or "general",
            "scenario": scenario_text,
            "question": question,
            "related_items": [item.id],
            "source_item": {"dim": dim, "topic": topic, "content": content, "confidence": item.confidence},
        }

    # ------------------------------------------------------------------
    # 2. 评估相似度
    # ------------------------------------------------------------------
    def evaluate_similarity(
        self,
        profile: CognitiveProfile,
        scenario_id: str,
        ai_response: str,
        user_response: str,
        related_items: Optional[list[str]] = None,
        use_llm: bool = True,
    ) -> dict:
        """
        评估 AI 回答和用户回答的相似度（四维度）。

        优先用 LLM 做语义级对比（理解同义改写、论证路径），
        LLM 不可用 / 超时 / 解析失败时自动降级到本地规则引擎（fail-open）。

        返回：{"overall_score", "dimension_scores", "divergence_points", "feedback"}
        """
        if not ai_response or not user_response:
            return {
                "overall_score": 0.0,
                "dimension_scores": {},
                "divergence_points": ["回答不完整，无法评估"],
                "feedback": "回答缺失，本次验证无效。",
            }

        # LLM 优先：语义级评估（理解同义改写/论证路径），失败静默降级规则
        if use_llm:
            llm_ev = self._evaluate_with_llm(profile, ai_response, user_response, related_items)
            if llm_ev is not None:
                llm_ev["_engine"] = "llm"
                return llm_ev

        ev = self._evaluate_rules(profile, ai_response, user_response, related_items)
        ev["_engine"] = "rules"
        return ev

    def _evaluate_with_llm(
        self,
        profile: CognitiveProfile,
        ai_response: str,
        user_response: str,
        related_items: Optional[list[str]] = None,
    ) -> Optional[dict]:
        """调用 LLM 做语义级相似度评估。任何异常返回 None（调用方降级规则）。"""
        try:
            from deskmate.memory.llm import chat_memory, memory_llm_available
        except Exception:
            return None
        if not memory_llm_available():
            return None

        # 收集画像条目（供 LLM 判断"像不像你"）
        related: list[Any] = []
        if related_items:
            all_items = (
                list(profile.active_stances())
                + list(profile.active_values())
                + list(profile.active_frameworks())
                + list(profile.active_patterns())
            )
            related = [it for it in all_items if it.id in related_items]
        if not related:
            for s in profile.active_stances()[:6]:
                related.append(s)

        # 画像条目摘要（立场/价值观为主，最多 5 条）
        item_lines: list[str] = []
        for it in related[:5]:
            tag = "立场" if getattr(it, "topic", "") else "价值观/框架"
            title = getattr(it, "topic", "") or getattr(it, "domain", "") or "未分类"
            content = _content_of_item(it)[:90]
            conf = getattr(it, "confidence", 0.5)
            item_lines.append(f"- [{tag}] {title}：{content}（置信度 {conf:.0%}）")

        system = (
            "你是一个严谨的「认知相似度评估器」。你会拿到用户的认知画像（已确认的立场/价值观/思维框架）、"
            "一个验证情境、一段 AI 模拟用户身份的回答、一段用户的真实回答。\n"
            "你的任务：判断 AI 的回答在多大程度上像『这个用户本人』会说的话。"
            "要理解语义，而不是只对关键词。\n"
            "评分维度（各 0-1 小数）：\n"
            "- stance 立场一致性：核心观点方向是否一致，是否都贴合画像立场\n"
            "- argument 论证模式：论证路径/依据是否相似（是否都引用史实、讲因果、讲代价、讲原则）\n"
            "- value 价值观排序：涉及取舍时，优先级是否一致\n"
            "- expression 表达习惯：用词、语气、书面化程度、严谨度是否相似\n"
            "overall_score 是四维综合。divergence_points 列出 AI 与用户真实差异的具体点（无则空数组）。\n"
            "只输出 JSON，不要任何其他文字。"
        )
        user = (
            f"【用户认知画像】\n{chr(10).join(item_lines)}\n\n"
            f"【AI 模拟回答】\n{ai_response}\n\n"
            f"【用户真实回答】\n{user_response}\n\n"
            f"输出 JSON：{{\"overall_score\": 0-1, \"dimension_scores\": "
            f"{{\"stance\":0-1,\"argument\":0-1,\"value\":0-1,\"expression\":0-1}}, "
            f"\"divergence_points\": [\"...\"], \"feedback\": \"一句话总结\"}}"
        )

        try:
            raw = chat_memory(
                system,
                user,
                temperature=0.2,
                max_tokens=500,
                timeout=60.0,
            )
        except Exception:
            return None

        if not raw:
            return None
        # 容错解析：剥离 ```json 包裹 / 提取第一个 { ... }
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
        except Exception:
            return None

        # 校验字段
        dims = d.get("dimension_scores") or {}
        def _num(v: Any, default: float) -> float:
            try:
                f = float(v)
                return min(1.0, max(0.0, f))
            except Exception:
                return default

        dim_scores = {
            "stance": _num(dims.get("stance"), 0.5),
            "argument": _num(dims.get("argument"), 0.5),
            "value": _num(dims.get("value"), 0.5),
            "expression": _num(dims.get("expression"), 0.5),
        }
        overall = _num(d.get("overall_score"), sum(dim_scores.values()) / 4)
        divergence = d.get("divergence_points") or []
        if not isinstance(divergence, list):
            divergence = []
        feedback = str(d.get("feedback") or "")
        if not feedback:
            feedback = "AI 的回答与你的回答大体一致。" if overall >= 0.5 else "AI 的回答和你差别较大。"

        return {
            "overall_score": round(overall, 2),
            "dimension_scores": {k: round(v, 2) for k, v in dim_scores.items()},
            "divergence_points": divergence,
            "feedback": feedback,
        }

    def _evaluate_rules(
        self,
        profile: CognitiveProfile,
        ai_response: str,
        user_response: str,
        related_items: Optional[list[str]] = None,
    ) -> dict:
        """
        规则驱动的相似度评估（LLM 不可用时的 fallback）。

        逻辑：
        - stance（立场一致性）：先判方向（坚守/妥协词），再叠加与画像关键词贴合度。
        - argument（论证模式）：论证连接词 + 思维框架步骤。
        - value（价值观排序）：排序优先项 + 价值观内容关键词。
        - expression（表达习惯）：用词重合 + 作者风格词。

        related_items：限定评估只针对场景引用的画像条目（默认全量，取前 6）。
        """
        dim_scores: dict[str, float] = {}
        divergence: list[str] = []

        # 找画像里和该场景相关的条目（优先 related_items，否则全量）
        related: list[Any] = []
        if related_items:
            all_items = (
                list(profile.active_stances())
                + list(profile.active_values())
                + list(profile.active_frameworks())
                + list(profile.active_patterns())
            )
            related = [it for it in all_items if it.id in related_items]
        if not related:
            for s in profile.active_stances():
                related.append(s)
            for v in profile.active_values():
                related.append(v)

        # --- stance：立场一致性 ---
        # 关键：先判"方向一致性"（AI 与用户对同一情境的决策方向是否一致），
        # 再叠加"与画像关键词贴合度"。
        # 决策方向词：正向坚守（守/坚持/不改/不会改/拒绝/宁可/尊重/守住）
        #              负向妥协（改/会改/放弃/妥协/接受/迁就/商业）
        positive_commit = [
            "不改", "不会改", "不能改", "拒绝", "守住", "坚持", "尊重", "宁可",
            "不让步", "不会退让", "不妥协", "不动摇", "原则不能", "不能放弃",
            "不会因为", "不违背", "不能违背", "遵守", "遵循", "守住底线",
        ]
        negative_commit = [
            "会改", "改啊", "可以改", "放弃", "妥协", "接受", "迁就", "迎合",
            "商业", "稿费", "屈服", "改成", "顺从", "让步", "退让", "动摇",
        ]

        def _direction(text: str) -> int:
            # 先数正向（长词优先，命中即算）
            pos = sum(1 for w in positive_commit if w in text)
            # 负向：排除"不+负向词"的否定语境（如"不让步"算正向不算负向）
            neg = 0
            for w in negative_commit:
                idx = text.find(w)
                while idx != -1:
                    # 词前紧邻"不/没/无" → 是肯定语境（如"不让步""不能动摇"），不算负向
                    prev = text[idx - 1] if idx > 0 else ""
                    if prev not in ("不", "没", "无"):
                        neg += 1
                    idx = text.find(w, idx + 1)
            return 1 if pos > neg else (-1 if neg > pos else 0)

        u_dir = _direction(user_response)
        a_dir = _direction(ai_response)
        if u_dir != 0 and a_dir != 0:
            # 双方都有明确方向：一致 → 高分；相反 → 低分
            dir_match = 1.0 if u_dir == a_dir else 0.15
        else:
            # 任一方方向不明 → 中性偏保守
            dir_match = 0.5

        # 与画像关键词贴合度（用户回答贴近画像 = 画像可信；AI 贴近画像 = 它懂你）
        user_hits = 0
        ai_hits = 0
        total_kw = 0
        user_rate = 0.0
        ai_rate = 0.0
        for item in related[:6]:
            kws = _extract_keywords(_content_of_item(item), limit=3)
            if not kws:
                continue
            total_kw += len(kws)
            for kw in kws:
                if kw in user_response:
                    user_hits += 1
                if kw in ai_response:
                    ai_hits += 1
        if total_kw > 0:
            user_rate = user_hits / total_kw
            ai_rate = ai_hits / total_kw
            fit = min(1.0, user_rate * 0.6 + ai_rate * 0.4)
        else:
            fit = 0.5

        # 组合：方向一致性 60% + 画像贴合度 40%
        dim_scores["stance"] = round(min(1.0, max(0.0, dir_match * 0.6 + fit * 0.4)), 2)
        if u_dir != 0 and a_dir != 0 and u_dir != a_dir:
            divergence.append("AI 的回答方向和你相反——它误解了你在这类情境下的选择。")
        elif user_rate < 0.15 and ai_rate >= 0.3:
            divergence.append("AI 的回答比你的回答更贴近画像立场——它可能高估了你的原则强度。")

        # --- argument：论证模式 ---
        arg_markers = ["因为", "所以", "但是", "如果", "假如", "因此", "不过", "同时"]
        user_args = sum(1 for m in arg_markers if m in user_response)
        ai_args = sum(1 for m in arg_markers if m in ai_response)
        arg_ratio = min(1.0, user_args / 3) if user_args > 0 else 0.4
        arg_sim = 1 - min(1.0, abs(user_args - ai_args) / 4)
        dim_scores["argument"] = round(min(1.0, arg_sim * 0.5 + arg_ratio * 0.5), 2)

        # --- value：价值观排序 ---
        user_value_hits = 0
        for v in related:
            for pri in (v.ranking if getattr(v, "ranking", None) else [])[:4]:
                if pri and pri in user_response:
                    user_value_hits += 1
        # 价值观贴合：排序词命中 + 用户回答贴合价值观内容关键词
        user_value_kw = 0
        value_total_kw = 0
        for v in related:
            vkws = _extract_keywords(_content_of_item(v), limit=3)
            if not vkws:
                continue
            value_total_kw += len(vkws)
            for kw in vkws:
                if kw in user_response:
                    user_value_kw += 1
        value_fit = (user_value_kw / value_total_kw) if value_total_kw else 0.4
        # 排序词命中优先（体现"取舍"），关键词贴合其次
        dim_scores["value"] = round(min(1.0, 0.35 + user_value_hits * 0.25 + value_fit * 0.3), 2)

        # --- expression：表达习惯 ---
        style_words = ["史料", "考据", "真实", "严肃", "严谨", "史料", "历史", "细节"]
        user_style = sum(1 for w in style_words if w in user_response)
        ai_style = sum(1 for w in style_words if w in ai_response)
        style_ratio = min(1.0, user_style / 2)
        style_sim = 1 - min(1.0, abs(user_style - ai_style) / 3)
        dim_scores["expression"] = round(min(1.0, style_sim * 0.5 + style_ratio * 0.5), 2)

        overall = round(sum(dim_scores.values()) / len(dim_scores) if dim_scores else 0.0, 2)

        # 反馈文案
        if overall >= 0.75:
            feedback = "AI 的回答与你高度一致——它确实懂你在这类情境下的选择。"
        elif overall >= 0.5:
            feedback = "AI 的回答与你大体一致，但在某些维度还有偏差，值得继续观察。"
        else:
            feedback = "AI 的回答和你差别较大——这暴露了它对你的理解盲区。"
            for d, label in DIM_LABELS.items():
                if dim_scores.get(d, 1.0) < 0.4:
                    divergence.append(f"{label}偏低（{dim_scores[d]:.0%}）——这是画像薄弱点。")

        return {
            "overall_score": overall,
            "dimension_scores": dim_scores,
            "divergence_points": divergence,
            "feedback": feedback,
        }

    # ------------------------------------------------------------------
    # 3. 反哺画像
    # ------------------------------------------------------------------
    def apply_feedback(self, profile: CognitiveProfile, evaluation: dict) -> CognitiveProfile:
        """
        将验证反馈应用到认知画像。

        - overall >= 0.75（像）：提升相关条目置信度
        - overall < 0.5（不像）：降相关条目置信度 + 生成 cognitive_gap
        - 中段：微调，不生成 gap
        """
        if profile is None:
            return profile

        overall = float(evaluation.get("overall_score", 0.0))
        related_items = evaluation.get("related_items", [])
        scenario = evaluation.get("scenario", "") or ""
        dims = evaluation.get("dimension_scores", {}) or {}

        # 定位相关条目
        targets = []
        if related_items:
            for it in profile.stances + profile.argument_patterns + profile.value_rankings + profile.thinking_frameworks:
                if it.id in related_items:
                    targets.append(it)

        note = ""
        if overall >= 0.75 and targets:
            # 像：置信度 +0.1（不超 0.95）
            for t in targets:
                old = t.confidence
                t.confidence = min(0.95, old + 0.1)
                t.last_updated = datetime.now().isoformat()
                t.history.append(HistoryEntry(
                    action="validated",
                    old_value=str(old),
                    new_value=str(t.confidence),
                    reason="Turing test 验证一致（像你）",
                ))
            note = f"验证一致：{len(targets)} 条画像置信度提升"
        elif overall < 0.5:
            # 不像：降置信度 + 生成 gap
            if targets:
                for t in targets:
                    old = t.confidence
                    t.confidence = max(0.3, old - 0.12)
                    t.last_updated = datetime.now().isoformat()
                    t.history.append(HistoryEntry(
                        action="downgraded",
                        old_value=str(old),
                        new_value=str(t.confidence),
                        reason="Turing test 验证不符（不像你）",
                    ))
            # 生成 gap（幂等：同 topic 已有 open gap 则跳过）
            topic = f"验证盲区：{_topic_from_eval(evaluation)}"
            existing = [g for g in profile.cognitive_gaps if g.status == "open" and g.topic == topic]
            if not existing:
                from deskmate.cognitive.models import CognitiveGap

                profile.cognitive_gaps.append(CognitiveGap(
                    topic=topic,
                    reason=scenario[:60] if scenario else "AI 在该情境下对你的判断出现偏差，需要访谈澄清",
                    priority=0.7,
                    related_items=[t.id for t in targets],
                ))
                note = f"验证不符：{len(targets)} 条画像置信度降低，已生成待澄清盲区"
            else:
                note = f"验证不符：{len(targets)} 条画像置信度降低（盲区已存在）"
        else:
            note = "验证结果中性，画像微调待更多样本。"

        # 记录验证历史（profile 上如果有扩展字段则记，否则记到条目）
        if hasattr(profile, "validation_count"):
            profile.validation_count += 1

        return profile

    def _evaluate_stub(self) -> dict:
        """占位——保留接口，实际逻辑走 evaluate_similarity。"""
        return {}


def _topic_from_eval(evaluation: dict) -> str:
    """从 evaluation 里取个主题词用于 gap 命名。"""
    scenario = evaluation.get("scenario", "") or ""
    m = re.search(r"「([^」]+)」", scenario)
    if m:
        return m.group(1)[:20]
    return scenario[:20] or "未分类"


# ---------------------------------------------------------------------------
# 模块级便捷入口（与 interview.py 的 get_interview_engine 对齐）
# ---------------------------------------------------------------------------
_engine_singleton: Optional[TuringScenarioEngine] = None


def get_validation_engine() -> TuringScenarioEngine:
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = TuringScenarioEngine()
    return _engine_singleton


def _all_profile_items(profile: CognitiveProfile) -> list:
    """画像里可被验证引用的全部条目（顺序与 generate_scenario 的候选收集一致）。"""
    if profile is None:
        return []
    return (
        list(profile.stances)
        + list(profile.argument_patterns)
        + list(profile.value_rankings)
        + list(profile.thinking_frameworks)
    )


def _restore_scenario(
    profile: CognitiveProfile,
    scenario_id: str,
    related_items: Optional[list[str]],
    scenario_text: str = "",
    domain: Optional[str] = None,
) -> Optional[dict]:
    """还原用户实际作答所针对的那个情境。

    评估阶段若重新走 ``generate_scenario``，会从当前画像里再挑一条高置信条目，
    于是「用户答的是 A，反哺改的却是 B」—— 置信度朝错误方向调，还会凭空生出
    「验证盲区」gap。所以前端把 generate 阶段拿到的情境回传，这里校验条目仍
    存在于画像后再复用。

    校验失败（条目已被清理 / 前端未回传）返回 None，由调用方回退到重新生成。
    """
    if not related_items:
        return None
    valid = {it.id: it for it in _all_profile_items(profile)}
    hits = [i for i in related_items if i in valid]
    if not hits:
        logger.info("validation scenario %s 引用的条目已不在画像中，回退重新生成", scenario_id)
        return None
    first = valid[hits[0]]
    topic = _topic_of_item(first)
    return {
        "id": scenario_id or uuid.uuid4().hex[:12],
        "domain": domain or topic or "general",
        "scenario": scenario_text or "",
        "question": "",
        "related_items": hits,
        "source_item": {
            "dim": "restored",
            "topic": topic,
            "content": _content_of_item(first),
            "confidence": getattr(first, "confidence", 0.0),
        },
    }


def _mark_validation_completed() -> None:
    """标记「真正完成过一次认知验证」。

    tick 弹出情境时写的是 ``last_validation_at``（用于三天节流），用户可能根本
    没作答。像我度的「已验证」状态必须落在评估反哺之后，否则永远显示已验证。
    """
    try:
        from deskmate.cognitive.store import CognitiveStore

        CognitiveStore().set_meta(
            "last_validation_completed_at", datetime.now().isoformat()
        )
    except Exception:
        logger.debug("mark validation completed failed", exc_info=True)


def run_validation(
    profile_path: Optional[str] = None,
    layer: str = "cognitive",
    domain: Optional[str] = None,
    ai_response: Optional[str] = None,
    user_response: Optional[str] = None,
    scenario_id: Optional[str] = None,
    related_items: Optional[list[str]] = None,
    scenario_text: str = "",
) -> dict:
    """
    便捷入口：生成情境 →（可选）评估 →（可选）反哺。

    ``scenario_id`` / ``related_items`` / ``scenario_text`` 由前端把 generate 阶段
    拿到的情境原样回传，评估时优先复用，保证反哺落在用户真正作答的那条画像上；
    缺失或失效时回退为重新生成（保持旧行为）。

    返回 {"scenario": {...}, "evaluation": {...}, "applied": bool, "note": str}
    """
    from deskmate.cognitive.profile import get_profile_manager

    pm = get_profile_manager(profile_path=profile_path, layer=layer)
    profile = pm.get()
    engine = get_validation_engine()

    if ai_response and user_response:
        # 评估阶段：优先还原用户作答所针对的情境，避免反哺张冠李戴。
        scenario = _restore_scenario(
            profile, scenario_id or "", related_items, scenario_text, domain
        )
        if scenario is None:
            scenario = engine.generate_scenario(profile, domain=domain)
    else:
        scenario = engine.generate_scenario(profile, domain=domain)
    if not scenario.get("id"):
        return {
            "scenario": {},
            "evaluation": {},
            "applied": False,
            "note": "画像条目不足（置信度>=0.7 的立场/价值观/框架太少），无法生成验证情境。",
        }

    result: dict[str, Any] = {"scenario": scenario}

    if ai_response and user_response:
        evaluation = engine.evaluate_similarity(
            profile, scenario["id"], ai_response, user_response,
            related_items=scenario.get("related_items", []),
        )
        evaluation["related_items"] = scenario.get("related_items", [])
        evaluation["scenario"] = scenario.get("scenario", "")
        engine.apply_feedback(profile, evaluation)
        pm.save()
        _mark_validation_completed()
        result["evaluation"] = evaluation
        result["applied"] = True
        result["note"] = "验证完成，已反哺画像。"
    else:
        result["evaluation"] = {}
        result["applied"] = False
        result["note"] = "已生成情境，等待 AI 回答与你的回答后评估。"

    return result
