"""反思层 R1（江河设计 · 第二段「反思」）——回看判断与看法，做裁决并留下为什么。

世界观回望（``worldview_reflection``）是「到点重新生成、覆盖旧的」；本模块补上
**「对已有看法做出裁决，并留下为什么」**——把沉淀变成**有记忆的沉淀**：

1. **回看判断/看法**：翻 ``item_history`` 最近若干条真实变化
   （created/updated/calibrated/superseded/questioned…，这张账本此前**从没被回看**）；
2. **照镜子**：把最近这些变化交给模型，问「哪些看法该保持、哪些该修正、哪些该推翻，
   以及为什么」；
3. **落痕**：裁决 + 理由写回 ``item_history``（``action='reflected'``）——这是「为什么变」
   的账，下一轮注入（``build_reflection_context``）会被召回，不再被覆盖吞掉。

与 ``worldview_reflection`` 的关系：**并列的挂点**（``engine._run_low_risk``），不是替代。
- ``worldview_reflection``：攒够理解 → 长出「新看法」（生成）；
- ``reflection``：回看变化 → 对旧看法做裁决 + 留因（修订）。
两者**错峰**：worldview 用「攒够 5 条理解」触发，反思用「最近变化 ≥ min_changes」触发
（不同素材、不同批次），且各过独立冷却，避免同一 tick 双重 LLM 轰炸。

硬断言（江河设计 §三）：``reason`` 为空 = 本次反思作废（不落 ``record_reflection``）——
落代码拦，不靠自觉。

fail-open：provider 缺失 / 素材不足 / 冷却未过 / LLM 异常 / 写库失败 → 一律返回
``{"reflected": False, "reason": ...}``，绝不抛异常、绝不阻断 L2 tick。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Optional

from deskmate.persona import for_scene

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 配置默认值（与 settings.DEFAULTS["reflection"] 一致；此处兜底）
# ----------------------------------------------------------------------
DEFAULT_CFG: dict[str, Any] = {
    "cooldown_hours": 168,   # 两次反思最小间隔 7 天
    "max_recent": 20,        # 一次最多回看多少条 item_history 变化
    "min_changes": 3,        # 攒够几条未反思的变化才做一次裁决
}

# 算作「真实变化」的 history 动作（与 store._CONTENT_ACTIONS + 状态变更合并）。
# 故意**不含** 'reflected' 自身——否则反思会把自己的痕当成新变化，形成自循环。
_CHANGE_ACTIONS = (
    "created", "updated", "deepened", "restored", "calibrated",
    "superseded", "questioned",
)

# ----------------------------------------------------------------------
# Prompt
# ----------------------------------------------------------------------
_SYSTEM = for_scene(
    """现在做的一件事叫「反思」——不是重新学一遍，也不是写日记。

过去这段时间，你的判断和看法有过一些变化（下面会给你）。现在回头看它们：
哪些你还认，哪些你已经变了，哪些你不再信——以及**为什么**。

【你怎么答】
- 不是每条都必须表态。觉得没变的可以不说，拿不准的就跳过。
- 「为什么」是关键：没有理由的表态不算数，宁可不写。
- 别复述那些变化本身，要说你现在怎么看这件事。
- 保持就是保持，不用为了显得有成长而硬说「我变了」。

【小结格式】按下面的「编号 / 裁决 / 理由」三行一组来写；可以写多组，
也可以整段留空——别硬凑。"""
)

# 小结行解析（行首匹配，容忍全角/半角冒号与前后空白）
_RE_NO = re.compile(r"^\s*编号\s*[：:]\s*(\d+)\s*$")
_RE_VERDICT = re.compile(r"^\s*裁决\s*[：:]\s*(保持|修正|推翻|maintain|revise|overturn)\s*$", re.I)
_RE_REASON = re.compile(r"^\s*理由\s*[：:]\s*(.+?)\s*$")
_VERDICT_MAP = {
    "保持": "maintain", "修正": "revise", "推翻": "overturn",
    "maintain": "maintain", "revise": "revise", "overturn": "overturn",
}


# ----------------------------------------------------------------------
# 状态文件（冷却 + 已回看去重）
# ----------------------------------------------------------------------
def _state_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "reflection_state.json")


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path: str, data: dict) -> None:
    """原子写（临时文件 + os.replace）——避免写一半崩掉丢数据。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("reflection: 写状态失败: %s", e)


def _is_due(state: dict, cooldown_hours: float) -> bool:
    """冷却闸门：没到时间不反思。"""
    if cooldown_hours <= 0:
        return True
    last = float(state.get("ts", 0) or 0)
    return (time.time() - last) >= cooldown_hours * 3600.0


