"""低风险自主行为：观察日记（observation_diary）。

把小桔主动摄入的世界观察（落在 ``learned_history``、带「（外部·」溯源标）写成
她自己的**观察日记**——落本地日记文件，不碰用户文件、不发消息、不删东西。

这是「让她看世界」最后一跳的**低风险自主落地**（Plan B，2026-09-06 萧哥拍板）。

## 选材与成文（2026-09-07 萧哥拍板）

旧实现把所有素材**强制拼成一段**（"100-250 字"硬约束）。萧哥反馈：
多条完全不同的新闻被迫挤在一段，每条都只能分到一两句话，没有"认真
分析"的空间——更糟的是把无关的事件强行找联系，读起来是拼接感。

新实现的处理（萧哥原话 "让她自己决定哪些值得记，哪些不值得"）：

- **不分组、不预筛**：把全部新鲜观察交给她（默认 ``max_obs=20``，从旧 6 提上来）。
- **她自己挑**：LLM 看完所有事实后，自己判断哪几条值得记、哪几条不值得。
- **独立成段**：值得记的，每条独立一段；主题不同自然分开，不再硬挤。
- **字数不限**：有分量的事件可以深写 300-500+ 字；一般就 100-200 字。
  每段的字数由内容决定，不由 prompt 决定。
- **每日篇数不限**：她觉得今天 3 条都值得，就 3 条独立成则；0 条就 0 条；
  品质优先于数量——这是"宁缺毋滥"。

降级路径（LLM 不可用 / 理解层异常 / LLM 显式认输）：走确定性 bullet（事实原文），
fail-open 不阻断。每次 tick 至多触发一次（``cooldown_hours`` 默认 12h）。

去重：每条观察按 fact 文本记一次，重复 tick 不会把同一段观察写进日记多遍。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any

from deskmate.memory.world_context import is_world_fact

logger = logging.getLogger(__name__)

# 世界观察在 learned_history 里的统一溯源标记（digest / 外部信息流水线都用它）。
# 只把带这个标记的事实当成「小桔自己观察到的世界」，不碰用户口述事实。
# ⚠ 标记有**两种**位置：前缀式「（外部·文化产业）…」与后缀式「…（外部·news）」，
# 判定统一走 deskmate.memory.world_context.is_world_fact（单一事实源）。
_PROVENANCE = "（外部·"


def _cooldown_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "last_observation_diary.json")


def _state_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "observation_diary_state.json")


def _understanding_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "observation_understanding.jsonl")


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


def _save_understanding_line(base_dir: str, date_iso: str, fact_keys: list[str],
                             body: str, entry_seq: int) -> None:
    """把每段独立理解各落一行 jsonl，供 world_context 回灌决策。

    分行：每段一段独立行（含 entry_seq 定位到日记里的第几则），便于后续按
    "哪段对应哪些事实 keys" 检索。空 fact_keys 不写（兜底）。
    """
    if not body:
        return
    path = _understanding_path(base_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps(
            {"date": date_iso, "ts": time.time(),
             "entry_seq": entry_seq,
             "fact_keys": list(fact_keys), "body": body},
            ensure_ascii=False,
        )
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        logger.warning("observation_diary: 存理解失败（不影响日记）: %s", e)


def _add_understanding_cognition(base_dir: str, date_iso: str,
                                 body: str, fact_keys: list[str]) -> None:
    """把每段独立理解沉淀到认知库（供创作建议跨会话消费）。失败静默。

    注意：``cognition_store.add_understanding`` 的真实签名是 keyword-only
    (``understanding=`` / ``facts=``)，别再用 ``body=`` 瞎传。
    """
    try:
        from ..cognition_store import add_understanding as _add_cognition

        _add_cognition(
            base_dir,
            date=date_iso,
            fact_keys=list(fact_keys),
            understanding=body,
            facts=[],  # LLM 挑出的具体事实原文，与 fact_keys 不是 1:1，传空
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("observation_diary: 写认知库失败（不影响日记）: %s", e)


def run_observation_diary(
    base_dir: str,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把新鲜的世界观察写成小桔的观察日记（含理解层、她自选的多段）。

    Args:
        base_dir: deskmate 根目录（~/.deskmate）
        cfg: observation_diary 配置
             （cooldown_hours / max_observations / understand）

    Returns:
        ``{"written", "skipped", "summary", "observations", "due", "understood",
            "segments": [{"body", "fact_keys", "entry_seq"}], "dropped_by_llm"}``
    """
    cfg = cfg or {}
    cooldown_hours = float(cfg.get("cooldown_hours", 12))
    # 默认从 6 提到 20：让 LLM 看到足够样本自己判断，
    # 而不是被样本量截断而被迫把无关的事塞进一段。
    max_obs = int(cfg.get("max_observations", 20))
    do_understand = bool(cfg.get("understand", True))

    result: dict[str, Any] = {
        "written": 0, "skipped": 0, "summary": "", "observations": [],
        "due": False, "understood": False, "segments": [],
        "dropped_by_llm": 0,
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

        # ⚠ active_recent 返回时间**正序**（最老在前）——而观察日记要的是
        # **最新**的观察（"看世界→记当下"）。正序遍历 + break 取满 max_obs
        # 会先取到库里最旧的积压旧闻，最新的排在最后、好几轮 tick 才轮得到。
        # 故这里反序，让"最新的优先"。
        items = list(reversed(LearnedStore(base_dir=base_dir).active_recent(limit=200)))
    except Exception as e:
        logger.warning("observation_diary: 读取记忆失败: %s", e)
        return result

    state = _load_json(_state_path(base_dir))
    written_keys = set(state.get("keys", []))

    fresh: list[str] = []
    fresh_items: list[dict] = []
    for it in items:
        fact = str(it.get("fact", "") or "").strip()
        if not is_world_fact(fact):
            continue
        k = _fact_key(fact)
        if k in written_keys:
            result["skipped"] += 1
            continue
        fresh.append(fact)
        fresh_items.append(it)
        if len(fresh) >= max_obs:
            break

    # 没新观察：仍刷冷却，避免每 tick 空转检查
    if not fresh:
        _save_json(cd_path, {"ts": time.time()})
        result["summary"] = "没有新的世界观察可记"
        return result

    # 主路径：让 LLM 自己挑 + 独立成段
    segments: list[dict[str, Any]] = []
    understood_flag = False
    if do_understand:
        try:
            from .understand_observations import decide_and_digest

            facts_arg = [
                {"fact": f, "credibility": it.get("credibility")}
                for f, it in zip(fresh, fresh_items)
            ]
            segments = decide_and_digest(facts_arg, at="", base_dir=base_dir)
        except Exception as e:
            logger.warning("observation_diary: 理解层异常，降级原文: %s", e)
            segments = []

    # 记录"被 LLM 丢掉的素材数"：让萧哥能看到实际被舍弃了多少
    # （统计意义——不写进日记，只在 summary 里说）
    if segments:
        understood_flag = True
    else:
        # 走降级：把所有事实塞进一条单一 bullet（LLM 不可用时唯一可用路径）
        try:
            from datetime import date as _date

            from deskmate.growth.diary import DiaryWriter

            today = _date.today().isoformat()
            writer = DiaryWriter(base_dir=base_dir)
            res = writer.append_observation(today, fresh)
            written_now = int(res.get("written", 0))
            result["written"] = written_now
            result["summary"] = f"把 {written_now} 条世界观察写进了观察日记（LLM 不可用或不可挑，原文 bullet）"
            # 这些事实已"过手"，无论写没写进日记都标已记 + 刷冷却——
            # 否则写盘失败时下一 tick 会重复提交、反复空转。
            for f in fresh:
                written_keys.add(_fact_key(f))
            if len(written_keys) > 500:
                written_keys = set(list(written_keys)[-500:])
            _save_json(_state_path(base_dir), {"keys": list(written_keys)})
            _save_json(cd_path, {"ts": time.time()})
            return result
        except Exception as e:
            logger.warning("observation_diary: 降级 bullet 写盘失败: %s", e)
            return result

    # segments 不空：每段独立成则
    try:
        from datetime import date as _date

        from deskmate.growth.diary import DiaryWriter

        today = _date.today().isoformat()
        writer = DiaryWriter(base_dir=base_dir)

        written_now = 0

        for idx, seg in enumerate(segments):
            try:
                body = str(seg.get("body", "") or "").strip()
                fact_keys = list(seg.get("fact_keys", []) or [])
                if not body:
                    continue
                res = writer.append_observation_natural(today, body)
                seq = int(res.get("seq", 0) or 0)
                if seq <= 0:
                    continue
                # 每段独立回灌决策 / 沉淀认知库
                _save_understanding_line(
                    base_dir, today, fact_keys, body, seq,
                )
                _add_understanding_cognition(
                    base_dir, today, body, fact_keys,
                )
                written_now += 1
                result["segments"].append({
                    "body": body, "fact_keys": fact_keys, "entry_seq": seq,
                })
            except Exception as e:
                logger.warning("observation_diary: 写则 %d 失败: %s", idx, e)
                continue

        result["written"] = written_now
        result["understood"] = understood_flag

        # 把这一批提交的事实都标为已记——下次相同 fact 不会再被提交给 LLM
        # （无论 LLM 挑没挑，反正已知。这是按"事实是否过手观察日记"判定而非"是否
        # 写入日记"，跟原行为一致。）
        for f in fresh:
            written_keys.add(_fact_key(f))
        if len(written_keys) > 500:
            written_keys = set(list(written_keys)[-500:])
        # 事实已"过手"，无论写没写进日记都标已记 + 刷冷却——否则写盘失败时
        # 下一 tick 会重复调 LLM、反复空转（P1 修复）。
        _save_json(_state_path(base_dir), {"keys": list(written_keys)})
        _save_json(cd_path, {"ts": time.time()})

        # 统计：被 LLM 主动丢掉的素材条数。
        # LLM 没返回"每段对应哪条事实"的映射，精确到条做不到——用段数近似：
        # 交给她 N 条，她挑出 M 段，则 (N-M) 条她不记也罢（P0 修复：原来因
        # fact_keys 整批关联到每段，kept 恒等于全集、dropped 永远 0）。
        submitted = len(fresh)
        kept = len(segments)
        dropped = max(0, submitted - kept)
        result["dropped_by_llm"] = dropped
        result["summary"] = (
            f"{submitted} 条世界观察交给她，她挑了 {kept} 段写进观察日记"
            f"（实际落盘 {written_now} 则，{dropped} 条不记也罢）"
            if dropped
            else f"{submitted} 条世界观察，她全部写进了观察日记（{written_now} 则）"
        )
    except Exception as e:
        logger.warning("observation_diary: 写日记异常: %s", e)
        return result

    return result
