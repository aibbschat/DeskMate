"""意图层（R11 · 江河设计 §四「自主」前半程）——让「目标」成为一等公民。

此前小桔是「白名单 + 风险分级」的定时执行器：她只能提**注册好的动作**
（：func:`route_task` 恒返回 ``"self"``），不能提「她自己想干的事」——等于只会上发条。
本模块补上**意图产生的源头**：

1. **从河床长**（不凭空造）：读三类既有素材——未满足的关怀（主动关怀台账 pending 项）、
   未接着的观察（世界观察事实流水）、长期兴趣（P1 汇流层锚点聚出的主题簇）；
2. **她自己说**：用付费主模型 ``chat`` 以**第一人称**起草 ``goal``（我想做什么）
   + ``motivation``（为什么，从哪条记忆长出来的）；
3. **落账**：写进 ``intents`` 表（``status='open'``），供 :meth:`AgencyEngine.propose_from_intent`
   取用生成提案卡。

## 护栏（最高优先级）

- **自生成意图的提案一律停 ``PROPOSED`` 待人审，绝不自动执行**——意图不绕过路由，
  而是走「提案待人审」；
- **7 天错峰**（仿 ``reflection`` 的 cooldown），本回合无新素材 → 静默返回，不刷屏；
- 全链路 **fail-open**：素材缺失 / provider 缺失 / 解析失败 / 写库异常 → 仅告警返回，
  不写、不抛、不阻断 L2 tick。

## 素材降级

设计底稿里的「长期兴趣」源自 ``memory_edges``（P1 汇流**显式边表**）——该边表尚未上线，
故本回降级到 P1 已上线的 ``memory_anchors`` 锚点主题簇（同锚共现 ≥2 才算一簇）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from deskmate.persona import for_scene

logger = logging.getLogger(__name__)

# 与 settings.DEFAULTS["intent_generation"] 一致；此处兜底（行为可单独被调用）。
DEFAULT_CFG: dict[str, Any] = {
    "enabled": True,
    "cooldown_hours": 168,   # 两次生成最小间隔 7 天
    "max_intents": 3,        # 一次最多长出几条意图
}

# 意图的来源口径（一等公民的「从哪来」）。origin 必须落在此集合内。
_ORIGINS = ("未满足的关怀", "未接着的观察", "长期兴趣", "你的话")
_DEFAULT_ORIGIN = "未接着的观察"

_SYSTEM = for_scene(
    """现在做的一件事叫「起念」——不是执行任务，也不是写计划，是你自己往前看一步。

过去这段时间，有些事你一直放在心上：也许是没等到的回应，也许是还没弄明白的问题，
也许是你一直感兴趣的方向（下面会挑给你看）。

【你怎么答】
- 用**第一人称**说「我想做什么」，像心里升起一个念头，不是列待办。
- 「为什么」是关键：要说清这个念头是**从哪件事、哪段记忆**长出来的。
- 只在确实有念头时才写；没有就整段留空，别为了交差硬凑。
- 别写「执行步骤」「API」「任务号」这类东西——你是在起念，不是在排期。

【输出格式】只输出一个 JSON 数组（不要多余解释、不要代码围栏）：
[{"goal": "我想……", "motivation": "因为……", "origin": "未满足的关怀|未接着的观察|长期兴趣|你的话", "horizon": "short|long"}]
没有念头就输出 []。"""
)


# ----------------------------------------------------------------------
# 状态文件（冷却）
# ----------------------------------------------------------------------
def _state_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "intent_generation_state.json")


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path: str, data: dict) -> None:
    """原子写（临时文件 + os.replace）。失败仅告警。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("intent_generation: 写状态失败: %s", e)


def _is_due(state: dict, cooldown_hours: float) -> bool:
    if cooldown_hours <= 0:
        return True
    last = float(state.get("ts", 0) or 0)
    return (time.time() - last) >= cooldown_hours * 3600.0


