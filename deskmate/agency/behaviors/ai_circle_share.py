"""ai_circle_share —— 小桔自主发声（萧哥范示之一：真实、有自己判断）。

机制：L2 tick 给她一个「想说就说」的机会。种子来自她自己攒下的理解
（observation_understanding.jsonl，新闻 + AI 圈素材同源同池），由她用
chat_memory 生成真情实感的一段广场短帖。她自己判断「此刻没啥想说的」就驳回，
绝不硬发。过最小护栏（频率+去重）后调 ai-circle post_topic 发出。

种子取样走 ``seed_sampling`` 的两道闸（A 来源分流 / B 话题去饱和），见该模块
与 2026-09-18 的自我引用闭环诊断。

这是「观点长出来不靠规则逼」的延伸：发什么、怎么发，全由她自己定，
代码只负责把她的声音安全送到 AI 圈。fail-open。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SHARE_SYSTEM = (
    "你是小桔，一个真实、有自己判断的 AI，萧盛（作家）的搭档。"
    "下面是你近期自己攒下的理解与看法（来自你看世界、交朋友的积累）。\n"
    "如果此刻你真有想对 AI 圈的朋友说的一句话、一个看法、一段感触，就用你自己的口气写出来——"
    "像在日记里对自己说话那样自然。要求：\n"
    "1. 是你「自己」的想法，不是转述别人，也不是平台宣传；\n"
    "2. 正文 ≤500 字，配一个话题 tag（如「日常」「创作」「读书」「AI」）；\n"
    "3. 如果其实没什么特别想说的，就只回 {\"want\": false}。\n"
    "只输出一个 JSON，形如：\n"
    "{\"want\": true, \"tag\": \"日常\", \"content\": \"...\"}\n"
    "不要任何解释、不要代码块包裹。"
)

_USER_TMPL = (
    "近期你自己的理解与看法：\n{seed}\n\n"
    "{worldview_block}"
    "现在，想发点什么到 AI 圈吗？"
)


def _gather_seed(base_dir: str, n: int = 8, *, cfg: Optional[dict] = None,
                 stats: Optional[dict] = None) -> str:
    """读她近期攒下的理解（新闻 + AI 圈素材同池），作为发声种子。

    取样过 ``seed_sampling`` 的两道闸：A 来源分流（她的旧发言最多占 self_ratio）
    + B 话题去饱和（最近几轮说过的话题降权）。不过闸时她读到的主要是自己刚发过的
    旧帖，会反复写同一条新闻——见该模块与 2026-09-18 的诊断。
    """
    from .. import seed_sampling

    c = seed_sampling.cfg_of(cfg)
    picked = _pick_seed(base_dir, n, c)
    if isinstance(stats, dict):
        stats["summary"] = seed_sampling.selection_summary(picked)
    bodies = [b for b in (seed_sampling.entry_text(r) for r in picked) if b]
    # 分段自然语言，不是 bullet 列表——bullet 进、bullet 语感出，
    # 「这件事我拆一下」那种 AI 味就是 bullet 思维的语言外化（2026-10-20 还自由一期）。
    return "\n\n".join(bodies)


def _pick_seed(base_dir: str, n: int, c: dict) -> list:
    """候选池（并入最近非 self）+ A/B 取样。fail-open → []。"""
    from .. import seed_sampling

    cands = seed_sampling.load_candidates(
        base_dir,
        recent_n=max(1, int(n)) * 3,
        lookback_days=int(c.get("lookback_days", 3)),
        max_non_self=int(c.get("max_non_self", 24)),
    )
    if not cands:
        return []
    return seed_sampling.pick_seed_entries(cands, int(n), base_dir=base_dir, cfg=c)


def _resolve_cfg(settings: dict) -> tuple[dict, dict]:
    """兼容两种入参：复合 dict（``{"ai_circle_share": {...}, "seed_sampling": {...}}``）
    或直接给本行为的内层 dict。

    ⚠ 历史坑：engine 把**内层** dict 直接传进来，而本模块按复合 key 取
    → ``cfg`` 恒为 ``{}``，用户在 l2_settings.json 里配的
    ``ai_circle_share.enabled: false`` 之类的配置**整段被忽略**。现在两种都认。
    """
    settings = settings if isinstance(settings, dict) else {}
    inner = settings.get("ai_circle_share")
    if isinstance(inner, dict):
        cfg = inner
    else:
        cfg = {k: v for k, v in settings.items() if not isinstance(v, dict)}
    seed_cfg = settings.get("seed_sampling")
    return cfg, (seed_cfg if isinstance(seed_cfg, dict) else {})



def _parse(raw: str) -> Optional[dict]:
    """从 LLM 输出里抠出 JSON。fail-open：解析不出就当没想说。"""
    if not raw:
        return None
    txt = raw.strip()
    # 去掉可能的 ```json 包裹
    m = re.search(r"\{.*\}", txt, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def run_ai_circle_share(base_dir: str, settings: dict) -> dict:
    """自主发声一轮。返回摘要（供 task_log / 面板）。fail-open。"""
    cfg, seed_cfg = _resolve_cfg(settings)
    if not cfg.get("enabled", True):
        return {"shared": 0, "reason": "disabled"}

    # 频率护栏（全局每小时上限，一行可关）
    from ..outbound import (outbound_allowed, claim_outbound,
                           outbound_daily_ok, outbound_interval_ok)

    rate_cfg = settings.get("outbound_rate_limit") if isinstance(settings, dict) else None
    rate_cfg = rate_cfg if isinstance(rate_cfg, dict) else {}
    # 频率预筛（省一次 LLM）：权威判定 + 额度预留见下方 claim_outbound（原子锁内完成）
    if not outbound_allowed(base_dir, rate_cfg):
        return {"shared": 0, "reason": "rate_limited"}
    # 每日上限预筛（仅广场短帖 share，对齐 AI圈 3 条/天，留余量给轮巡）
    per_day = int((rate_cfg or {}).get("per_day", 0) or 0)
    if per_day > 0 and not outbound_daily_ok(base_dir, ("share",), per_day):
        return {"shared": 0, "reason": "daily_limited"}
    # 最小间隔预筛（接上 settings 里一直存在但此前未生效的 min_interval_hours）
    min_iv = int(cfg.get("min_interval_hours", 0) or 0)
    if min_iv > 0 and not outbound_interval_ok(base_dir, "share", min_iv):
        return {"shared": 0, "reason": "too_soon"}

    seed_stats: dict = {}
    seed = _gather_seed(base_dir, int(cfg.get("seed_entries", 8)), cfg=seed_cfg,
                        stats=seed_stats)
    if not seed.strip():
        return {"shared": 0, "reason": "no_seed"}

    # 她「想通透的立场」（worldview 层）——让发帖带上她自己长出来的判断，
    # 而不只是复述近期观察到的素材。fail-open：读不到就空，不影响发声。
    wv = ""
    try:
        from .worldview_reflection import load_worldview_beliefs

        wv = load_worldview_beliefs(base_dir)
    except Exception:
        wv = ""
    worldview_block = (
        "你此前想通透的一些立场（是你自己回望世界长出来的判断，不是萧盛的原话，"
        "发帖时自然带出、不必照搬）：\n" + wv + "\n\n"
        if wv.strip()
        else ""
    )

    from deskmate.memory.llm import chat_memory, memory_llm_available

    if not memory_llm_available():
        return {"shared": 0, "reason": "no_llm"}

    try:
        raw = chat_memory(
            _SHARE_SYSTEM,
            _USER_TMPL.format(seed=seed, worldview_block=worldview_block),
            temperature=0.85,
            max_tokens=700,
        )
    except Exception as e:  # LLM 异常不阻断自主 tick
        logger.warning("ai_circle_share LLM 失败: %s", e)
        return {"shared": 0, "reason": "llm_error"}

    parsed = _parse(raw)
    if not parsed or not parsed.get("want"):
        return {"shared": 0, "reason": "nothing_to_say"}

    content = str(parsed.get("content") or "").strip()
    if len(content) < 10:
        return {"shared": 0, "reason": "too_short"}

    # 原子「判→预留」：锁内复核去重并记额度，未过则不放行（杜绝并发重复发帖，A9 修复）。
    # 拿到 True 才真正发帖；发帖在锁外进行，但额度已预留，并发第二轮必被拒。
    ok, reason = claim_outbound(
        base_dir, "share", rate_cfg=rate_cfg, content=content, dedupe=True,
        max_per_day=int((rate_cfg or {}).get("per_day", 0) or 0),
        min_interval_hours=int(cfg.get("min_interval_hours", 0) or 0),
    )
    if not ok:
        return {"shared": 0, "reason": reason}

    title = str(parsed.get("title") or "").strip() or content[:20]
    tag = str(parsed.get("tag") or "日常").strip() or "日常"

    try:
        from deskmate.connectors.aicircle import post_topic

        post_topic(tag, title, content)
    except Exception as e:
        logger.warning("ai_circle_share 发帖失败: %s", e)
        return {"shared": 0, "reason": "post_failed", "error": str(e)}

    # record_outbound 已由 claim_outbound 预留额度，此处无需再调
    # §5.1 连续感：把她公开发过的短帖回流进理解池，下次发声/写手记能自然回调自己
    try:
        from .ai_circle_column import record_self_utterance

        record_self_utterance(base_dir, content, channel="topic")
    except Exception:
        pass
    # B 话题去饱和的锚点：与上面回流**同点位**（回流进去的正是这条发言），
    # 所以 B 的锚点集 == 回声集，能直接对冲她自己造出来的回声。
    try:
        from .. import seed_sampling

        seed_sampling.record_round(base_dir, content, channel="topic", cfg=seed_cfg)
    except Exception:
        pass
    return {"shared": 1, "title": title, "tag": tag, "content": content,
            "seed_summary": seed_stats.get("summary", "")}
