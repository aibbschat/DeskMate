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
                _, op = self.profile_manager.add_stance(
                    topic=s.get("topic", ""),
                    content=s.get("content", ""),
                    source=source,
                    tags=s.get("tags", []),
                )
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
                _, op = self.profile_manager.add_argument_pattern(
                    content=p.get("content", ""),
                    source=source,
                    pattern_type=p.get("pattern_type", ""),
                    evidence_preference=p.get("evidence_preference", ""),
                    tags=p.get("tags", []),
                )
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
                _, op = self.profile_manager.add_value_ranking(
                    domain=v.get("domain", ""),
                    ranking=v.get("ranking", []),
                    content=v.get("content", ""),
                    source=source,
                    tradeoff_notes=v.get("tradeoff_notes", ""),
                    tags=v.get("tags", []),
                )
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
                _, op = self.profile_manager.add_thinking_framework(
                    domain=f.get("domain", ""),
                    steps=f.get("steps", []),
                    content=f.get("content", ""),
                    source=source,
                    tags=f.get("tags", []),
                )
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
