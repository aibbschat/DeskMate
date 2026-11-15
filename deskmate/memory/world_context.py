"""把小桔「看世界」得来的观察回灌进对话 / 主动推理上下文。

背景（2026-09-06 复盘）：world_ingestion（豆包搜索）与 digest（AI 圈网文观察日记）
都把外部观察写进了 learned_history（带「（外部·」溯源标）。但全代码库里
``LearnedStore.relevant()`` / ``active_recent()`` 除了单元测试、日记素材（``diary_inspiration``），
**聊天与主动决策路径一条都没调用**——她"看"了，但既不会在聊天里主动提及，也不能据此决策。
本模块就是那条缺失的"回灌"管：让「她观察到的世界」从只落库、无人消费，
变成聊天与主动推理可读的依据。

2026-09-06 萧哥追加「理解层」：除了**事实**（她观察到的世界），还回灌**她的理解**
（LLM 消化观察后的判断，见 ``agency/behaviors/understand_observations.py``，落
``agency/observation_understanding.jsonl``）。事实是可信依据，理解是参考视角——
决策时两者分层呈现，高风险动作仍须人审。

设计：
- 只取带「（外部·」溯源标的事实（世界观察），不碰用户口述事实，避免混淆来源。
- 理解段来自独立 jsonl（最近 7 天、最多 3 条），与事实解耦，理解缺失不影响事实回灌。
- fail-open：任何异常返回已有的可用段（或空串），绝不阻断主流程（聊天 / 主动 tick 都比记忆重要）。
- 7 天过期的条目在 ``relevant()`` 里已被自动过滤，不会注入过期世界。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

_EXTERNAL_PREFIX = "（外部"
# 后缀式溯源标：外部信息流水线（ingestion_pipeline._fact_for_internal）把标记加在**末尾**
_SUFFIX_RE = re.compile(r"（外部·[^）]{0,20}）\s*$")


def is_world_fact(fact: str) -> bool:
    """判断一条 learned_history 事实是不是「小桔看到的世界」（外部观察）。

    溯源标实际有**两种**写法，都必须认，否则会静默漏掉整批观察：

    - 前缀式（digest 写入）：``（外部·文化产业）风向：……``
    - 后缀式（外部信息流水线写入）：``……摘要（外部·news）``

    2026-09-06 打包前全链体检实测：真实库 24 条外部观察里 **17 条是后缀式**
    （豆包搜索摄入、external_items 状态已 digested，即已过可信闸门），
    而旧判定只认前缀 → 这 17 条真世界新闻**一条都进不了**观察日记 / 认知库 /
    对话回灌，链路在这儿静默断掉。故收敛为前缀、后缀都识别的单一判定点。

    只认这两种位置（不认句中），避免把用户口述里偶然出现的字样误判成世界观察。
    """
    t = str(fact or "").strip()
    if not t:
        return False
    if t.startswith(_EXTERNAL_PREFIX):
        return True
    return bool(_SUFFIX_RE.search(t))


# ---- 三段独立配额（2026-11-15 修「主干道断点」）------------------------------
# 病因：三段原本按「事实 → 理解 → 立场」拼，而 manager.py 给 world 块 cap=4500、
# context_budget._cap_text 是 text[:cap] 从头截。真实库里光事实段就 4740 字符，
# 于是「她对世界的理解」「她自己想通透的立场」连同防虚构的作品硬锚，
# 每轮都被静默切掉——接线正确，但一次都没抵达聊天。
# 修法：① 每段各有配额，互不挤占；② 保头截断下越靠前越安全，所以先放
# 「她是谁」（立场 / 理解），再放「她看过什么」（剪报）；③ 三者之和锁在
# 4400 以内（manager 的 cap=4500 留余量），不增加每轮 token 消耗。
_WORLD_TOTAL_CAP = 4400
_WORKS_ANCHOR_MARK = "【萧盛真实作品事实"


def _quota(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _quotas(compact: bool = False) -> tuple[int, int, int]:
    """三段配额；环境变量便于灰度对照，总和超限时按比例缩回。

    ``compact`` 供闲聊轮用：只留「她是谁」的精简版（立场 700 / 理解 300 /
    事实 0），不灌新闻剪报。``_clip_beliefs`` 保证作品硬锚在立场段尾部且永不裁，
    所以硬锚位置始终 ≤700，落在闲聊轮 ``cap_light=1200`` 的存活区内。
    """
    if compact:
        return (
            _quota("DESKMATE_WORLD_LIGHT_BELIEFS_CAP", 700),
            _quota("DESKMATE_WORLD_LIGHT_UNDERSTAND_CAP", 300),
            0,
        )
    caps = [
        _quota("DESKMATE_WORLD_BELIEFS_CAP", 1900),
        _quota("DESKMATE_WORLD_UNDERSTAND_CAP", 900),
        _quota("DESKMATE_WORLD_FACTS_CAP", 1500),
    ]
    total = sum(caps)
    if total > _WORLD_TOTAL_CAP:
        caps = [max(200, int(c * _WORLD_TOTAL_CAP / total)) for c in caps]
    return caps[0], caps[1], caps[2]


def _clip_lines(text: str, cap: int) -> str:
    """按整行保头裁剪，不在条目中间腰斩；事实行是倒序的，保留即保留最新。"""
    if not text or len(text) <= cap:
        return text or ""
    lines = text.split("\n")
    kept: list[str] = []
    used = 0
    for line in lines:
        need = len(line) + (1 if kept else 0)
        if used + need > cap:
            break
        kept.append(line)
        used += need
    if not kept:
        return lines[0][: max(0, cap - 1)] + "…"
    return "\n".join(kept)


def _clip_beliefs(text: str, cap: int) -> str:
    """立场段裁剪：头部按配额取完整条目，尾部作品硬锚**永不裁**。

    硬锚是防虚构闸门（提及萧盛作品前必须核对清单），宁可少给几条立场，
    也不能让它在预算裁剪里消失；test_world_context.py 也锁着这条。
    """
    if not text:
        return ""
    idx = text.find(_WORKS_ANCHOR_MARK)
    if idx < 0:
        return _clip_lines(text, cap)
    head = text[:idx].rstrip()
    anchor = text[idx:].rstrip()
    head = _clip_lines(head, max(0, cap - len(anchor) - 2))
    return (head + "\n\n" + anchor) if head else anchor


def build_world_context(
    base_dir: str | Path | None = None,
    limit: int = 25,
    query: str | None = None,
    *,
    compact: bool = False,
) -> str:
    """取近期活跃的「世界观察」事实 + 小桔的理解，格式化为一段注入文本。

    Args:
        base_dir: 记忆库目录；None 走默认（~/.deskmate）。
        limit: 最多取多少条活跃事实做筛选（默认 25）。
        query: 可选——当前对话/决策的查询文本。传了就用语义检索（hybrid_search）
               从 external_items 取与 query 相关的 digested 条目，补充到事实段。
               不传则保持现有行为（只从 learned_history 取近期活跃）。
               语义检索走可信闸门（trusted_only=True），只返回已验证条目；
               embedding 不可用时自动降级为纯关键词，再不行就只用 learned_history。
        compact: 闲聊轮精简版（2026-11-15）：立场/理解用小额配额、事实段整个不取，
                 连语义检索都不跑（「早」当 query 只会捞噪声）。合计约 1000 字符，
                 落在闲聊轮 ``cap_light=1200`` 内，作品硬锚必然存活。

    Returns:
        三段拼装的多行文本，顺序为「她想通透的立场 → 她对世界的理解 →
        她观察到的世界」，各段独立配额（见 ``_quotas``），工作轮合计不超过
        4400 字符，保证 manager.py 的 cap=4500 从头截断时三段都还在。
        三段皆空时返回空串（作品硬锚恒在，实际几乎不为空）。
    """
    beliefs_cap, understand_cap, facts_cap = _quotas(compact=compact)
    if facts_cap > 0:
        fact_block = _clip_lines(
            _build_fact_block(base_dir, limit, query=query), facts_cap
        )
    else:
        fact_block = ""
    understand_block = _clip_lines(_build_understand_block(base_dir), understand_cap)
    worldview_block = _clip_beliefs(_build_worldview_block(base_dir), beliefs_cap)
    # 保头截断下越靠前越安全：先「她是谁」，再「她看过什么」。
    # 历史顺序（事实在前）会把她的理解与立场整段挤掉，勿改回去。
    parts = [p for p in (worldview_block, understand_block, fact_block) if p]
    if not parts:
        return ""
    return "\n\n".join(parts)


def _build_fact_block(base_dir: str | Path | None, limit: int, *, query: str | None = None) -> str:
    """构建事实段：learned_history 近期活跃 +（可选）语义检索相关条目。

    两路合并去重（按归一化后的 fact 文本）。语义检索失败不影响 learned_history。
    """
    import re

    lines: list[str] = []
    seen: set[str] = set()

    def _norm(text: str) -> str:
        return re.sub(r"\s+", "", text or "")

    def _add(fact: str, ts: str) -> None:
        key = _norm(fact)
        if not key or key in seen:
            return
        seen.add(key)
        lines.append(f"- [{ts}] {fact}")

    # 路 1：learned_history 近期活跃（带「（外部·」溯源标）
    try:
        from .learned_store import LearnedStore

        items = LearnedStore(base_dir=base_dir).relevant(limit=limit)
        for it in items:
            fact = (it.get("fact") or "").strip()
            if not is_world_fact(fact):
                continue  # 只回灌世界观察（前缀/后缀溯源标都认），不碰用户事实
            ts = (it.get("ts") or "")[:10]
            _add(fact, ts)
    except Exception:
        pass

    # 路 2：语义检索（传了 query 才走）——从 external_items 取与 query 相关的 digested 条目
    if query and query.strip():
        try:
            from .external_store import ExternalStore
            from .semantic_search import SemanticSearcher

            store = ExternalStore(base_dir=base_dir)
            searcher = SemanticSearcher(store=store)
            result = searcher.hybrid_search(
                query.strip(),
                limit=min(limit, 15),
                trusted_only=True,  # 可信闸门：只取已验证条目
            )
            for r in result.get("results", []):
                # 格式化成带「（外部·」溯源标的事实，与 learned_history 风格一致
                stype = (r.get("source_type") or "unknown").strip()
                title = (r.get("title") or "").strip()
                summary = (r.get("summary") or "").strip()
                content = (r.get("content") or "").strip()
                body = summary or content
                if title and body and title not in body:
                    fact = f"（外部·{stype}）{title}：{body}"
                elif title:
                    fact = f"（外部·{stype}）{title}"
                else:
                    fact = f"（外部·{stype}）{body}"
                # 截断过长的事实
                if len(fact) > 240:
                    fact = fact[:240] + "…"
                ts = (r.get("fetched_at") or "")[:10]
                _add(fact, ts)
        except Exception:
            # 语义检索失败不影响 learned_history 的事实
            pass

    if not lines:
        return ""

    header = (
        "## 她观察到的世界（小桔近期主动摄入的外部信息，供你回应时参考；"
        "这是她看到的，不是用户原话，引用时勿当成萧哥说过的）"
    )
    return header + "\n" + "\n".join(lines[:limit])


def _build_understand_block(base_dir: str | Path | None) -> str:
    """读理解层 jsonl，回灌「她的理解」段（决策参考，可能带主观）。"""
    root = (
        Path(base_dir)
        if base_dir
        else Path(os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate")))
    )
    path = root / "agency" / "observation_understanding.jsonl"
    if not path.is_file():
        return ""
    try:
        from ..agency.behaviors.worldview_reflection import _is_automation_noise

        cutoff = time.time() - 7 * 86400  # 只取近 7 天
        blocks: list[str] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = float(rec.get("ts") or 0)
                if ts < cutoff:
                    continue
                text = str(rec.get("body") or rec.get("understanding") or "").strip()
                if text and not _is_automation_noise(text):
                    blocks.append(text)
        if not blocks:
            return ""
        # 最多 3 条，避免注入过长；倒序（最新的在前）
        blocks = blocks[-3:][::-1]
        header = (
            "## 她对世界的理解（小桔消化观察后的判断，供决策参考；"
            "这是她的视角、可能带主观，高风险动作仍须人审）"
        )
        return header + "\n" + "\n".join(f"- {b}" for b in blocks)
    except Exception:
        return ""


def _build_worldview_block(base_dir: str | Path | None) -> str:
    """把她「想通透的立场」（worldview 层）回灌进对话上下文。

    2026-09-16 补的缺口：worldview 层此前只有被 AlterEgo 门控的 build_cognitive_context
    一个读取点，等于写进库没人读。这里把它接进「她观察到的世界」这条常驻上下文
    （由 manager.py 经 AUTO_INJECT 注入聊天，不依赖 AlterEgo 开关），让她的 digested
    立场能在日常对话里自然流露。fail-open：读不到就空，不影响世界观察回灌。
    """
    try:
        from ..agency.behaviors.worldview_reflection import load_worldview_beliefs
    except Exception:
        return ""
    try:
        wv = load_worldview_beliefs(base_dir)
        if not wv.strip():
            return ""
        header = (
            "## 她自己想通透的立场（小桔回望世界长出来的判断，不是萧盛的原话，"
            "聊天时可自然带出；这是她的视角、可能带主观）"
        )
        return header + "\n" + wv
    except Exception:
        return ""