# ----------------------------------------------------------------------
# 读 item_history 变化
# ----------------------------------------------------------------------
def _subject_kind(layer: str) -> str:
    """把条目所在层映射成「被反思对象的类别」。

    真实 ``item_history`` 里只有认知条目（profile_items）的变更，没有独立的
    judgment 账本；故 subject_kind 由 profile_items.layer 派生：worldview →
    ``worldview``，其余（cognitive / emotional）→ ``profile_item``。
    """
    if (layer or "").strip().lower() == "worldview":
        return "worldview"
    return "profile_item"


def _recent_changes(store, limit: int) -> list[dict]:
    """读最近的 item_history 真实变化（左连 profile_items 取层/正文）。

    fail-open：库异常 → ``[]``。``store`` 需提供 ``_connect()``（CognitiveStore）。
    """
    limit = max(1, int(limit))
    placeholders = ",".join("?" * len(_CHANGE_ACTIONS))
    sql = (
        "SELECT h.item_id, h.seq, h.action, h.old_value, h.new_value, h.reason, h.ts, "
        "COALESCE(p.layer, ''), COALESCE(p.dimension, ''), COALESCE(p.content, '') "
        "FROM item_history h LEFT JOIN profile_items p ON p.id = h.item_id "
        f"WHERE h.action IN ({placeholders}) "
        "ORDER BY h.ts DESC, h.seq DESC LIMIT ?"
    )
    out: list[dict] = []
    try:
        with store._connect() as conn:
            for row in conn.execute(sql, (*_CHANGE_ACTIONS, limit)):
                (item_id, seq, action, old_value, new_value, reason, ts,
                 layer, dim, content) = row
                out.append({
                    "item_id": item_id, "seq": seq, "action": action,
                    "old_value": old_value, "new_value": new_value,
                    "reason": reason, "ts": ts,
                    "subject_kind": _subject_kind(layer),
                    "dimension": dim, "content": content,
                })
    except Exception as e:
        logger.warning("reflection: 读 item_history 失败: %s", e)
        return []
    return out


# ----------------------------------------------------------------------
# Prompt 构造 + 解析
# ----------------------------------------------------------------------
def _build_prompt(batch: list[dict]) -> str:
    lines = ["【你最近有过变化的一些看法 / 判断（只是提醒你，不必逐条都表态）】"]
    for i, c in enumerate(batch, 1):
        content = (c.get("content") or "").strip().replace("\n", " ")
        content = content[:80] or str(c.get("item_id") or "")
        seg = f"{i}. {content}（最近动作：{c.get('action') or ''}）"
        why = (c.get("reason") or "").strip()
        if why:
            seg += f"　当时的理由：{why[:60]}"
        lines.append(seg)
    lines.append("")
    lines.append(
        "回头看这些：哪些你仍然认（保持），哪些你已经变了（修正），哪些你不再信（推翻）？"
    )
    lines.append("对每一条你确实想表态的，用这几行小结（没有就整段留空，别硬凑）：")
    lines.append("编号：<上面那条的序号>")
    lines.append("裁决：<保持 或 修正 或 推翻>")
    lines.append("理由：<一句话，为什么>")
    return "\n".join(lines)


