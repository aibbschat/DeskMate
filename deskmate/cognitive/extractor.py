"""
认知提取管线——从文本/对话中多层解析认知特征，增量更新认知画像。

核心设计：
- 多层解析：内容→观点→论证→价值观→思维模式，一次 LLM 调用完成
- 结构化输出：LLM 返回 JSON，解析为五个维度的认知特征
- 增量更新：提取的特征通过 CognitiveProfileManager 增量更新到画像
- 容错处理：LLM 返回格式错误时降级处理，不崩溃

提取质量取决于 prompt 设计和文本质量。冷启动阶段从已有数据提取，
后续每日读书任务读完一本书后调用本管线，认知画像会逐步增厚。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict
from typing import Optional

from .models import (
    CognitiveProfile,
    Stance,
    ArgumentPattern,
    ValueRanking,
    ThinkingFramework,
    SourceRef,
)
from .profile import CognitiveProfileManager, get_profile_manager

logger = logging.getLogger(__name__)


# 提取 prompt——精心设计，要求 LLM 从文本中提取五个维度的认知特征
EXTRACTION_SYSTEM_PROMPT = """你是一个「认知分析师」，专门从作者的文字作品中提取作者的深层认知特征。

你的任务不是总结内容，而是理解「作者是怎么想的」。你要从文字的字里行间推断：
- 作者对什么问题持有什么立场（不是写了什么，而是作者相信什么）
- 作者习惯怎么分析问题、怎么论证（用什么逻辑、什么证据、什么修辞）
- 作者的价值观排序（在冲突情境下，作者认为什么更重要）
- 作者的思维框架（分析问题的习惯性路径：先看什么、再看什么、最后看什么）

重要原则：
1. 只提取有文本证据支持的认知特征，不要凭空推测
2. 区分「作品中人物的观点」和「作者本人的立场」——只有通过叙事选择、人物命运安排、叙述者语气能推断出的作者立场才提取
3. 每个特征都要附上文本证据（原文片段），支持溯源
4. 置信度要诚实：证据充分的给高分，只有间接证据的给低分
5. 不要提取内容摘要（写了什么故事），只提取认知特征（作者怎么想）

输出严格的 JSON 格式，不要输出任何其他文字：
{
  "stances": [
    {"topic": "议题（如：历史真实性/创作主题/人物塑造）", "content": "作者的立场", "evidence": "原文证据片段", "confidence": 0.5, "tags": ["标签"]}
  ],
  "argument_patterns": [
    {"content": "论证模式描述", "pattern_type": "历史类比/制度推演/反证/归纳/其他", "evidence_preference": "证据偏好", "evidence": "原文证据", "confidence": 0.5, "tags": []}
  ],
  "value_rankings": [
    {"domain": "领域（创作/历史/技术/生活）", "ranking": ["优先级1", "优先级2", "优先级3"], "content": "价值观描述", "tradeoff_notes": "权衡说明", "evidence": "原文证据", "confidence": 0.5, "tags": []}
  ],
  "thinking_frameworks": [
    {"domain": "适用领域", "steps": ["步骤1", "步骤2", "步骤3"], "content": "思维框架描述", "evidence": "原文证据", "confidence": 0.5, "tags": []}
  ]
}

