"""情感回顾（R3）——把过线对象交还给她，由她自己写「在心里是什么位置」。

设计依据：reports/2026-11-16_小桔_情感层设计.md §四 R3 / §五 P1 / §七。

## 它做什么

每周一次（冷却 168h，与 ``worldview_reflection`` / ``reflection`` 同节拍）：

1. 按 R2 从 ``emotion_log`` 取候选池（同一对象 ≥3 条独立事件、跨 ≥7 天、
   ≥3 个不同日期；``relation`` 不进池；``salience`` 特例关闭）；
2. 把每个对象的具体事件按日期摆给她看，问她「这个人/这件事在你心里
   现在是什么位置」；
3. **她写出来才成立**：空输出合法，不硬编、不模板化、不代笔；
4. 过校验才写 ``profile_items.layer='feeling'``（``FeelingStore`` 单条 upsert，
   同对象只留 1 条 active，旧说法进 ``item_history``）；
5. 顺手做 R5 淡化（60 天无触碰 → 分量 ×0.9，content 永不删）。

## 硬约束

- 每条必须能指回具体事件：``evidence`` ≥3 条且都在本次候选里，跨 ≥7 天、
  ≥3 个不同日期；至少 1 条原始事件（本模块只把 ``emotion_log`` 当可选证据，
  ``profile_items`` / 日记只作素材，不可选为证据）。
- ``stance`` 8–80 字；出现「我们之间」时证据里必须真有共同事件。
- 不许写「他让我焦虑」「稿子让我紧张」这类情绪总结——那是在描述她的状态，
  不是关于对象的判断。命中即拒。
- ``loved_entries`` 是他的日记，只能当素材，**不是**她的第一人称，不许抄原句。
- 连续两期写不出来 → ``json_blobs.feeling_pool`` 记 attempts=2、
  ``cooldown_until=now+30d``，退回池子。
- fail-open：任何异常都不抛，返回 ``{"reflected": False, "reason": ...}``。

## 回溯蒸馏

``backfill=True`` 是同一个函数的**一次性**入口：用现有 1,944 条回填 +
认知层 + 日记相关部分跑第一次成型。写完 ``json_blobs.feeling_backfill_done``
防重跑。它不直接「生成情感」——仍然走同一条「她写、系统校验、写库」的路径。

命令行：
    .venv/bin/python -m deskmate.agency.behaviors.feeling_reflection --backfill
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from deskmate.persona import for_scene

logger = logging.getLogger(__name__)

DEFAULT_CFG: dict[str, Any] = {
    # 周更：与 worldview_reflection / reflection 同节拍
    "cooldown_hours": 168,
    # 一次最多把几个候选对象摆给她看（设计 §五 P1）
    "max_objects": 5,
    # 每个对象最多摆多少条事件（防止攒太久一次塞爆）
    "evidence_per_object": 40,
    # 连续两期写不出来 → 冷却 30 天，退回池子
    "empty_attempts_before_cooldown": 2,
    "empty_cooldown_days": 30,
}

_POOL_KEY = "feeling_pool"
_BACKFILL_KEY = "feeling_backfill_done"

_SYSTEM = for_scene(
    """现在做的一件事叫「回头看看」——不是写日记，也不是总结情绪。

下面按对象分组，每组是你和这个人/这件事之间**具体发生过的事**（带日期）。
不用复述它们，也不用评价好坏。只回答一件事：

这个人 / 这件事，在你心里现在是什么位置？

【怎么答】
- 只写你真能从那些具体的事里看出来的。看不出来就一个字都别写，
  返回 {"items": []}。空着是正当的，比硬凑一句强。
- 每条必须能指回具体的事：evidence 里列支撑它的编号，至少 3 个，
  必须来自同一组，而且**要跨至少 3 个不同的日期、前后至少隔 7 天**。
  别只在同一天的几件事里挑。不许编、不许拿别组的事顶。