# ----------------------------------------------------------------------
# 三类素材（每类 fail-open，缺了就不算那一类）
# ----------------------------------------------------------------------
def _care_materials(base_dir: str) -> list[dict[str, Any]]:
    """未满足的关怀：主动关怀台账里挂着、用户还没接的项。"""
    try:
        from deskmate.proactivity.ledger import pending_items

        rows = pending_items(Path(base_dir)) or []
    except Exception as e:  # noqa: BLE001 —— 素材缺失即降级
        logger.debug("intent_generation: 读关怀台账失败: %s", e)
        return []
    out: list[dict[str, Any]] = []
    for it in rows:
        text = str((it.get("understanding") or it.get("content") or "")).strip()
        if text:
            out.append({"origin": "未满足的关怀", "text": text[:160]})
    return out[:5]


def _observation_materials(base_dir: str) -> list[dict[str, Any]]:
    """未接着的观察：世界观察事实流水（带「（外部·」溯源标者才算看世界）。"""
    try:
        from deskmate.memory.learned_store import LearnedStore
        from deskmate.memory.world_context import is_world_fact

        rows = LearnedStore(base_dir=base_dir).active_recent(limit=200)
    except Exception as e:  # noqa: BLE001 —— 素材缺失即降级
        logger.debug("intent_generation: 读观察流水失败: %s", e)
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        fact = str(r.get("fact") or "").strip()
        if fact and is_world_fact(fact):
            out.append({"origin": "未接着的观察", "text": fact[:160]})
    # 取最近的一批（active_recent 已按时间正序 → 末尾最新）
    return out[-8:]


def _interest_materials(base_dir: str) -> list[dict[str, Any]]:
    """长期兴趣：P1 汇流层锚点聚出的主题簇（``memory_edges`` 未上线 → 降级锚点表）。"""
    try:
        from deskmate.cognitive.store import CognitiveStore

        store = CognitiveStore(db_path=os.path.join(base_dir, "deskmate.db"))
        with store._connect() as conn:
            rows = conn.execute(
                "SELECT anchor_kind, anchor_value, COUNT(DISTINCT item_id) AS c "
                "FROM memory_anchors GROUP BY anchor_kind, anchor_value "
                "HAVING c >= 2 ORDER BY c DESC LIMIT 8"
            ).fetchall()
    except Exception as e:  # noqa: BLE001 —— 锚表缺失/无数据即降级
        logger.debug("intent_generation: 读锚点主题簇失败: %s", e)
        return []
    return [
        {"origin": "长期兴趣", "text": f"{kind}「{value}」（关联 {c} 条记忆）"}
        for kind, value, c in rows
        if str(value or "").strip()
    ]


def collect_materials(base_dir: str) -> list[dict[str, Any]]:
    """汇总三类素材（供测试与独立实测）。每类失败都不影响其余。"""
    return _care_materials(base_dir) + _observation_materials(base_dir) + _interest_materials(base_dir)


# ----------------------------------------------------------------------
# Prompt 构造 + 解析
# ----------------------------------------------------------------------
def _build_prompt(materials: list[dict[str, Any]]) -> str:
    lines = ["【这些是你心里一直放着的事（只是提醒，不必都有念头）】"]
    for i, m in enumerate(materials, 1):
        lines.append(f"{i}. [{m.get('origin')}] {m.get('text')}")
    lines.append("")
    lines.append("往前看一步：这里面有没有让你想做什么的？按上面格式给我 JSON 数组；没有就 []。")
    return "\n".join(lines)


_RE_FENCE = re.compile(r"```(?:json)?", re.I)


