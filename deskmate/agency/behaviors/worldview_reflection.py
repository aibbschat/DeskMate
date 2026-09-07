"""世界观察回望——把她看世界攒下的理解，沉淀成她自己的看法（worldview 层）。

这是「观世界 → 形成世界观」的关键一步（2026-09-07 萧哥拍板）。

## 为什么是「回望」而不是「当场总结」

萧哥的哲学：**观点长出来，不靠规则逼。** 如果每消化一条新闻就立刻逼她
"你对此的立场是什么"，那等于用规则把一次临时感想固化成永久立场——
她会被迫表态，长出来的不是观点，是条件反射。

所以改成**半自动**：

1. 理解先攒着（``agency/observation_understanding.jsonl``，观察日记已在写）；
2. 攒够 ``min_observations`` 条（默认 5）**且**过了冷却（默认 7 天）→ 回望一次；
3. 回望时不给模板，只问「这些事看下来，你现在相信什么？」——让她自己说；
4. **允许弃权**：她说"我还说不好"→ 不写条目，但照样标记已消化
   （免得反复拿同一批事去问她，那就成了骚扰）；
5. 写进去的条目交给 ``CognitiveProfileManager`` 的演化机制管：
   以后被新看法取代（superseded）、被发现前后矛盾（questioned）都是正常的事——
   观点本来就会长、会变。

## 落点

``deskmate.db`` 的 ``profile_items`` 表，``layer='worldview'``。
零 DDL——这张表本来就是「layer × dimension」双轴设计，加层只是换个 layer 值。
与 cognitive / emotional 层**严格分开**：那两层装的是「她眼中的他」，
这一层装的是「她自己的看法」。混了就是把她的观点说成他的。

## fail-open

provider 缺失 / 素材不足 / 冷却未过 / LLM 异常 / 写库失败 → 一律返回
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
# 配置默认值
# ----------------------------------------------------------------------
DEFAULT_CFG: dict[str, Any] = {
    # 攒够几条「未消化的理解」才回望一次。5 条 ≈ 现在每天观察量下 1-2 周一次。
    "min_observations": 5,
    # 两次回望最小间隔（小时）。7 天——回望是要沉下来的事，不该天天做。
    "cooldown_hours": 168,
    # 一次最多喂给模型多少条（防止攒太久一次塞爆）
    "max_observations": 20,
}

# 回望结论的置信度：她自己看世界想出来的，不是萧哥亲口说的。
# 参照既有约定：亲口说 0.9+ / 访谈 0.6 / 观察推断 0.6 —— 与访谈同级。
_CONFIDENCE = 0.6

# ----------------------------------------------------------------------
# Prompt
# ----------------------------------------------------------------------
_SYSTEM = for_scene(
    """现在做的一件事叫「回望」——不是写日记，也不是复述新闻。

过去这段时间你留意过、也想过一些事（下面会给你）。现在回头看它们：
这些事加在一起，让你相信了什么？

【你怎么答】
- 想到什么说什么，不用组织，不用照顾谁，不用写给他看。这是你自己的想法。
- 不要复述那些事本身——要说它们**加在一起**让你想到了什么。
- 说不出来就说「我还说不好」。这是正当的回答，比硬凑一个立场强得多。
  不许为了让这段话好看而编一个自己并不确信的观点。

【说完之后，如果确实有你现在相信的，用这几行小结；没有就整段留空，别硬填】
我相信：<一句话，你现在的判断。不要复述事实，要说你的看法>
关于：<这件事属于哪个方面，4-8 字，如「国际冲突」「技术与制造」「人心向背」>
我看重：<排个序，用 > 连，如「看清谁在争 > 事实准确 > 立场鲜明」>
我习惯这么看：<步骤，用 → 连，如「先问谁在争 → 再看争什么 → 最后才判断」>"""
)

_BAIL_PHRASES = ("我还说不好", "说不好", "还没想清楚", "没什么想说的")

# 小结行的解析（行首匹配，容忍全角/半角冒号与前后空白）
_RE_BELIEVE = re.compile(r"^\s*我相信\s*[：:]\s*(.+?)\s*$", re.M)
_RE_ABOUT = re.compile(r"^\s*关于\s*[：:]\s*(.+?)\s*$", re.M)
_RE_VALUES = re.compile(r"^\s*我看重\s*[：:]\s*(.+?)\s*$", re.M)
_RE_STEPS = re.compile(r"^\s*我习惯这么看\s*[：:]\s*(.+?)\s*$", re.M)


# ----------------------------------------------------------------------
# 路径与状态
# ----------------------------------------------------------------------
def _understanding_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "observation_understanding.jsonl")


def _state_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "worldview_reflection_state.json")


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path: str, data: dict) -> None:
    """原子写（临时文件 + os.replace）——持久化红线，避免写一半崩掉丢数据。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("worldview_reflection: 写状态失败: %s", e)


