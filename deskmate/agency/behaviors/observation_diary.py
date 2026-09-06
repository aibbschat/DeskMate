"""低风险自主行为：观察日记（observation_diary）。

把小桔主动摄入的世界观察（落在 ``learned_history``、带「（外部·」溯源标）写成
她自己的**观察日记**——落本地日记文件，不碰用户文件、不发消息、不删东西。

这是「让她看世界」最后一跳的**低风险自主落地**（Plan B，2026-09-06 萧哥拍板）：

- 她不只是看到了、存下了、聊天里能引用；
- 还把自己的观察沉淀成日记，你能翻到（桌宠 → 查看日记）。

去重：每条观察按 fact 文本记一次，重复 tick 不会把同一条观察写进日记多遍。
冷却：按 ``cfg.cooldown_hours``（默认 12h）节流，避免后台每 30s 一轮狂写日记。
fail-open：任何异常都不抛、不阻断 L2 tick 主流程。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# 世界观察在 learned_history 里的统一溯源前缀（digest / world_ingestion 都用它）。
# 只把带这个前缀的事实当成「小桔自己观察到的世界」，不碰用户口述事实。
_PROVENANCE = "（外部·"


def _cooldown_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "last_observation_diary.json")


def _state_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "observation_diary_state.json")


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _is_due(cooldown_path: str, cooldown_hours: float) -> bool:
    state = _load_json(cooldown_path)
    last = state.get("ts")
    if not last:
        return True
    try:
        return (time.time() - float(last)) >= cooldown_hours * 3600
    except (TypeError, ValueError):
        return True


def _fact_key(fact: str) -> str:
    """观察去重 key：fact 文本归一化后的 md5。"""
    return hashlib.md5(fact.strip().encode("utf-8")).hexdigest()


def run_observation_diary(
    base_dir: str,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把新鲜的世界观察写成小桔的观察日记。

    Args:
        base_dir: deskmate 根目录（~/.deskmate）
        cfg: observation_diary 配置（cooldown_hours / max_observations）

    Returns:
        {"written", "skipped", "summary", "observations", "due"}
    """
    cfg = cfg or {}
    cooldown_hours = float(cfg.get("cooldown_hours", 12))
    max_obs = int(cfg.get("max_observations", 6))

    result: dict[str, Any] = {
        "written": 0, "skipped": 0, "summary": "", "observations": [], "due": False,
    }

    # 冷却节流：未到点不跑（避免后台频繁 tick 刷屏日记）
    cd_path = _cooldown_path(base_dir)
    if not _is_due(cd_path, cooldown_hours):
        result["summary"] = "冷却中，本轮未写观察日记"
        return result
    result["due"] = True

    # 取新鲜的世界观察（带「（外部·」溯源标）
    try:
        from deskmate.memory.learned_store import LearnedStore

        items = LearnedStore(base_dir=base_dir).active_recent(limit=200)
    except Exception as e:
        logger.warning("observation_diary: 读取记忆失败: %s", e)
        return result

    state = _load_json(_state_path(base_dir))
    written_keys = set(state.get("keys", []))

    fresh: list[str] = []
    for it in items:
        fact = str(it.get("fact", "") or "").strip()
        if _PROVENANCE not in fact:
            continue
        k = _fact_key(fact)
        if k in written_keys:
            result["skipped"] += 1
            continue
        fresh.append(fact)
        if len(fresh) >= max_obs:
            break

    # 没新观察：仍刷冷却，避免每 tick 空转检查
    if not fresh:
        _save_json(cd_path, {"ts": time.time()})
        result["summary"] = "没有新的世界观察可记"
        return result

    # 写进日记（小桔第一人称，确定性渲染，不进叙事润色）
    try:
        from datetime import date as _date

        from deskmate.growth.diary import DiaryWriter

        today = _date.today().isoformat()
        res = DiaryWriter(base_dir=base_dir).append_observation(today, fresh)
        written = int(res.get("written", 0))
    except Exception as e:
        logger.warning("observation_diary: 写日记失败: %s", e)
        return result

    if written:
        for f in fresh:
            written_keys.add(_fact_key(f))
        # 控制 state 体积：只保留最近 500 个 key
        if len(written_keys) > 500:
            written_keys = set(list(written_keys)[-500:])
        _save_json(_state_path(base_dir), {"keys": list(written_keys)})
        _save_json(cd_path, {"ts": time.time()})
        result["written"] = written
        result["observations"] = fresh
        result["summary"] = f"把 {written} 条世界观察写进了观察日记"
    return result