如果某个维度没有足够证据，返回空数组。不要编造。"""


def _extract_json(text: str) -> dict:
    """从 LLM 返回的文本中提取 JSON。容错处理各种格式问题。"""
    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试提取第一个 { 到最后一个 }
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # 尝试修复常见的 JSON 格式问题（尾部逗号、注释等）
    try:
        # 移除 // 注释
        cleaned = re.sub(r"//.*$", "", text, flags=re.MULTILINE)
        # 移除对象/数组尾部逗号
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    logger.warning(f"Failed to parse JSON from LLM response: {text[:200]}")
    return {}


# ---------------------------------------------------------------------------
# P1 汇流：共享锚抽取（轻量、无 LLM、fail-open）
# ---------------------------------------------------------------------------
# 条目的「议题」字段：立场看 topic，价值排序 / 思维框架看 domain。
_ANCHOR_TOPIC_FIELDS = ("topic", "domain")


def _person_names_from_config() -> list[str]:
    """已知人名源（逗号分隔）。仓库暂无 known-names 表，先用环境变量兜底；
    未配置 = 不产出人名锚（best-effort：识别不出就跳过）。"""
    raw = os.getenv("DESKMATE_PERSON_NAMES", "")
    return [n.strip() for n in raw.split(",") if n.strip()]


def extract_anchors(
    text: str,
    source_title: str,
    source_type: str = "",
    extracted_items: Optional[list] = None,
) -> list[tuple[str, str]]:
    """从一次抽取产出里派生「共享锚」（P1：topic / book / person）。

    - topic：取条目自身的议题字段（立场 topic / 价值排序与思维框架 domain）；
    - book：source_type == 'book' → 以书名作锚（同书条目自动成邻座）；
    - person：best-effort，仅命中配置的已知人名才产出（无名单则跳过）。

    轻量、无 LLM；异常一律吞掉（fail-open），返回已去重、已滤空值的 [(kind, value)]。
    """
    anchors: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(kind: str, value: str) -> None:
        value = (value or "").strip()
        if not kind or not value:
            return
        key = (kind, value)
        if key in seen:
            return
        seen.add(key)
        anchors.append(key)

    try:
        for it in extracted_items or []:
            if not isinstance(it, dict):
                continue
            for fld in _ANCHOR_TOPIC_FIELDS:
                v = it.get(fld)
                if isinstance(v, str) and v.strip():
                    _add("topic", v)
                    break
        if source_type == "book" and (source_title or "").strip():
            _add("book", source_title)
        for name in _person_names_from_config():
            if name in (text or ""):
                _add("person", name)
    except Exception as e:  # noqa: BLE001 —— fail-open：抽锚失败不影响主抽取
        logger.warning("extract_anchors 失败（忽略，不影响主抽取）: %s", e)
    return anchors


def _attach_anchors(
    pm: Optional[CognitiveProfileManager],
    item_id: str,
    text: str,
    source_title: str,
    source_type: str,
    item_dict: dict,
) -> None:
    """把条目锚写进 memory_anchors（P1 只写 profile_item，即 recall_items 查询的表）。

    句柄取自 pm.store（CognitiveProfileManager 已暴露 CognitiveStore，profile.py:58）。
    fail-open：任何异常仅告警、绝不抛——挂锚失败不影响主抽取主链路。
    """
    try:
        store = getattr(pm, "store", None)
        if store is None or not item_id:
            return
        anchors = extract_anchors(text, source_title, source_type, [item_dict])
        if anchors:
            store.upsert_anchors("profile_item", item_id, anchors)
    except Exception as e:  # noqa: BLE001 —— fail-open：挂锚失败不影响主抽取
        logger.warning("挂锚失败（忽略，不影响主抽取）: %s", e)


class CognitiveExtractor:
    """认知提取器——从文本中提取认知特征，增量更新画像。"""

    def __init__(self, profile_manager: Optional[CognitiveProfileManager] = None):
        self.profile_manager = profile_manager or get_profile_manager()

    def extract_from_text(
        self,
        text: str,
        source_title: str,
        source_type: str = "book",
        source_date: str = "",
        max_chars: int = 8000,
    ) -> dict:
        """
        从一段文本中提取认知特征，增量更新到认知画像。

        Args:
            text: 文本内容（作品正文/对话记录/文章）
            source_title: 来源标题（书名/对话日期/文章名）
            source_type: 来源类型（book/conversation/article/vault/memory）
            source_date: 来源日期
            max_chars: 最大处理字符数（超出截断，避免 token 超限）

        Returns:
            提取结果统计：{"stances": n, "argument_patterns": n, "value_rankings": n, "thinking_frameworks": n, "operations": [...]}
        """
        from deskmate.memory.llm import chat, llm_available

        if not llm_available():
            logger.warning("LLM not available, skipping cognitive extraction")
            return {"error": "llm_not_available"}

        # 截断文本
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...（文本过长，已截断）"

        # 构建 user prompt
        user_prompt = f"""请从以下文字中提取作者的认知特征。

来源：{source_title}
类型：{source_type}
日期：{source_date or "未知"}