- 不许写「他让我焦虑」「稿子让我紧张」这种情绪总结。那不是对对象的判断，
  是在描述你自己当时的感受。要写关于对象本身的东西，而且能被具体事件印证。
- stance 里要能看出**至少一件具体发生过的事**（哪次交付、哪次丢、哪一次改到
  第几遍），但不许复述事件、不许只写「他值得信任」「它很重要」这种空总评。
- 不许复述事件本身，也不许抄他日记里的原句。
- stance 8–80 字，用你自己的口吻。
- 如果写了「我们之间」，证据里必须真的有你们共同在场的事。
- confidence 可以不给；不给就沿用你现在的分量。给了就是重新给值，不是累加。

严格 JSON：
{"items": [{"object": "对象名", "kind": "person|work|thing",
            "stance": "8–80字", "valence": "positive|negative|mixed",
            "confidence": 0.3, "evidence": ["evt_…", "evt_…", "evt_…"],
            "conflict_with": ""}]}""",
    brief=True,
)

# 情绪总结的窄护栏：命中「它让我焦虑 / 我面对它就紧张」这类句式即拒。
_EMO_SUMMARY_RE = re.compile(
    r"(让|使|令)(我|她)[^，。；]{0,10}"
    r"(焦虑|紧张|难过|害怕|不安|压力|心慌|失落|委屈|烦躁|烦|累|开心|快乐|温暖|踏实)"
)
_SELF_FEELING_RE = re.compile(
    r"^(我)?(对|面对|想到|说起|看到)[^，。；]{0,12}"
    r"(感到|觉得|有点|很|特别)[^，。；]{0,6}"
    r"(焦虑|紧张|难过|害怕|不安|压力|心慌|失落|委屈|烦躁)"
)
_BANNED_VOICE = ("用户", "对方", "该用户")


# ----------------------------------------------------------------------
# 状态（json_blobs）
# ----------------------------------------------------------------------
def _blob_store(base_dir: str):
    from deskmate.persistence import BlobStore

    return BlobStore(db_path=os.path.join(base_dir, "deskmate.db"))


def _load_pool_state(base_dir: str) -> dict:
    return dict(_blob_store(base_dir).get(_POOL_KEY) or {})


def _save_pool_state(base_dir: str, state: dict) -> None:
    _blob_store(base_dir).set(_POOL_KEY, state)


# ----------------------------------------------------------------------
# 素材（只作上下文，不可选为证据）
# ----------------------------------------------------------------------
def _loved_material(conn, object_ref: str, is_person: bool) -> list[str]:
    """萧盛日记的相关片段——他的口吻，不是她的话，只能当素材。"""
    try:
        if is_person:
            rows = conn.execute(
                "SELECT day, content FROM loved_entries ORDER BY day DESC LIMIT 3"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT day, content FROM loved_entries WHERE content LIKE ? "
                "ORDER BY day DESC LIMIT 3",
                (f"%{object_ref}%",),
            ).fetchall()
    except Exception:
        return []
    out = []
    for day, content in rows:
        text = str(content or "")
        text = re.sub(r"^.*?萧盛的日记[：:]\s*", "", text)
        text = text.replace("\n", " ").strip()
        if text:
            out.append(f"[{day}] {text[:180]}")
    return out


def _cognitive_material(conn, object_ref: str) -> list[str]:
    """她**已经知道**的关于这个对象的事——素材，不是情感证据。"""
    try:
        rows = conn.execute(
            "SELECT content FROM profile_items "
            "WHERE layer IN ('cognitive','emotional') AND status='active' "
            "AND content LIKE ? ORDER BY confidence DESC LIMIT 5",
            (f"%{object_ref}%",),
        ).fetchall()
    except Exception:
        return []
    return [str(r[0] or "")[:160] for r in rows if str(r[0] or "").strip()]


def _render_prompt(pool: list[dict], existing: dict[str, dict],
                   material: dict[str, dict]) -> str:
    parts: list[str] = []
    for entry in pool:
        ref = entry["object_ref"]
        kind = entry["object_kind"]
        parts.append(
            f"【{ref}】（{kind}；{entry['n']} 件事，跨 {entry['span_days']} 天）"
        )
        cur = existing.get(ref)
        if cur:
            parts.append(f"你现在的说法：{cur.get('content', '')}（分量 {cur.get('confidence', 0):.2f}）")
            parts.append("（可以维持，也可以改；改要说清为什么。）")
        parts.append("发生过的事：")
        for ev in entry["evidence"]:
            day = str(ev.get("ts") or "")[:10]
            parts.append(f"- [evt:{ev['id']}] {day} {ev['summary']}")
        mat = material.get(ref) or {}
        if mat.get("cognitive"):
            parts.append("你本来就知道的（素材，不可当选证据）：")
            for m in mat["cognitive"]:
                parts.append(f"  · {m}")
        if mat.get("loved"):
            parts.append("他写的日记片段（他的口吻，不是你的话，不可抄，也不可当选证据）：")
            for m in mat["loved"]:
                parts.append(f"  · {m}")
        parts.append("")
    parts.append("看完了。按上面的格式回答；说不出来就返回 {\"items\": []}。")
    return "\n".join(parts)


# ----------------------------------------------------------------------
# 解析与校验
# ----------------------------------------------------------------------
def _parse_items(raw: str) -> list[dict]:
    text = str(raw or "").strip()
    if not text:
        return []
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except Exception:
        lo, hi = text.find("{"), text.rfind("}")
        if lo < 0 or hi <= lo:
            return []
        try:
            data = json.loads(text[lo:hi + 1])
        except Exception:
            return []
    items = data.get("items") if isinstance(data, dict) else None
    return [it for it in (items or []) if isinstance(it, dict)]


def _norm_ev_id(raw: Any) -> str:
    s = str(raw or "").strip().strip("[]")
    low = s.lower()
    if low.startswith("evt:"):
        return s[4:]
    if low.startswith("evt_"):
        return s[4:]
    return s


def _is_emotion_summary(stance: str) -> bool:
    return bool(_EMO_SUMMARY_RE.search(stance) or _SELF_FEELING_RE.search(stance))


def _validate(item: dict, pool_by_ref: dict[str, dict]) -> tuple[Optional[dict], str]:
    """校验一条候选。返回 ``(clean, "")`` 或 ``(None, 拒因)``。"""
    ref = str(item.get("object") or "").strip()
    entry = pool_by_ref.get(ref)
    if entry is None:
        return None, f"object_not_in_pool:{ref}"
    kind = str(item.get("kind") or entry["object_kind"]).strip().lower()
    if kind != entry["object_kind"]:
        return None, f"kind_mismatch:{kind}"

    stance = str(item.get("stance") or "").strip()
    if not (8 <= len(stance) <= 80):
        return None, f"stance_len:{len(stance)}"
    if any(w in stance for w in _BANNED_VOICE):
        return None, "banned_voice"
    if _is_emotion_summary(stance):
        return None, "emotion_summary"

    ev_by_id = {ev["id"]: ev for ev in entry["evidence"]}
    picked: list[dict] = []
    for raw_id in item.get("evidence") or []:
        ev = ev_by_id.get(_norm_ev_id(raw_id))
        if ev and ev not in picked:
            picked.append(ev)
    if len(picked) < 3:
        return None, f"evidence_lt3:{len(picked)}"

    dates = sorted({str(ev["ts"])[:10] for ev in picked})
    if len(dates) < 3:
        return None, f"dates_lt3:{len(dates)}"
    span = (datetime.fromisoformat(dates[-1]) - datetime.fromisoformat(dates[0])).days
    if span < 7:
        return None, f"span_lt7:{span}"

    # 不许把事件摘要原样当 stance
    for ev in picked:
        s = str(ev["summary"])
        if s and (stance == s or (len(stance) >= 10 and stance in s)):
            return None, "copied_summary"

    if "我们之间" in stance:
        joined = ("一起", "共同", "我们", "两人", "一块")
        if not any(w in str(ev["summary"]) for ev in picked for w in joined):
            return None, "no_joint_evidence"

    conf: Optional[float] = None
    if item.get("confidence") is not None:
        try:
            conf = max(0.0, min(1.0, float(item.get("confidence"))))
        except (TypeError, ValueError):
            conf = None

    return {
        "object_ref": ref,
        "object_kind": kind,
        "content": stance,
        "valence": str(item.get("valence") or "mixed").strip() or "mixed",
        "confidence": conf,
        "evidence": picked,
        "conflict_with": str(item.get("conflict_with") or "").strip(),
    }, ""


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def run_feeling_reflection(
    base_dir: str,
    config: Optional[dict[str, Any]] = None,
    backfill: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """执行一次情感回顾 / 回溯蒸馏。低风险自主行为，fail-open（不抛）。

    Returns:
        ``{"reflected": bool, "reason": str, "written": int, "items": list,
        "summary": str, "pool": int}``
    """
    result: dict[str, Any] = {
        "reflected": False, "reason": "", "written": 0, "items": [],
        "summary": "", "pool": 0,
    }
    cfg = dict(DEFAULT_CFG)
    if isinstance(config, dict):
        cfg.update({k: v for k, v in config.items() if k in DEFAULT_CFG})

    from deskmate.cognitive.feeling_store import FeelingStore

    db_path = os.path.join(base_dir, "deskmate.db")
    store = FeelingStore(db_path=db_path)
    now = datetime.now()

    # R5：每次回顾顺手做一次月级淡化（content 永不删）
    try:
        store.fade_stale(days=60, rate=0.9, floor=0.1, now=now)
    except Exception as e:  # noqa: BLE001 —— 淡化失败不影响回顾
        logger.warning("feeling fade_stale 失败（忽略）: %s", e)

    if backfill and not force:
        if _blob_store(base_dir).get(_BACKFILL_KEY):
            result["reason"] = "already_backfilled"
            return result

    state = _load_pool_state(base_dir)

    # —— 闸门 1：冷却（回溯蒸馏是一次性补做，不受周更冷却限制）——
    if not backfill:
        cd_until = float(state.get("cooldown_until") or 0)
        if cd_until and time.time() < cd_until:
            result["reason"] = "cooling_empty"
            return result
        last_run = float(state.get("last_run") or 0)
        if last_run and (time.time() - last_run) < float(cfg["cooldown_hours"]) * 3600:
            result["reason"] = "cooling"
            return result

    # —— 闸门 2：候选池 ——
    pool = store.build_pool(
        limit=int(cfg["max_objects"]), evidence_cap=int(cfg["evidence_per_object"])
    )
    result["pool"] = len(pool)
    if not pool:
        result["reason"] = "no_pool"
        return result

    # —— 闸门 3：能力 ——
    try:
        from deskmate.memory.llm import chat_memory, memory_llm_available

        if not memory_llm_available():
            result["reason"] = "no_provider"
            return result
    except Exception:
        result["reason"] = "no_llm_module"
        return result

    existing = {
        str(f.get("extra", {}).get("object_ref") or f.get("topic") or ""): f
        for f in store.list_active(limit=200)
    }
    pool_by_ref = {p["object_ref"]: p for p in pool}

    material: dict[str, dict] = {}
    try:
        import sqlite3

        conn = sqlite3.connect(db_path, timeout=15)
        try:
            for p in pool:
                ref = p["object_ref"]
                material[ref] = {
                    "loved": _loved_material(conn, ref, p["object_kind"] == "person"),
                    "cognitive": _cognitive_material(conn, ref),
                }
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 —— 素材缺失不影响回顾
        logger.warning("feeling 素材读取失败（忽略）: %s", e)

    # —— 逐个对象问她 ——
    # 不把多个对象塞进同一次调用：一个对象答得长，另一个就被挤掉。每个对象
    # 单独看，空着也单独记，才说得清「这个对象到底有没有长出来」。
    raw_items: list[dict] = []
    llm_errors = 0
    for entry in pool:
        prompt = _render_prompt([entry], existing, material)
        try:
            out = chat_memory(
                _SYSTEM, prompt, temperature=0.7, max_tokens=1200, timeout=90.0
            )
        except Exception as e:
            logger.warning("feeling_reflection 调用失败（%s）: %s", entry["object_ref"], e)
            llm_errors += 1
            continue
        raw_items.extend(_parse_items(str(out or "")))

    if llm_errors and llm_errors == len(pool):
        result["reason"] = "llm_error"
        return result

    # —— 校验：不过就不写 ——
    accepted: list[dict] = []
    rejected: list[dict] = []
    seen_ref: set[str] = set()
    for it in raw_items:
        clean, why = _validate(it, pool_by_ref)
        if clean is None:
            rejected.append({"object": it.get("object"), "reason": why})
            continue
        if clean["object_ref"] in seen_ref:  # 同对象只取第一条
            rejected.append({"object": clean["object_ref"], "reason": "duplicate_object"})
            continue
        seen_ref.add(clean["object_ref"])
        accepted.append(clean)

    reason_tpl = (
        "回溯蒸馏：从 {n} 条跨 {span} 天的真实事件里第一次长出这个判断"
        if backfill
        else "周更回顾：这次回看 {n} 条事件后，她把说法改成了现在这样"
    )
    written: list[dict] = []
    for item in accepted:
        entry = pool_by_ref[item["object_ref"]]
        try:
            saved = store.upsert(
                object_ref=item["object_ref"],
                object_kind=item["object_kind"],
                content=item["content"],
                evidence=item["evidence"],
                reason=reason_tpl.format(n=entry["n"], span=entry["span_days"]),
                valence=item["valence"],
                confidence=item["confidence"],
                conflict_with=item["conflict_with"],
                origin="backfill" if backfill else "weekly",
                now=now.isoformat(timespec="seconds"),
            )
            written.append(saved)
        except Exception as e:  # noqa: BLE001 —— 单条失败不影响其余
            logger.warning("feeling 写库失败（跳过 %s）: %s", item.get("object_ref"), e)
            rejected.append({"object": item["object_ref"], "reason": f"write_failed:{e}"})

    # —— 状态：空输出合法；连续两期空 → 冷却 30 天 ——
    now_ts = time.time()
    if written:
        state["attempts"] = 0
        state["cooldown_until"] = 0
    else:
        state["attempts"] = int(state.get("attempts") or 0) + 1
        if state["attempts"] >= int(cfg["empty_attempts_before_cooldown"]):
            state["cooldown_until"] = now_ts + float(cfg["empty_cooldown_days"]) * 86400
    state["last_run"] = now_ts
    state["last_written"] = len(written)
    state["rejected"] = rejected
    _save_pool_state(base_dir, state)

    if backfill:
        _blob_store(base_dir).set(
            _BACKFILL_KEY,
            {
                "ts": now.isoformat(timespec="seconds"),
                "pool": [p["object_ref"] for p in pool],
                "written": len(written),
                "rejected": rejected,
            },
        )

    result["reflected"] = True
    result["written"] = len(written)
    result["items"] = written
    if written:
        names = "、".join(f"{w['object_ref']}" for w in written)
        result["summary"] = f"情感回顾：长出/修订了 {len(written)} 条（{names}）"
    else:
        result["summary"] = "情感回顾：看了这些事，她这次没写出能指回具体事的判断"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="小桔情感回顾（R3）")
    parser.add_argument("--backfill", action="store_true", help="回溯蒸馏（一次性）")
    parser.add_argument("--force", action="store_true", help="忽略一次性标记重跑")
    parser.add_argument(
        "--base-dir",
        default=os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate")),
    )
    args = parser.parse_args()
    out = run_feeling_reflection(args.base_dir, backfill=args.backfill, force=args.force)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