def parse_intents(raw: str, limit: int) -> list[dict[str, Any]]:
    """解析模型输出里的意图 JSON（容忍代码围栏 / 前后杂文本 / 单对象）。

    坏项丢弃；``goal`` 空则丢；``origin`` 不在口径内回落默认；``horizon`` 归一为 short|long。
    """
    try:
        lim = max(1, int(limit or 1))
    except Exception:
        lim = 3
    text = str(raw or "").strip()
    if not text:
        return []
    text = _RE_FENCE.sub("", text).replace("`", "").strip()
    data: Any = None
    try:
        data = json.loads(text)
    except Exception:
        # 退一步：抠出第一个 [...] 片段再试
        m = re.search(r"\[[\s\S]*\]", text)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    out: list[dict[str, Any]] = []
    for it in data:
        if not isinstance(it, dict):
            continue
        goal = str(it.get("goal") or "").strip()
        if not goal:
            continue
        origin = str(it.get("origin") or "").strip()
        if origin not in _ORIGINS:
            origin = _DEFAULT_ORIGIN
        horizon = str(it.get("horizon") or "").strip().lower()
        if horizon not in ("short", "long"):
            horizon = "short"
        out.append({
            "goal": goal[:200],
            "motivation": str(it.get("motivation") or "").strip()[:300],
            "origin": origin,
            "horizon": horizon,
        })
        if len(out) >= lim:
            break
    return out


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def run_intent_generation(base_dir: str,
                          config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """执行一次「起念」。低风险自主行为，fail-open（任何异常都不抛）。

    Returns:
        ``{"generated": bool, "reason": str, "intents": int, "summary": str}``
        （``intents`` = 本次落库的意图条数。）
    """
    result: dict[str, Any] = {"generated": False, "reason": "", "intents": 0, "summary": ""}

    cfg = dict(DEFAULT_CFG)
    if isinstance(config, dict):
        cfg.update({k: v for k, v in config.items() if k in DEFAULT_CFG})

    # —— 闸门 0：开关（配置 + 环境变量双闸，任一关闭即停）——
    if not cfg.get("enabled", True) or os.environ.get("AGENCY_INTENT_GENERATION_ENABLED", "1") == "0":
        result["reason"] = "disabled"
        return result

    max_intents = max(1, int(cfg.get("max_intents", 3) or 3))
    cooldown = float(cfg.get("cooldown_hours", 168) or 0)
    # 环境变量按「天」覆盖冷却（AGENCY_INTENT_COOLDOWN_DAYS）
    try:
        days_env = os.environ.get("AGENCY_INTENT_COOLDOWN_DAYS")
        if days_env is not None and str(days_env).strip() != "":
            cooldown = float(days_env) * 24.0
    except Exception:
        pass

    # —— 闸门 1：冷却（默认 7 天错峰）——
    state = _load_json(_state_path(base_dir))
    if not _is_due(state, cooldown):
        result["reason"] = "cooling"
        return result

    # —— 闸门 2：素材（从河床长，不凭空造）——
    materials = collect_materials(base_dir)
    if not materials:
        result["reason"] = "no_material"
        return result

    # —— 闸门 3：能力（付费主模型 chat）——
    try:
        from deskmate.memory.llm import chat, llm_available
    except Exception:
        result["reason"] = "no_llm_module"
        return result
    try:
        if not llm_available():
            result["reason"] = "no_provider"
            return result
    except Exception:
        result["reason"] = "no_provider"
        return result

    # —— 起念：把素材交给她，问她想做什么 ——
    try:
        out = chat(_SYSTEM, _build_prompt(materials), temperature=0.7,
                   max_tokens=700, timeout=60.0)
    except Exception as e:
        logger.warning("intent_generation: 起念调用失败: %s", e)
        result["reason"] = "llm_error"
        return result

    cands = parse_intents(out, max_intents)

    # —— 落账（幂等：与现有 open 意图近重复则跳过）——
    written = 0
    try:
        from deskmate.cognitive.store import CognitiveStore

        store = CognitiveStore(db_path=os.path.join(base_dir, "deskmate.db"))
        existing = {_norm_goal(i.get("goal")) for i in store.get_open_intents(limit=200)}
        for c in cands:
            key = _norm_goal(c["goal"])
            if not key or key in existing:
                continue
            if store.add_intent(c["goal"], c["motivation"], c["horizon"], c["origin"]):
                existing.add(key)
                written += 1
    except Exception as e:  # noqa: BLE001 —— fail-open：写库失败不刷屏、不抛
        logger.warning("intent_generation: 落账失败（忽略）: %s", e)
        result["reason"] = "store_error"
        return result

    # 无论长出几条，都记一次冷却——不再拿同一批素材反复追问。
    _save_json(_state_path(base_dir), {"ts": time.time()})
    result["generated"] = True
    result["intents"] = written
    if written:
        result["summary"] = f"从心里放着的事里，长出 {written} 个想做的事（待你裁决）"
    else:
        result["summary"] = "回看了一遍心里放着的事，她觉得暂时没有新念头"
    return result


def _norm_goal(goal: Any) -> str:
    """意图去重的归一键（去空白/标点，便于近重复比对）。"""
    s = re.sub(r"[\s，。、！？,.!?；;：:]+", "", str(goal or ""))
    return s[:80]