def parse_verdicts(raw: str, batch_len: int) -> list[dict[str, Any]]:
    """解析模型输出里的「编号 / 裁决 / 理由」组。

    返回 ``[{"index": int, "verdict": str, "reason": str}, ...]``；越界 / 缺裁决 /
    缺理由的组一律丢弃（硬断言：没有理由的反思作废）。
    """
    out: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    for line in str(raw or "").splitlines():
        m_no = _RE_NO.match(line)
        if m_no:
            if cur is not None:
                out.append(cur)
            cur = {"index": int(m_no.group(1)) - 1, "verdict": "", "reason": ""}
            continue
        if cur is None:
            continue
        m_v = _RE_VERDICT.match(line)
        if m_v:
            key = m_v.group(1).strip()
            cur["verdict"] = _VERDICT_MAP.get(key.lower(), _VERDICT_MAP.get(key, ""))
            continue
        m_r = _RE_REASON.match(line)
        if m_r:
            cur["reason"] = m_r.group(1).strip()
    if cur is not None:
        out.append(cur)
    return [
        v for v in out
        if 0 <= v["index"] < max(0, int(batch_len))
        and v["verdict"] in ("maintain", "revise", "overturn")
        and (v["reason"] or "").strip()
    ]


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def run_reflection(base_dir: str,
                   config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """执行一次「反思」。低风险自主行为，fail-open（任何异常都不抛）。

    Returns:
        ``{"reflected": bool, "reason": str, "verdicts": int,
        "bailed": bool, "essay": str, "summary": str}``
        （``verdicts`` = 本次成功落痕的裁决条数。）
    """
    result: dict[str, Any] = {
        "reflected": False, "reason": "", "verdicts": 0,
        "bailed": False, "essay": "", "summary": "",
    }

    cfg = dict(DEFAULT_CFG)
    if isinstance(config, dict):
        cfg.update({k: v for k, v in config.items() if k in DEFAULT_CFG})

    min_changes = max(1, int(cfg.get("min_changes", 3) or 3))
    max_recent = max(min_changes, int(cfg.get("max_recent", 20) or 20))
    cooldown = float(cfg.get("cooldown_hours", 168) or 0)

    # —— 闸门 0：打开认知库 ——
    try:
        from deskmate.cognitive.store import CognitiveStore

        store = CognitiveStore(db_path=os.path.join(base_dir, "deskmate.db"))
    except Exception as e:
        logger.warning("reflection: 打开认知库失败: %s", e)
        result["reason"] = "no_store"
        return result

    # —— 闸门 1：素材够不够（最近有多少条未被反思的变化）——
    changes = _recent_changes(store, max_recent * 3)
    if not changes:
        result["reason"] = "no_changes"
        return result

    state = _load_json(_state_path(base_dir))
    reflected_keys = set(state.get("reflected") or [])
    pending = [
        c for c in changes
        if f"{c.get('item_id')}#{c.get('seq')}" not in reflected_keys
    ]
    if len(pending) < min_changes:
        result["reason"] = f"not_enough:{len(pending)}/{min_changes}"
        return result

    # —— 闸门 2：冷却 ——
    if not _is_due(state, cooldown):
        result["reason"] = "cooling"
        return result

    batch = pending[:max_recent]

    # —— 闸门 3：能力（默认启用 LLM 裁决；DESKMATE_REFLECTION_LLM=0 可先埋框架后置）——
    if os.environ.get("DESKMATE_REFLECTION_LLM", "1") == "0":
        result["reason"] = "llm_disabled"
        return result
    try:
        from deskmate.memory.llm import chat_memory, memory_llm_available
    except Exception:
        result["reason"] = "no_llm_module"
        return result
    try:
        if not memory_llm_available():
            result["reason"] = "no_provider"
            return result
    except Exception:
        result["reason"] = "no_provider"
        return result

    # —— 反思：把最近的变化交给她，问她现在还认哪些、为什么 ——
    prompt = _build_prompt(batch)
    try:
        out = chat_memory(_SYSTEM, prompt, temperature=0.6,
                          max_tokens=600, timeout=30.0)
    except Exception as e:
        logger.warning("reflection: 反思调用失败: %s", e)
        result["reason"] = "llm_error"
        return result

    essay = str(out or "")
    result["essay"] = essay.strip()
    verdicts = parse_verdicts(essay, len(batch))
    result["bailed"] = not verdicts

    # —— 落痕（硬断言：reason 空 → record_reflection 返回 False，不写）——
    #     并对 revise/overturn **落地**（R10）：真正改条目可信度/状态，让「改了看法」
    #     在数据与召回里可见；maintain 不改条目（R1 的 reflected 痕已足够）。
    recorded = 0
    for v in verdicts:
        c = batch[v["index"]]
        if store.record_reflection(
            c["item_id"], v["verdict"], v["reason"], c["subject_kind"]
        ):
            recorded += 1
            if v["verdict"] in ("revise", "overturn"):
                store.apply_reflection_verdict(
                    c["item_id"], v["verdict"], v["reason"]
                )
    result["verdicts"] = recorded

    # 无论她有没有给出结论，都标记这批已回看——不再拿同一批变化反复问她。
    for c in batch:
        reflected_keys.add(f"{c.get('item_id')}#{c.get('seq')}")
    if len(reflected_keys) > 2000:
        reflected_keys = set(sorted(reflected_keys)[-2000:])
    _save_json(_state_path(base_dir), {
        "ts": time.time(),
        "reflected": sorted(reflected_keys),
    })

    result["reflected"] = True
    if recorded:
        result["summary"] = (
            f"回看 {len(batch)} 条变化，对 {recorded} 条旧看法做了裁决并留下为什么"
        )
    else:
        result["summary"] = f"回看 {len(batch)} 条变化，她觉得暂无需要改的看法"
    return result