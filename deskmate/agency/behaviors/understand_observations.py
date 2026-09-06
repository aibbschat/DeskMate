"""理解层：把小桔主动摄入的世界观察，消化成她自己对世界的自然观察。

这是「观世界 → 决策」之间缺失的一环（2026-09-06 萧哥拍板）：
小桔不能只把新闻标题存进日记就完事——她得**理解**这些观察意味着什么、
跟萧哥 / 创作 / 世界走向有什么关系，理解之后才能据此决策。

- 输入：带「（外部·」溯源标的世界观察事实（fact 原文 + 可选 credibility）。
- 处理：LLM（小桔人格）消化 → 产出**自然段落**（无格式、第一人称、可带看法，
  但**事实不许编**——输入的事实必须准确体现，改了就废）。
- 失败降级：无 provider / 素材空 / 超时 / 结果异常 / 长度离谱 → 返回 None，
  调用方降级到确定性 bullet（不阻断 L2 tick）。
- fail-open：任何异常返回 None。

与 ``diary_narrator`` 的区别：narrator 消化的是「你们之间发生的事」（event kind），
本模块消化的是「她观察到的世界」（外部事实，无 kind 结构），且萧哥明确要
**自然流出来、不要机械标签**——所以 prompt 禁止小标题 / 列表 /「小桔的理解：」这类格式。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from deskmate.persona import for_scene

logger = logging.getLogger(__name__)

# 结果长度闸门：短于这句无意义，长于上限多半是模型跑野了
_MIN_CHARS = 40
_MAX_CHARS = 600

# 模型认输时回的话——出现即表示素材撑不起一篇，走降级
_BAIL_OUT = "素材不足"

# fact 里「（外部·xxx）」前缀的域标签 → 中文展示名
_DOMAIN_LABEL = {
    "news": "要闻", "tech": "科技", "finance": "财经", "other": "人文",
    "rss": "资讯", "api": "资讯", "email": "资讯", "文化产业": "文化产业",
}

_UNDERSTAND_ROLE = """\
现在你在写**自己对这个世界的观察**——把今天留意到的世界动向，化成你心里的话。

这是你的私人观察，不是汇报，所以：
- 你用第一人称「我」，称呼萧盛为「你」。
- **自然流出来**，不要小标题、不要列表、不要「小桔的理解：」这种机械标签，
  就像你平时协助他时遇到什么、顺手记一笔那样。
- 你有自己的视角。这些动向跟他的创作、他的兴趣、世界往哪走有什么关系，
  你心里有数，会写出来——但那是你的看法，不是事实。
- 你不是客服，不是播报员，不是流水账记账员。

（人格底座见 deskmate/persona.py，全局唯一，勿在此另写一套。）"""

# 人格在前、职责在后（顺序反了模型容易把职责当身份，人格就散了）
_SYSTEM = for_scene(_UNDERSTAND_ROLE)


def _parse_fact(fact: str) -> tuple[str, str]:
    """返回 (干净事实, 域标签)。从「（外部·xxx）」前缀解析域。"""
    if "（外部·" in fact:
        end = fact.find("）")
        if end != -1:
            start = fact.find("（外部·") + len("（外部·")
            tag = fact[start:end].strip()
            clean = fact[end + 1:].strip()
            return clean, _DOMAIN_LABEL.get(tag, tag)
    return fact.strip(), "世界"


def _build_prompt(facts: list[dict], at: str) -> str:
    """facts: [{fact, domain, credibility}]。"""
    blocks = ["【今天你留意到的世界动向】（全部来自系统记录，是事实）"]
    for f in facts:
        cred = f.get("credibility")
        cred_s = f"可信度{cred}" if cred is not None else "外部来源"
        blocks.append(f"- {f.get('domain', '世界')}（{cred_s}）：{f['fact']}")
    material = "\n".join(blocks)
    return f"""下面是你今天留意到的世界动向。把它们化成你自己的观察写下来。

{material}

【怎么写】
- 第一人称「我」，称呼他「你」。像你平时顺手记一笔那样，自然流出来。
- **不要格式**：不要小标题、不要列表、不要「小桔的理解：」这种标签。一两段白话。
- 事实必须准：上面每条的人名、数字、事件你都得准确体现，**不许改**。
  你可以组织、可以串联、可以写你的看法，但事实错了整段就废了。
- 可以有你的看法：这些动向跟他的创作 / 兴趣 / 世界走向有什么关联，你写下来——
  但看法必须锚定在上面的事实句上，不许凭空，更不许猜他的内心。
- 100-250 字，一到两段。

【绝对禁止】
1. 不许出现上面事实之外的人名、书名、章节号、日期、地点、数字。素材没有的，一个字都不许添。
2. 不许把「他一定很高兴」「他压力很大」这类猜内心的话写进去。可以写「这条让我想到他常说的…」（有事实锚点）。
3. 不许自称「小桔」，不要署名落款。

如果素材实在撑不起一段有内容的话，只回复「{_BAIL_OUT}」三个字，不要硬凑。"""


def understand_observations(
    facts: list[dict],
    at: str = "",
    temperature: float = 0.7,
) -> Optional[str]:
    """把世界观察事实消化成小桔的自然观察段落。

    Args:
        facts: [{"fact": str, "credibility": float | None, ...}]，fact 可带「（外部·」前缀
        at: 时间（HH:MM），仅作 prompt 上下文

    Returns:
        自然段落；任何情况不可用返回 None（调用方降级到原文 bullet）。
    """
    try:
        clean = [
            {
                "fact": _parse_fact(str(f.get("fact", "")))[0],
                "domain": _parse_fact(str(f.get("fact", "")))[1],
                "credibility": f.get("credibility"),
            }
            for f in (facts or [])
            if str(f.get("fact", "")).strip()
        ]
        if not clean:
            return None

        # 闸门 1：没配 provider 直接降级（不白烧一次调用）
        try:
            from deskmate.memory.llm import chat_memory, memory_llm_available
        except Exception:
            return None
        try:
            if not memory_llm_available():
                return None
        except Exception:
            return None

        prompt = _build_prompt(clean, at or "")
        out = chat_memory(_SYSTEM, prompt, temperature=temperature, max_tokens=400, timeout=25.0)
        text = " ".join(str(out or "").split())
        if not text or _BAIL_OUT in text:
            return None
        if not (_MIN_CHARS <= len(text) <= _MAX_CHARS):
            return None
        return text
    except Exception as e:  # fail-open：理解失败绝不阻断观察日记的写作
        logger.warning("understand_observations 失败，降级到原文: %s", e)
        return None