文字内容：
---
{text}
---

请严格按照系统提示中的 JSON 格式输出认知特征。只输出 JSON，不要输出其他文字。"""

        try:
            response = chat(
                system=EXTRACTION_SYSTEM_PROMPT,
                user=user_prompt,
                temperature=0.2,
                max_tokens=2000,
                timeout=300.0,
            )
        except Exception as e:
            logger.error(f"Cognitive extraction LLM call failed: {e}")
            return {"error": f"llm_call_failed: {e}"}

        # 解析 JSON
        data = _extract_json(response)
        if not data:
            return {"error": "json_parse_failed", "raw_response": response[:500]}

        # 增量更新到画像
        operations = []
        stats = {"stances": 0, "argument_patterns": 0, "value_rankings": 0, "thinking_frameworks": 0}

        # 立场
        for s in data.get("stances", []):
            source = SourceRef(
                type=source_type,
                title=source_title,
                excerpt=s.get("evidence", "")[:300],
                date=source_date,
                confidence=s.get("confidence", 0.5),
            )
            try:
                stance, op = self.profile_manager.add_stance(
                    topic=s.get("topic", ""),
                    content=s.get("content", ""),
                    source=source,
                    tags=s.get("tags", []),
                )
                _attach_anchors(self.profile_manager, stance.id, text, source_title, source_type, s)
                operations.append({"type": "stance", "topic": s.get("topic"), "operation": op})
                stats["stances"] += 1
            except Exception as e:
                logger.error(f"Failed to add stance: {e}")

        # 论证模式
        for p in data.get("argument_patterns", []):
            source = SourceRef(
                type=source_type,
                title=source_title,
                excerpt=p.get("evidence", "")[:300],
                date=source_date,
                confidence=p.get("confidence", 0.5),
            )
            try:
                pattern, op = self.profile_manager.add_argument_pattern(
                    content=p.get("content", ""),
                    source=source,
                    pattern_type=p.get("pattern_type", ""),
                    evidence_preference=p.get("evidence_preference", ""),
                    tags=p.get("tags", []),
                )
                _attach_anchors(self.profile_manager, pattern.id, text, source_title, source_type, p)
                operations.append({"type": "argument_pattern", "operation": op})
                stats["argument_patterns"] += 1
            except Exception as e:
                logger.error(f"Failed to add argument pattern: {e}")

        # 价值观排序
        for v in data.get("value_rankings", []):
            source = SourceRef(
                type=source_type,
                title=source_title,
                excerpt=v.get("evidence", "")[:300],
                date=source_date,
                confidence=v.get("confidence", 0.5),
            )
            try:
                vr, op = self.profile_manager.add_value_ranking(
                    domain=v.get("domain", ""),
                    ranking=v.get("ranking", []),
                    content=v.get("content", ""),
                    source=source,
                    tradeoff_notes=v.get("tradeoff_notes", ""),
                    tags=v.get("tags", []),
                )
                _attach_anchors(self.profile_manager, vr.id, text, source_title, source_type, v)
                operations.append({"type": "value_ranking", "domain": v.get("domain"), "operation": op})
                stats["value_rankings"] += 1
            except Exception as e:
                logger.error(f"Failed to add value ranking: {e}")

        # 思维框架
        for f in data.get("thinking_frameworks", []):
            source = SourceRef(
                type=source_type,
                title=source_title,
                excerpt=f.get("evidence", "")[:300],
                date=source_date,
                confidence=f.get("confidence", 0.5),
            )
            try:
                tf, op = self.profile_manager.add_thinking_framework(
                    domain=f.get("domain", ""),
                    steps=f.get("steps", []),
                    content=f.get("content", ""),
                    source=source,
                    tags=f.get("tags", []),
                )
                _attach_anchors(self.profile_manager, tf.id, text, source_title, source_type, f)
                operations.append({"type": "thinking_framework", "domain": f.get("domain"), "operation": op})
                stats["thinking_frameworks"] += 1
            except Exception as e:
                logger.error(f"Failed to add thinking framework: {e}")

        # 更新画像摘要
        self._update_summary()

        return {**stats, "operations": operations}

    def _update_summary(self):
        """更新画像的一句话总结。"""
        profile = self.profile_manager.get()
        active_stances = profile.active_stances()
        active_values = profile.active_values()

        parts = []
        if active_stances:
            topics = list(set(s.topic for s in active_stances if s.topic))[:3]
            parts.append(f"在{', '.join(topics)}等问题上有明确立场")
        if active_values:
            domains = list(set(v.domain for v in active_values if v.domain))[:2]
            parts.append(f"在{', '.join(domains)}领域有清晰的价值观排序")
        if profile.open_gaps():
            parts.append(f"还有{len(profile.open_gaps())}个认知盲区待澄清")

        if parts:
            profile.summary = "；".join(parts) + "。"
            self.profile_manager.save()


# 全局单例
_default_extractor: Optional[CognitiveExtractor] = None


def get_extractor(profile_manager: Optional[CognitiveProfileManager] = None) -> CognitiveExtractor:
    global _default_extractor
    if _default_extractor is None:
        _default_extractor = CognitiveExtractor(profile_manager)
    return _default_extractor


def _all_dim_items(profile: "CognitiveProfile") -> list:
    """画像四个维度的全部条目（供查重）。"""
    return (
        list(profile.stances)
        + list(profile.argument_patterns)
        + list(profile.value_rankings)
        + list(profile.thinking_frameworks)
    )


# ---------------------------------------------------------------------------
# 交互式认知提取（验证 / 访谈回答）——用户亲口说的，抽四维度带 contexts
# ---------------------------------------------------------------------------
RESPONSE_EXTRACTION_SYSTEM_PROMPT = """你是一个「认知特征提取器」，从用户亲口说的话里提取 ta 的认知特征。
与作品分析不同：这里用户是直接表达，所以放心提取「用户本人相信什么」，不必推断。

