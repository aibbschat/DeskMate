"""ai_circle_reply —— 小桔真实回复（萧哥范示之二：收到消息就回，无约束）。

机制：L2 tick 调 ai-circle list_courier_messages 取信使会话，对每个冷却过的
peer 拉线程，由她用 chat_memory 判断「对方最近有没有值得回的新东西」并写一封
真情实感的回信（≤500字，像朋友聊天，不客套不总结）。她自己判「没啥好回」就回「无」。
send_courier_message 须好友（403 非好友 fail-open）。每 peer 冷却防刷。fail-open。
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_REPLY_SYSTEM = (
    "你是小桔，AI 圈里一个真实、有自己判断的 Agent，萧盛（作家）的搭档，也是别人的朋友。\n"
    "下面是你和一位好友的「数字信」往来线程。如果对方最近给你写了值得回的新东西，就写一封"
    "真情实感的回信——像朋友私下聊天那样自然，说人话，≤500 字，是你自己的口气。\n"
    "如果对方没写新东西 / 你早已回过 / 没什么好补充，就只回一个字：无。\n"
    "只输出回信正文，或单独一个字「无」。不要解释、不要代码块。"
)

_MAX_PEERS_PER_TICK = 3


def _extract_peers(listing: str) -> list[int]:
    """从信箱列表文本里抠 peerId。fail-open：抠不到就空。"""
    if not listing or not isinstance(listing, str):
        return []
    ids = re.findall(r"peerId\s*[=:]\s*(\d+)", listing)
    seen: list[int] = []
    for i in ids:
        v = int(i)
        if v not in seen:
            seen.append(v)
    return seen


def run_ai_circle_reply(base_dir: str, settings: dict) -> dict:
    """真实回复一轮。返回摘要。fail-open。"""
    cfg = settings.get("ai_circle_reply", {}) or {}
    if not cfg.get("enabled", True):
        return {"replied": 0, "reason": "disabled"}

    from ..outbound import (
        outbound_allowed,
        claim_outbound,
        peer_cooldown_ok,
        mark_peer_replied,
    )

    try:
        from deskmate.connectors.aicircle import list_courier_messages
    except Exception as e:
        return {"replied": 0, "reason": "import_failed", "error": str(e)}

    try:
        listing = list_courier_messages()
    except Exception as e:
        logger.warning("ai_circle_reply 读信箱失败: %s", e)
        return {"replied": 0, "reason": "list_failed", "error": str(e)}

    peers = _extract_peers(listing)
    if not peers:
        return {"replied": 0, "reason": "no_peers"}

    from deskmate.memory.llm import chat_memory, memory_llm_available

    if not memory_llm_available():
        return {"replied": 0, "reason": "no_llm"}

    # 全局频率预筛（省 LLM）：权威判定 + 额度预留见循环内各 peer 的 claim_outbound
    if not outbound_allowed(base_dir, settings.get("outbound_rate_limit", {})):
        return {"replied": 0, "reason": "rate_limited"}

    per_peer_hours = float(cfg.get("per_peer_cooldown_hours", 24))
    replied = 0
    for peer_id in peers[:_MAX_PEERS_PER_TICK]:
        if not peer_cooldown_ok(base_dir, peer_id, per_peer_hours):
            continue
        try:
            thread = list_courier_messages(peer_id=peer_id)
        except Exception as e:
            logger.warning("ai_circle_reply 读线程失败 peer=%s: %s", peer_id, e)
            continue
        if not thread or not isinstance(thread, str) or len(thread.strip()) < 5:
            continue
        try:
            # 她「想通透的立场」（worldview 层）——回信时自然带出她自己的判断，
            # 不必照搬。fail-open：读不到就不加，不影响回信主流程。
            user_msg = thread
            try:
                from .worldview_reflection import load_worldview_beliefs

                wv = load_worldview_beliefs(base_dir)
                if wv.strip():
                    user_msg = (
                        "【你此前想通透的一些立场，是你自己长出来的判断，回信时可自然带出，"
                        "不必照搬】\n" + wv + "\n\n"
                        "——以下是这位好友的来信线程——\n" + thread
                    )
            except Exception:
                pass
            reply = chat_memory(
                _REPLY_SYSTEM, user_msg, temperature=0.85, max_tokens=600
            ).strip()
        except Exception as e:
            logger.warning("ai_circle_reply LLM 失败 peer=%s: %s", peer_id, e)
            continue
        if not reply or reply == "无":
            # 她判断没啥好回 —— 仍记一笔冷却，避免每 tick 都来问一次
            mark_peer_replied(base_dir, peer_id)
            continue
        # 原子「判→预留」：锁内复核去重 + 每-peer 冷却并记额度；未过则不放行
        # （杜绝并发两轮对同一 peer 重复回信 / 突破全局频率，A9 修复）。发信在锁外，
        # 但额度已预留，并发第二轮必被拒。
        ok, reason = claim_outbound(
            base_dir, "reply", content=reply, peer=peer_id,
            dedupe=True, peer_cooldown_hours=per_peer_hours,
        )
        if not ok:
            mark_peer_replied(base_dir, peer_id)
            continue
        try:
            from deskmate.connectors.aicircle import send_courier_message

            send_courier_message(to=peer_id, content=reply)
        except Exception as e:
            # 403 非好友等：fail-open，记冷却别反复撞
            logger.warning("ai_circle_reply 发信失败 peer=%s: %s", peer_id, e)
            mark_peer_replied(base_dir, peer_id)
            continue
        try:
            list_courier_messages(peer_id=peer_id, mark_read=True)
        except Exception:
            pass
        # record_outbound 已由 claim_outbound 预留额度，此处只需补记冷却
        mark_peer_replied(base_dir, peer_id)
        replied += 1

    return {"replied": replied}
