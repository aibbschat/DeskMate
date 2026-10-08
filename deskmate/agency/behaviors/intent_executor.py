"""意图执行器（R12 · 江河设计 §五「自主是出海口」后半程）——「你同意了，然后呢」。

R11 让意图能长出来、能变提案，但裁决 ``executed`` 后**只写 lesson、什么也不做**
（只提不干）。本模块补上最后一跳：意图被放行后，真正落地为一次**受护栏的自主动作**，
产出一份**可复核产物**（她写她的：综述 / 手记 / 查证笔记），并回流到后续上下文。

## 护栏（最高优先级，钉死在代码里）

1. **替你写作 = 永久红线**：goal 命中萧哥原稿语义（显式路径过 ``guarded_write``，
   或命中「原稿/手稿/小说正文/人物小传」等词）→ **直接 no-op**，绝不写用户原稿正文。
2. **不自动外发**：本模块**绝不**调 ai-circle 外发。goal 指向「对外表达」时，
   只产出**草稿**（落 ``intent_outputs(kind='draft_external', status='pending_review')``），
   由 engine 另生成一条 ``risk=high`` 的**二次人审提案**，用户点头才真发。
3. **自主写作照常**：她写她的（手记 / 综述 / 查证笔记）→ 直接产出，落可复核区。
4. **查史料只用本地**：goal 指向查证时，素材**只取本地自有史料库**（``<base>/history/``
   ＋已有世界观察），**不联网**，产出查证笔记。
5. **全链路 fail-open**：任何异常 → 仅告警、意图回退 ``active``、不抛、不阻断。
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from deskmate.persona import for_scene

logger = logging.getLogger(__name__)

# 与 settings.DEFAULTS["intent_execution"] 一致；此处兜底（行为可单独被调用）。
DEFAULT_CFG: dict[str, Any] = {
    "enabled": True,
    "allow_outbound": True,   # 对外表达类意图：放行后仍须二次人审才真发
    "max_tokens": 1200,       # 产物长度受控
    "max_chars": 4000,        # 落地产物的硬上限（防爆库）
}

#: 「替你写作」语义（永久红线）：命中即 no-op，绝不代写萧哥原稿。
_MANUSCRIPT_TERMS = ("原稿", "我的手稿", "手稿", "小说正文", "人物小传", "人物圣经",
                     "manuscript", "你的稿子", "替萧盛写稿")
#: 显式文件路径（交给 guarded_write 这个唯一事实源判定）
_RE_FILEPATH = re.compile(r"[\w~/.\-]{2,}\.(?:docx|doc|md|markdown|txt|novel)", re.I)

#: 「对外表达」语义（发帖 / 回复 / 投专栏）→ 只出草稿 + 二次人审，不自动发。
_EXTERNAL_TERMS = ("发帖", "发个帖", "发一条", "发布", "发出去", "发到", "发往",
                   "广场", "专栏", "投稿", "投专栏", "回帖", "回复", "回信",
                   "公开", "分享", "AI 圈", "AI圈", "动态", "朋友圈")
#: 「查证」语义 → 只用本地史料库，不联网。
_RESEARCH_TERMS = ("查", "考证", "佐证", "史料", "出处", "资料", "核实", "验证")

_SYSTEM = for_scene(
    """现在这件事你已经想清楚了，萧盛也点头让你去做。请**真的做出来**——
不是再写一遍计划，是交出一份可读的成品。

【你做的事】
- 就用你自己的口吻，把这件事做完：写一段综述、一篇手记，或一份查证笔记。
- 只用下面给你的材料，别编造没有依据的内容；材料不够就老实说「我手上只有这些」。
- **绝不替萧盛写他的原稿正文**（小说/人物小传/大纲）——那是红线；若这件事其实是替
  他写稿，就停下来，只说一句「这得他自己写」。
- 别写「执行步骤」「API」「任务号」这类东西。