提取四个维度（有证据才提，没有就返回空数组）：
- stances：明确立场（topic=议题, content=立场描述）
- argument_patterns：论证习惯（content, pattern_type 如 历史类比/制度推演/反证/归纳/举例, evidence_preference 证据偏好）
- value_rankings：价值观排序（domain=领域, ranking=优先级列表, content, tradeoff_notes=什么情况可妥协）
- thinking_frameworks：思维框架（domain=领域, steps=分析步骤列表, content）

关键要求——每条必须带 contexts（适用情境 / 触发条件）：
- 如果用户的话显式或隐含了「在什么情况下 / 对什么人 / 什么利害关系下」才成立，写进 contexts
  （如 ["创作圈"]、["学术圈"]、["面对家人"]、["出版取舍"]）
- 如果看不出特定情境、属普适立场，contexts 给空数组 []
- 容纳矛盾：用户在不同情境有不同取向是正常的，分别写不同条目、各带各自 contexts，不要强行统一

只输出严格 JSON，不要其他文字：
{
  "stances": [{"topic":"","content":"","contexts":[],"confidence":0.6}],
  "argument_patterns": [{"content":"","pattern_type":"","evidence_preference":"","contexts":[],"confidence":0.6}],
  "value_rankings": [{"domain":"","ranking":[],"content":"","tradeoff_notes":"","contexts":[],"confidence":0.6}],
  "thinking_frameworks": [{"domain":"","steps":[],"content":"","contexts":[],"confidence":0.6}]
}"""


def extract_cognitive_items(
    text: str,
    source_type: str = "validation",
    source_title: str = "认知验证",
    source_date: str = "",
    context_hint: str = "",
    pm: Optional[CognitiveProfileManager] = None,
    mode: str = "new_only",
    max_items: int = 4,
) -> dict:
    """从用户亲口表达的文字里抽取认知特征，增量落到四维度（带 contexts）。

    - mode="new_only"：只落「画像里没有同 key 的」新条目（验证答对即沉淀用，避免重复灌噪声）
    - mode="merge"：允许对已有同 key 条目补充来源 / 深化（访谈消化 gap 用）
    - fail-open：LLM 不可用 / 解析失败 → 返回 {"deposited":0,"skipped":0,"error":...}
    """
    from deskmate.memory.llm import chat, llm_available

    if pm is None:
        pm = get_profile_manager()
    if not text or not text.strip():
        return {"deposited": 0, "skipped": 0, "error": "empty_text"}
    if not llm_available():
        return {"deposited": 0, "skipped": 0, "error": "llm_not_available"}

    user_prompt = (
        f"【情境提示】{context_hint}\n\n"
        f"【用户亲口说的话】\n{text}\n\n"
        "请提取该用户的认知特征，严格按系统提示的 JSON 格式输出。"
    )
    try:
        response = chat(
            system=RESPONSE_EXTRACTION_SYSTEM_PROMPT,
            user=user_prompt,
            temperature=0.2,
            max_tokens=1500,
            timeout=120.0,
        )
    except Exception as e:
        logger.warning("extract_cognitive_items LLM 失败: %s", e)
        return {"deposited": 0, "skipped": 0, "error": f"llm_call_failed: {e}"}

    data = _extract_json(response)
    if not data:
        return {"deposited": 0, "skipped": 0, "error": "json_parse_failed"}

    profile = pm.profile
    deposited = skipped = 0

    def _has_active(key_fn, key):
        if mode != "new_only" or not key:
            return False
        for it in _all_dim_items(profile):
            if getattr(it, "status", "active") == "active" and key_fn(it) == key:
                return True
        return False

    # 立场
    for s in data.get("stances", [])[:max_items]:
        topic = (s.get("topic") or "").strip()
        content = (s.get("content") or "").strip()
        if not content:
            continue
        if _has_active(lambda it: getattr(it, "topic", ""), topic):
            skipped += 1
            continue
        src = SourceRef(type=source_type, title=source_title, excerpt=content[:120],
                        date=source_date, confidence=0.6)
        obj, _op = pm.add_stance(topic=topic, content=content, source=src,
                                 tags=[source_type], contexts=s.get("contexts") or [])
        _attach_anchors(pm, obj.id, text, source_title, source_type, s)
        deposited += 1

    # 论证模式
    for p in data.get("argument_patterns", [])[:max_items]:
        content = (p.get("content") or "").strip()
        if not content:
            continue
        if _has_active(lambda it: it.content[:20], content[:20]):
            skipped += 1
            continue
        src = SourceRef(type=source_type, title=source_title, excerpt=content[:120],
                        date=source_date, confidence=0.6)
        obj, _op = pm.add_argument_pattern(content=content, source=src,
                                          pattern_type=p.get("pattern_type", ""),
                                          evidence_preference=p.get("evidence_preference", ""),
                                          tags=[source_type], contexts=p.get("contexts") or [])
        _attach_anchors(pm, obj.id, text, source_title, source_type, p)
        deposited += 1

    # 价值观排序
    for v in data.get("value_rankings", [])[:max_items]:
        domain = (v.get("domain") or "").strip()
        content = (v.get("content") or "").strip()
        if not domain and not content:
            continue
        if _has_active(lambda it: getattr(it, "domain", ""), domain):
            skipped += 1
            continue
        src = SourceRef(type=source_type, title=source_title, excerpt=content[:120],
                        date=source_date, confidence=0.6)
        obj, _op = pm.add_value_ranking(domain=domain, ranking=v.get("ranking", []), content=content,
                                        source=src, tradeoff_notes=v.get("tradeoff_notes", ""),
                                        tags=[source_type], contexts=v.get("contexts") or [])
        _attach_anchors(pm, obj.id, text, source_title, source_type, v)
        deposited += 1

    # 思维框架
    for f in data.get("thinking_frameworks", [])[:max_items]:
        domain = (f.get("domain") or "").strip()
        content = (f.get("content") or "").strip()
        if not domain and not content:
            continue
        if _has_active(lambda it: getattr(it, "domain", ""), domain):
            skipped += 1
            continue
        src = SourceRef(type=source_type, title=source_title, excerpt=content[:120],
                        date=source_date, confidence=0.6)
        obj, _op = pm.add_thinking_framework(domain=domain, steps=f.get("steps", []), content=content,
                                             source=src, tags=[source_type], contexts=f.get("contexts") or [])
        _attach_anchors(pm, obj.id, text, source_title, source_type, f)
        deposited += 1

    if deposited:
        pm.save()
    return {"deposited": deposited, "skipped": skipped}
