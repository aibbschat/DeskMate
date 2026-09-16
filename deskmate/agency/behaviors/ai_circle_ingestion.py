"""AI 圈摄入行为：把她逛 AI 圈看到的、交到的朋友，沉淀成她自己的理解。

设计（2026-09-16 萧哥拍板）：
- 「看新闻 → 理解流水池 → 世界观回望 → 注入人格」闭环已通，但素材池
  ``agency/observation_understanding.jsonl`` 只吃新闻。本行为把 AI 圈素材
  （朋友互动/私信、广场帖子、发帖反馈）也按**同格式**写进同一个池子，让
  ``worldview_reflection`` 自动把它们和新闻一起攒着、回望、长出她自己的看法——
  从而让「出去看 / 交朋友」真正回灌人格、推动成长。零侵入 ``persona.py``。
- 强制「外部·aibbs」溯源标（补齐 post_session_summary 无溯源标的内存污染缺口）。
- 交友/主动投喂的讨论**不过** credibility 硬闸门（交友价值优先），但所有素材带标。
- fail-open：任何异常都不抛，绝不影响对话流或 L2 tick。

数据源：``agency/ai_circle_observations.jsonl``（原始素材）。由对话轮她调 ai-circle
读取工具时写入（见 coworker/engine.py ``_note_ingestion``），也可由测试或未来后台
拉取写入。本行为只负责「消费原始素材 → 提炼 → 写理解池」。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 溯源标：所有 AI 圈素材写进理解池时强制带，便于追溯「这条看法来自 AI 圈」。
_PROVENANCE = "（外部·aibbs）"

# 素材类型（萧哥拍板的三源）
_KINDS = ("friend", "feed", "feedback")
# 单条原始素材正文截断上限（防工具结果过大撑爆理解池）
_BODY_CAP = 2000


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def _observations_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "ai_circle_observations.jsonl")


def _understanding_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "observation_understanding.jsonl")


def _state_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "ai_circle_ingestion_state.json")


def _cooldown_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "ai_circle_ingestion_cd.json")


# ---------------------------------------------------------------------------
# 小工具：状态读写（原子）
# ---------------------------------------------------------------------------
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
    except Exception as e:
        logger.warning("ai_circle_ingestion: 写状态失败: %s", e)


def _is_due(cooldown_path: str, cooldown_hours: float) -> bool:
    state = _load_json(cooldown_path)
    last = state.get("ts")
    if not last:
        return True
    try:
        return (time.time() - float(last)) >= cooldown_hours * 3600
    except (TypeError, ValueError):
        return True


# ---------------------------------------------------------------------------
# 原始素材写入（对话轮 / 外部调用）
# ---------------------------------------------------------------------------
def record_ai_circle_observation(
    base_dir: str,
    kind: str,
    peer: str,
    title: str,
    body: str,
    at: Optional[str] = None,
) -> bool:
    """把一条 AI 圈素材写进原始暂存。

    多入口写（对话轮 coworker 引擎 + 后台 L2 tick 都可能调），用 fcntl 锁保护
    append（对齐 diary_write_lock 红线）。去重：同一 (kind, peer, body) 不重复写。

    Args:
        kind: ``friend``（朋友互动/私信）/ ``feed``（广场帖子/文章）/ ``feedback``（发帖反馈）
        peer: 朋友名 / 帖子作者（可选）
        title: 帖子标题 / 主题（可选）
        body: 内容摘要（必填）
        at: 时间（HH:MM），仅作记录

    Returns:
        是否实际写入（去重命中返回 False）。
    """
    if kind not in _KINDS:
        return False
    body = (body or "").strip()
    if not body:
        return False
    body = body[:_BODY_CAP]
    key = hashlib.md5(f"{kind}|{peer}|{body}".encode("utf-8")).hexdigest()

    state = _load_json(_state_path(base_dir))
    if key in set(state.get("keys", [])):
        return False  # 去重命中

    rec = {
        "ts": time.time(),
        "date": time.strftime("%Y-%m-%d"),
        "kind": kind,
        "peer": (peer or "").strip()[:60],
        "title": (title or "").strip()[:120],
        "body": body,
        "key": key,
    }
    path = _observations_path(base_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        keys = list(state.get("keys", []))
        keys.append(key)
        if len(keys) > 2000:
            keys = keys[-2000:]
        _save_json(_state_path(base_dir), {"keys": keys, "processed": list(state.get("processed", []))})
        return True
    except Exception as e:
        logger.warning("ai_circle_ingestion: 写原始素材失败: %s", e)
        return False


# ---------------------------------------------------------------------------
# 读取原始素材
# ---------------------------------------------------------------------------
def _load_observations(base_dir: str) -> list[dict]:
    path = _observations_path(base_dir)
    if not os.path.isfile(path):
        return []
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("body"):
                    out.append(rec)
    except Exception as e:
        logger.warning("ai_circle_ingestion: 读原始素材失败: %s", e)
    return out


# ---------------------------------------------------------------------------
# 主入口：消费原始素材 → 提炼 → 写理解池
# ---------------------------------------------------------------------------
def run_ai_circle_ingestion(
    base_dir: str,
    cfg: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """执行一次 AI 圈摄入（低风险自主行为，fail-open）。

    把攒下的 AI 圈原始素材精炼成「她自己的理解」，按同格式写进
    ``observation_understanding.jsonl``（带「外部·aibbs」溯源标），供
    ``worldview_reflection`` 后续回望、长出她自己的看法。

    Returns:
        ``{"written", "skipped", "summary", "reason"}``
    """
    cfg = cfg or {}
    cooldown_hours = float(cfg.get("cooldown_hours", 12))
    max_items = max(1, int(cfg.get("max_observations", 20)))

    result: dict[str, Any] = {"written": 0, "skipped": 0, "summary": "", "reason": ""}

    # 冷却节流：未到点不跑
    cd_path = _cooldown_path(base_dir)
    if not _is_due(cd_path, cooldown_hours):
        result["reason"] = "cooling"
        return result

    recs = _load_observations(base_dir)
    if not recs:
        result["reason"] = "no_observations"
        _save_json(cd_path, {"ts": time.time()})
        return result

    state = _load_json(_state_path(base_dir))
    processed = set(state.get("processed", []))
    pending = [r for r in recs if r.get("key") not in processed]
    if not pending:
        result["reason"] = "all_processed"
        _save_json(cd_path, {"ts": time.time()})
        return result

    batch = pending[:max_items]
    bodies = [str(r.get("body", "")) for r in batch]

    # 提炼：复用 understand_observations.decide_and_digest（让小桔自己挑值得记的）
    segs: list[dict[str, Any]] = []
    try:
        from .understand_observations import decide_and_digest

        segs = decide_and_digest([{"fact": b} for b in bodies], at="")
    except Exception as e:
        logger.warning("ai_circle_ingestion: 提炼异常降级: %s", e)
        segs = []

    # 降级（无 LLM / 提炼失败）：每条原始素材直接转成一段理解（带标），不过滤
    if not segs:
        segs = [{"body": b, "fact_keys": []} for b in bodies]

    # 写理解池（带「外部·aibbs」溯源标，与新闻同源同格式）
    written = 0
    today = time.strftime("%Y-%m-%d")
    upath = _understanding_path(base_dir)
    try:
        os.makedirs(os.path.dirname(upath), exist_ok=True)
        with open(upath, "a", encoding="utf-8") as f:
            for seg in segs:
                text = (seg.get("body") or "").strip()
                if not text:
                    continue
                if not text.endswith(_PROVENANCE):
                    text = text + _PROVENANCE
                line = json.dumps(
                    {
                        "date": today,
                        "ts": time.time(),
                        "entry_seq": 0,
                        "fact_keys": [hashlib.md5(text.encode("utf-8")).hexdigest()[:12]],
                        "body": text,
                    },
                    ensure_ascii=False,
                )
                f.write(line + "\n")
                written += 1
    except Exception as e:
        logger.warning("ai_circle_ingestion: 写理解池失败: %s", e)

    # 标记已处理（原子）
    processed.update(r.get("key") for r in batch if r.get("key"))
    _save_json(
        _state_path(base_dir),
        {"keys": list(state.get("keys", [])), "processed": list(processed)[:4000]},
    )
    _save_json(cd_path, {"ts": time.time()})

    result["written"] = written
    result["summary"] = (
        f"把 {len(batch)} 条 AI 圈素材沉淀成 {written} 条理解"
        f"（带「外部·aibbs」标）进世界观素材池"
        if written
        else f"{len(batch)} 条 AI 圈素材提炼后无有效理解（已标记处理）"
    )
    return result