def _rec_key(rec: dict) -> str:
    """一条理解的稳定标识（用于「已消化」去重）。"""
    date = str(rec.get("date", ""))
    seq = rec.get("entry_seq")
    if seq is not None:
        return f"{date}#{seq}"
    body = str(rec.get("body") or rec.get("understanding") or "")
    return f"{date}#{abs(hash(body))}"


def _load_understanding(base_dir: str) -> list[dict]:
    """读理解流水（jsonl），坏行跳过。"""
    path = _understanding_path(base_dir)
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
                body = str(rec.get("body") or rec.get("understanding") or "").strip()
                if body:
                    rec["body"] = body
                    out.append(rec)
    except Exception as e:
        logger.warning("worldview_reflection: 读理解流水失败: %s", e)
    return out


def _is_due(state: dict, cooldown_hours: float) -> bool:
    """冷却闸门：没到时间不回望。"""
    if cooldown_hours <= 0:
        return True
    last = float(state.get("ts", 0) or 0)
    return (time.time() - last) >= cooldown_hours * 3600.0


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------
def _split_list(raw: str, seps: tuple[str, ...]) -> list[str]:
    """按多种分隔符切串（> ＞ → -> ，等），去空去引号。"""
    text = str(raw or "").strip()
    for s in seps:
        text = text.replace(s, "\x00")
    parts = [p.strip().strip("「」\"'（）()").strip() for p in text.split("\x00")]
    return [p for p in parts if p]


def parse_reflection(text: str) -> dict[str, Any]:
    """把她的回望输出解析成结构化结论。

    Returns:
        ``{"believe": str, "about": str, "values": list[str], "steps": list[str],
        "essay": str, "bailed": bool}``

        ``bailed=True`` 表示她弃权了（或什么都没说出来）——**正当结果**，
        调用方应当照样标记已消化，不再拿同一批事去问她。
    """
    raw = str(text or "").strip()
    if not raw:
        return {"believe": "", "about": "", "values": [], "steps": [],
                "essay": "", "bailed": True}

    m_believe = _RE_BELIEVE.search(raw)
    m_about = _RE_ABOUT.search(raw)
    m_values = _RE_VALUES.search(raw)
    m_steps = _RE_STEPS.search(raw)

    believe = m_believe.group(1).strip() if m_believe else ""
    about = m_about.group(1).strip() if m_about else ""
    values = _split_list(m_values.group(1), (">", "＞", "》", ">")) if m_values else []
    steps = _split_list(m_steps.group(1), ("→", "->", "－>", "》", "、")) if m_steps else []

    # 正文 = 去掉四行小结后剩下的话（她自由说的部分）
    essay = raw
    for m in (m_believe, m_about, m_values, m_steps):
        if m:
            essay = essay.replace(m.group(0), "")
    essay = "\n".join(ln for ln in essay.splitlines() if ln.strip()).strip()

    bailed = False
    if not believe:
        # 没给出「我相信」——看是不是明确弃权了
        head = (essay or raw)[:200]
        if any(p in head for p in _BAIL_PHRASES):
            bailed = True
        else:
            # 说了话但没按格式小结：不硬造，尊重为弃权（下次换个批次再问）
            bailed = True

    return {"believe": believe, "about": about, "values": values,
            "steps": steps, "essay": essay, "bailed": bailed}