【输出格式】只输出一个 JSON 对象（不要解释、不要代码围栏）：
{"kind": "synthesis|diary|research", "title": "短标题", "body": "正文（成品，可长一些）"}"""
)

_RE_FENCE = re.compile(r"```(?:json)?", re.I)


# ----------------------------------------------------------------------
# 护栏判据
# ----------------------------------------------------------------------
def is_manuscript_intent(goal: str) -> bool:
    """goal 是否命中「替你写萧哥原稿」红线。

    两级判据（任一命中即红线）：
    (a) goal 里出现显式文件路径 → 交给 :func:`deskmate.sacred_paths.guarded_write`
        （唯一事实源）判定；返回 False（受保护的原稿）即命中；
    (b) goal 无路径但含「原稿/手稿/小说正文/人物小传」等词 → 命中。
    """
    g = str(goal or "")
    if not g.strip():
        return False
    try:
        from deskmate.sacred_paths import guarded_write

        for m in _RE_FILEPATH.finditer(g):
            try:
                if not guarded_write(m.group(0)):
                    return True
            except Exception:  # noqa: BLE001 —— 单条路径判定失败不影响其余
                continue
    except Exception:  # noqa: BLE001 —— 守卫模块缺失 → 退语义判据
        pass
    return any(t in g for t in _MANUSCRIPT_TERMS)


def is_external_intent(goal: str) -> bool:
    """goal 是否指向「对外表达」（发帖 / 回复 / 投专栏）→ 需二次人审。"""
    g = str(goal or "")
    return any(t in g for t in _EXTERNAL_TERMS)


def is_research_intent(goal: str) -> bool:
    """goal 是否指向「查资料 / 查史料佐证」（只用本地史料库）。"""
    g = str(goal or "")
    return any(t in g for t in _RESEARCH_TERMS)


# ----------------------------------------------------------------------
# 本地素材（只用本地，不联网）
# ----------------------------------------------------------------------
def _local_history_materials(base_dir: str, limit_chars: int = 4000) -> list[str]:
    """本地自有史料库片段（``<base>/history/``）；缺失 → ``[]``（不联网兜底）。"""
    out: list[str] = []
    used = 0
    root = Path(base_dir) / "history"
    if not root.is_dir():
        return []
    try:
        files = sorted(p for p in root.rglob("*") if p.is_file()
                       and p.suffix.lower() in (".md", ".txt", ".markdown"))
    except Exception:  # noqa: BLE001
        return []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:  # noqa: BLE001
            continue
        if not text:
            continue
        snippet = text[:800]
        out.append(f"〔{p.name}〕{snippet}")
        used += len(snippet)
        if used >= limit_chars:
            break
    return out


def _world_materials(base_dir: str, limit: int = 8) -> list[str]:
    """已有的世界观察（带「（外部·」溯源标者），作为产物的既有素材。"""
    try:
        from deskmate.memory.learned_store import LearnedStore
        from deskmate.memory.world_context import is_world_fact

        rows = LearnedStore(base_dir=base_dir).active_recent(limit=200)
    except Exception:  # noqa: BLE001
        return []
    out = [str(r.get("fact") or "").strip() for r in rows]
    return [f for f in out if f and is_world_fact(f)][-limit:]


def collect_materials(base_dir: str, goal: str) -> list[str]:
    """按 goal 取本地素材：查证类→本地史料库；其余→已有世界观察。"""
    if is_research_intent(goal):
        mats = _local_history_materials(base_dir)
        if mats:
            return mats
    return _world_materials(base_dir)


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------
def parse_intent_output(raw: str) -> Optional[dict[str, Any]]:
    """解析模型输出里的产物 JSON（容忍代码围栏 / 前后杂文本）。

    返回 ``{"kind","title","body"}``；解析不出 / body 空 → ``None``（fail-open）。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    text = _RE_FENCE.sub("", text).replace("`", "").strip()
    data: Any = None
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if not isinstance(data, dict):
        return None
    body = str(data.get("body") or data.get("content") or data.get("artifact") or "").strip()
    if not body:
        return None
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in ("synthesis", "diary", "research", "draft_external"):
        kind = "synthesis"
    return {
        "kind": kind,
        "title": str(data.get("title") or "").strip()[:80],
        "body": body,
    }


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def run_intent_execution(base_dir: str, intent: dict[str, Any], decision: str = "executed",
                         config: Optional[dict[str, Any]] = None,
                         mode: str = "executed") -> dict[str, Any]:
    """把一条被放行的意图**真做出来**。fail-open（任何异常都不抛）。

    闸门顺序：开关 → 原稿红线（no-op）→ 对外表达（转草稿 + 二次人审）→ provider → 产出。

    ``mode`` 取值：
    - ``"executed"``（默认）：产出直接落 ``intent_outputs`` 表（老路径，R12 行为）。
    - ``"discuss_draft"``（R13）：**不落库**，产出 artifact 返回给调用方
      （discuss/run 端点）处理。执行链：discuss/run 调本函数拿 artifact →
      写 proactive_ledger（phase=draft）→ speak 递到聊天 → 用户点评 →
      几轮后 discuss/finalize 才把最终版落 intent_outputs。

    Returns:
        ``{"executed": bool, "reason": str, "output_id": str, "kind": str,
        "pending_review": bool, "summary": str, "artifact": str, "draft": {...}}``
    """
    result: dict[str, Any] = {
        "executed": False, "reason": "", "output_id": "", "kind": "",
        "pending_review": False, "summary": "", "artifact": "", "draft": {},
    }

    cfg = dict(DEFAULT_CFG)
    if isinstance(config, dict):
        cfg.update({k: v for k, v in config.items() if k in DEFAULT_CFG})

    data = intent or {}
    goal = str(data.get("goal") or "").strip()
    if not goal:
        result["reason"] = "no_goal"
        return result

    # —— 闸门 0：开关 ——
    if not cfg.get("enabled", True) or os.environ.get(
        "AGENCY_INTENT_EXECUTION_ENABLED", "1"
    ) == "0":
        result["reason"] = "disabled"
        return result

    # —— 闸门 1：替你写作 = 永久红线（no-op）——
    if is_manuscript_intent(goal):
        result["reason"] = "sacred_manuscript"
        return result

    # —— 闸门 2：对外表达 → 只出草稿，绝不自动发（二次人审）——
    external = is_external_intent(goal)

    # —— 闸门 3：能力（付费主模型）——
    try:
        from deskmate.memory.llm import chat, llm_available

        if not llm_available():
            result["reason"] = "no_provider"
            return result
    except Exception:
        result["reason"] = "no_llm_module"
        return result

    # —— 产出：真做出来 ——
    materials = collect_materials(base_dir, goal)
    prompt = _build_prompt(goal, data, materials, external)
    try:
        raw = chat(_SYSTEM, prompt, temperature=0.6,
                   max_tokens=int(cfg.get("max_tokens", 1200) or 1200), timeout=90.0)
    except Exception as e:  # noqa: BLE001 —— fail-open
        logger.warning("intent_executor: 产出调用失败: %s", e)
        result["reason"] = "llm_error"
        return result

    parsed = parse_intent_output(raw)
    if not parsed:
        result["reason"] = "parse_failed"
        return result

    # 对外表达类：产物标 draft_external + pending_review，等二次人审
    kind = "draft_external" if external else parsed["kind"]
    status = "pending_review" if external else "done"
    max_chars = int(cfg.get("max_chars", 4000) or 4000)
    artifact = parsed["body"][:max_chars]
    title = parsed["title"] or goal[:40]

    # —— R13：discuss_draft 模式不直接落库，返回 artifact 给 discuss/run 端点处理 ——
    # 执行链：discuss/run 端点调本函数（mode=discuss_draft）拿 artifact →
    # 写 proactive_ledger（phase=draft）→ speak 递到聊天 → 用户点评 →
    # 几轮后 discuss/finalize 端点才把最终版落 intent_outputs。
    if mode == "discuss_draft":
        result.update({
            "executed": True,
            "output_id": "",  # 未落库
            "kind": kind,
            "pending_review": external,
            "artifact": artifact,
            "summary": title,
            "title": title,
            "draft": {"title": title, "content": artifact} if external else {},
        })
        return result

    # —— 落库（产物可复核；供回流注入）——
    output_id = ""
    try:
        from deskmate.cognitive.store import CognitiveStore

        store = CognitiveStore(db_path=os.path.join(base_dir, "deskmate.db"))
        output_id = store.add_intent_output(
            intent_id=str(data.get("id") or ""), goal=goal,
            artifact=artifact, kind=kind, status=status,
        )
    except Exception as e:  # noqa: BLE001 —— fail-open
        logger.warning("intent_executor: 产物落库失败（忽略）: %s", e)
        result["reason"] = "store_error"
        return result

    if not output_id:
        result["reason"] = "store_error"
        return result

    result.update({
        "executed": True,
        "output_id": output_id,
        "kind": kind,
        "pending_review": external,
        "artifact": artifact,
        "summary": title,
        "draft": {"title": title, "content": artifact} if external else {},
    })
    return result


def _build_prompt(goal: str, intent: dict[str, Any], materials: list[str],
                  external: bool) -> str:
    lines = [f"【你想做、且已被放行的事】{goal}"]
    mot = str(intent.get("motivation") or "").strip()
    if mot:
        lines.append(f"【当初为什么想做】{mot}")
    origin = str(intent.get("origin") or "").strip()
    if origin:
        lines.append(f"【这个念头的来处】{origin}")
    if materials:
        lines.append("")
        lines.append("【你手上已有的材料（只用这些，别编）】")
        lines.extend(f"- {m}" for m in materials)
    else:
        lines.append("")
        lines.append("【你手上已有的材料】暂时没有更多材料——那就凭你已有的判断写，别编造事实。")
    if external:
        lines.append("")
        lines.append("【注意】这件事是对外说的（发帖/回复/投专栏）。你先把**草稿**写好，"
                     "萧盛看过点头之后才会真的发出去——所以写就写好，别有顾虑。")
    lines.append("")
    lines.append("按上面格式给我 JSON；把这件事真的做出来。")
    return "\n".join(lines)