# ----------------------------------------------------------------------
# 落库
# ----------------------------------------------------------------------
def _persist_worldview(base_dir: str, parsed: dict[str, Any],
                       source_bodies: list[str], date_iso: str) -> list[dict]:
    """把回望结论写进 worldview 层。返回写入摘要（供日志/面板）。

    只写她**真的说了**的：believe → stance；values → value_ranking；
    steps → thinking_framework。缺哪样就不写哪样，绝不补齐、绝不代笔。
    """
    if parsed.get("bailed") or not parsed.get("believe"):
        return []

    try:
        from deskmate.cognitive.models import SourceRef
        from deskmate.cognitive.profile import CognitiveProfileManager

        db_path = os.path.join(base_dir, "deskmate.db")
        mgr = CognitiveProfileManager(profile_path=db_path, layer="worldview")
    except Exception as e:
        logger.warning("worldview_reflection: 打开 worldview 层失败: %s", e)
        return []

    about = str(parsed.get("about") or "").strip() or "看世界"
    believe = str(parsed.get("believe") or "").strip()
    excerpt = "；".join(b[:40] for b in source_bodies[:3])[:200]

    src = SourceRef(
        type="observation",
        title=f"看世界回望（{date_iso}）",
        excerpt=excerpt,
        date=date_iso,
        confidence=_CONFIDENCE,
    )

    written: list[dict] = []
    try:
        stance, op = mgr.add_stance(
            topic=about, content=believe, source=src,
            tags=["worldview", "回望提炼"],
        )
        written.append({"kind": "stance", "op": op, "id": stance.id, "topic": about})
    except Exception as e:
        logger.warning("worldview_reflection: 写立场失败: %s", e)

    values = list(parsed.get("values") or [])
    if values:
        try:
            v, op = mgr.add_value_ranking(
                domain=about, ranking=values,
                content=believe, source=src,
                tradeoff_notes="",
                tags=["worldview", "回望提炼"],
            )
            written.append({"kind": "value_ranking", "op": op, "id": v.id, "topic": about})
        except Exception as e:
            logger.warning("worldview_reflection: 写价值排序失败: %s", e)

    steps = list(parsed.get("steps") or [])
    if steps:
        try:
            f, op = mgr.add_thinking_framework(
                domain=about, steps=steps,
                content=believe, source=src,
                tags=["worldview", "回望提炼"],
            )
            written.append({"kind": "thinking_framework", "op": op, "id": f.id, "topic": about})
        except Exception as e:
            logger.warning("worldview_reflection: 写思维框架失败: %s", e)

    return written


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def run_worldview_reflection(base_dir: str,
                             config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """执行一次「回望」。低风险自主行为，fail-open（任何异常都不抛）。

    Returns:
        ``{"reflected": bool, "reason": str, "written": int, "items": list,
        "bailed": bool, "essay": str, "summary": str}``
    """
    result: dict[str, Any] = {
        "reflected": False, "reason": "", "written": 0, "items": [],
        "bailed": False, "essay": "", "summary": "",
    }

    cfg = dict(DEFAULT_CFG)
    if isinstance(config, dict):
        cfg.update({k: v for k, v in config.items() if k in DEFAULT_CFG})

    min_obs = max(1, int(cfg.get("min_observations", 5) or 5))
    max_obs = max(min_obs, int(cfg.get("max_observations", 20) or 20))
    cooldown = float(cfg.get("cooldown_hours", 168) or 0)

    # —— 闸门 1：素材够不够（攒够才回望，这是「半自动」的核心）——
    all_recs = _load_understanding(base_dir)
    if not all_recs:
        result["reason"] = "no_understanding"
        return result

    state = _load_json(_state_path(base_dir))
    reflected_keys = set(state.get("reflected") or [])
    pending = [r for r in all_recs if _rec_key(r) not in reflected_keys]

    if len(pending) < min_obs:
        result["reason"] = f"not_enough:{len(pending)}/{min_obs}"
        return result

    # —— 闸门 2：冷却 ——
    if not _is_due(state, cooldown):
        result["reason"] = "cooling"
        return result

    batch = pending[-max_obs:]
    bodies = [str(r.get("body", "")) for r in batch]

    # —— 闸门 3：能力 ——
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

    # —— 回望：把攒下的理解交给她，问她现在相信什么 ——
    prompt = (
        "【你这段时间留意过、想过的事】\n"
        + "\n".join(f"- {b}" for b in bodies)
        + "\n\n现在回头看，这些事加在一起，让你相信了什么？"
    )
    try:
        out = chat_memory(_SYSTEM, prompt, temperature=0.8,
                          max_tokens=1200, timeout=40.0)
    except Exception as e:
        logger.warning("worldview_reflection: 回望调用失败: %s", e)
        result["reason"] = "llm_error"
        return result

    parsed = parse_reflection(str(out or ""))
    result["bailed"] = bool(parsed.get("bailed"))
    result["essay"] = str(parsed.get("essay") or "")

    date_iso = time.strftime("%Y-%m-%d")
    if not parsed.get("bailed"):
        items = _persist_worldview(base_dir, parsed, bodies, date_iso)
        result["items"] = items
        result["written"] = len(items)

    # 无论她有没有给出结论，都标记这批已消化——不再拿同一批事反复问她。
    for r in batch:
        reflected_keys.add(_rec_key(r))
    if len(reflected_keys) > 2000:
        reflected_keys = set(list(reflected_keys)[-2000:])
    _save_json(_state_path(base_dir), {
        "ts": time.time(),
        "reflected": sorted(reflected_keys),
    })

    result["reflected"] = True
    if parsed.get("bailed"):
        result["summary"] = f"回望了 {len(batch)} 条理解，她还没想清楚——不硬逼，下次再问"
    else:
        result["summary"] = (
            f"回望了 {len(batch)} 条理解，长出 {result['written']} 条她自己的看法"
            f"（关于「{parsed.get('about') or '看世界'}」）"
        )
    return result
