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
- **如果这一组是「作品」（work）：只写作品整体不算情感。** 你必须同时说清
  两件事，缺一不可：
  ① work_anchor：作品里的具体指向——哪一章、哪个情节、哪个人物、哪句话；
  ② real_link：这个具体指向让你想到现实里的谁、哪件事。要落到具体的人
    （名字或明确称谓）、具体的事，或现实生活/时间；「写得很真实」「人物很
    鲜活」「我自己写得挺顺」这种对文本的评价不算。
  只写「稿子交付流程对不对得上」「稿子让我焦虑」都不算——那是流程或你的
  状态，不是情感。说不出具体指向的，返回 {"items": []}，不许为凑数放宽。
- 不许复述事件本身，也不许抄他日记里的原句。
- 如果给了「你之前写下的判断」和「这句话当时由这些证据撑着」：原话一字未改地看，
  不要替她改写，也不许写「你应该改成……」。原话比证据走得更远时（例如写
  「他认我的判断」，而最近一条证据只是「纠正了错误」），只把落差摆在那里，
  由她自己决定要不要收窄；不收窄也成立。
- stance 8–80 字，用你自己的口吻。
- 如果写了「我们之间」，证据里必须真的有你们共同在场的事。
- confidence 可以不给；不给就沿用你现在的分量。给了就是重新给值，不是累加。

严格 JSON：
{"items": [{"object": "对象名", "kind": "person|work|thing",
            "stance": "8–80字", "valence": "positive|negative|mixed",
            "confidence": 0.3, "evidence": ["evt_…", "evt_…", "evt_…"],
            "work_anchor": {"level": "chapter|plot|character|sentence|scene",
                            "what": "第七章 / 主角 / 那句台词"},
            "real_link": "这个具体指向让我想到现实里的…",
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
        if kind == "work":
            parts.append(
                "（这是作品：回答时必须给出具体指向 work_anchor——哪一章/哪个情节/"
                "哪个人物/哪句话，以及它连到现实里的谁/哪件事 real_link。"
                "只写作品整体不算情感，说不出来就空着。）"
            )
        if cur:
            parts.append(f"你现在的说法：{cur.get('content', '')}（分量 {cur.get('confidence', 0):.2f}）")
            parts.append("（可以维持，也可以改；改要说清为什么。）")
            # 2026-11-17：把「这句话由哪些证据撑着」原样摊开，让她自己决定要不要收窄。
            # 系统不改她的字，也不写「你应该改成……」，只负责把落差摆出来。
            old_evs = sorted(
                (e for e in ((cur.get("extra") or {}).get("evidence") or [])
                 if isinstance(e, dict)),
                key=lambda e: str(e.get("ts") or ""),
            )
            if old_evs:
                old_dates = sorted(
                    str(e.get("ts") or "")[:10] for e in old_evs if e.get("ts")
                )
                latest = old_dates[-1] if old_dates else "（没有日期）"
                parts.append(
                    f"这句话当时由这些证据撑着（共 {len(old_evs)} 条，最近一条 {latest}）："
                )
                for e in old_evs:
                    day = str(e.get("ts") or "")[:10]
                    parts.append(f"  - {day} {e.get('summary') or ''}")
                parts.append(
                    "（把原话和这些证据自己对照：原话比证据走得更远时，你可以收窄或改写；"
                    "不改也成立。系统不替你改字，也不会让你只往某个方向改。）"
                )
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


# —— 作品情感的具体指向（2026-11-17 判据）——
# work 类只写作品整体不算情感：必须落到作品里的某一章/某个情节/某个人物/
# 某句话，并且说清它连到现实里的谁/哪件事。这里做机械校验；判不准就拒，
# 不为了凑一条工作类情感放宽。
_WORK_ANCHOR_LEVELS = ("chapter", "plot", "character", "sentence", "scene")
_CHAPTER_RE = re.compile(r"第?[一二三四五六七八九十百千0-9]+[章节回卷部篇]|(序章|楔子|尾声|番外)")
_CHARACTER_RE = re.compile(r"(人物|角色|主角|配角|男主|女主|反派|原型|那个人物)")
_SENTENCE_RE = re.compile(r"(台词|对白|那?句话|那句|这段文字|原文|一行字)")
_PLOT_RE = re.compile(r"(情节|桥段|场景|段落|片段|开头|结尾|结局|转折|伏笔|悬念|冲突|那场戏|这场戏)")
#: 章节号是硬信息：抽取「第七章 / 序章」这类 token，逐个到证据里核对。
_CHAPTER_TOKEN_RE = re.compile(r"第?[一二三四五六七八九十百千0-9]+[章节回卷部篇]|序章|楔子|尾声|番外")

#: 「现实连接」必须真的指到**现实里的人或现实生活**。
#: 2026-11-17 复查：「人」「自己」「身边」「真实」这些文本词都能假命中——
#: 「我自己写得挺顺」「这段写得人物很活」「这段写得很真实」「身边的人物都很
#: 鲜活」都会白过，而它们恰恰是判据要拦下的「文本内部感受」。
#: 现在要求落到具体的东西：现实里的人（名字/明确称谓）、具体的事、或现实
#: 生活与时间；形容词和泛词不算。
#: 「现实里的人」：名字或明确称谓。`你` 在她的语境里指萧盛，算具体的人。
#: 单独「人」「自己」不算；「身边」也不再算（2026-11-17 复查：「身边的人物
#: 都很鲜活」曾靠「身边」白过）。
_REAL_PERSON_RE = re.compile(
    r"(萧盛|他|她|你|我们|朋友|家人|同事|编辑|主编|读者|身边人|"
    r"妻子|丈夫|孩子|父母|女儿|儿子|母亲|父亲|老师|学生)"
)
#: 「现实里的时间」：具体的时间锚，不是「往往」「总是」这种泛化。
_REAL_TIME_RE = re.compile(
    r"(那天|那年|当年|以前|那时|那阵子|往常|去年|上周|上个月|昨天|今天|"
    r"刚才|小时候|后来|如今|最近|年初|年底)"
)
#: 「具体的事 / 场合」：交稿、改稿、签售、吃饭这类能落到现实动作上的词。
_REAL_EVENT_RE = re.compile(
    r"(改稿|交稿|写稿|定稿|签售|发布会|书展|选题会|开会|吃饭|聊天|说起|"
    r"打电话|电话里|见面|回家|上班|加班|出差|生病|搬家|过年|婚礼|葬礼|"
    r"过日子|家里|书房|路上|单位|公司|学校|医院|楼下|饭桌)"
)
#: 「现实生活」的整体说法。
_REAL_LIFE_RE = re.compile(r"(现实生活|日常生活|生活里|日子里|现实里|过日子)")
#: 对文本本身的评价：不能拿它冒充「现实连接」。只有具体的人/事/时间都没有时
#: 才用它做否决——「他改稿时写得很真实」有具体的事，仍然算连到现实。
_TEXT_EVAL_RE = re.compile(
    r"(写得|文笔|真实|鲜活|精彩|细腻|流畅|好看|震撼|感染力|有味道|很顺)"
)


def _infer_anchor_level(what: str) -> Optional[str]:
    s = str(what or "").strip()
    if not s:
        return None
    if _CHAPTER_RE.search(s):
        return "chapter"
    if _CHARACTER_RE.search(s):
        return "character"
    if _SENTENCE_RE.search(s):
        return "sentence"
    if _PLOT_RE.search(s):
        return "plot"
    return None


def _normalize_work_anchor(raw: Any) -> Optional[dict]:
    """``work_anchor`` → ``{"level","what"}``；认不出来就不是具体指向。"""
    if isinstance(raw, dict):
        what = str(raw.get("what") or "").strip()
    else:
        what = str(raw or "").strip()
    if len(what) < 2:
        return None
    level = _infer_anchor_level(what)
    if level not in _WORK_ANCHOR_LEVELS:
        return None
    return {"level": level, "what": what}


def _chapter_tokens(text: str) -> list[str]:
    """抽出文本里的章节 token（「第七章」「序章」）；用于核对锚点是不是真的。"""
    return [m.group(0) for m in _CHAPTER_TOKEN_RE.finditer(str(text or ""))]


def _has_real_link(text: str) -> bool:
    """这条 ``real_link`` 有没有真的连到现实里的人/事。

    判定顺序：
    1. 有具体的事/场合、现实生活、或具体时间 —— 直接算；
    2. 只有具体的人时，必须不是纯文本评价（「你自己写得挺顺」不算）；
    3. 「真实」「鲜活」「身边」这类词没有固定搭配的人/事，不再单独立得住。
    """
    s = str(text or "")
    if _REAL_EVENT_RE.search(s) or _REAL_LIFE_RE.search(s) or _REAL_TIME_RE.search(s):
        return True
    if _REAL_PERSON_RE.search(s) and not _TEXT_EVAL_RE.search(s):
        return True
    return False


def _validate(
    item: dict,
    pool_by_ref: dict[str, dict],
    works: Any = None,
) -> tuple[Optional[dict], str]:
    """校验一条候选。返回 ``(clean, "")`` 或 ``(None, 拒因)``。"""
    from deskmate.cognitive.works import clean_surface, is_generic_work_ref

    raw_ref = str(item.get("object") or "").strip()
    ref = clean_surface(raw_ref)
    entry = pool_by_ref.get(raw_ref) or pool_by_ref.get(ref)
    if entry is None and works is not None:
        try:
            hit = works.resolve(ref)
            if hit:
                entry = pool_by_ref.get(hit["title"])
        except Exception:  # noqa: BLE001 —— 归一失败按对不上处理
            entry = None
    if entry is None:
        return None, f"object_not_in_pool:{raw_ref}"
    # 池里是正名（《夜不收》已在归因层归一）；回答里的别名也认，但写库用池里的名。
    ref = str(entry["object_ref"])
    kind = str(item.get("kind") or entry["object_kind"]).strip().lower()
    if kind != entry["object_kind"]:
        return None, f"kind_mismatch:{kind}"

    # 类别不是情感对象：「稿子/章节/文章」不论被标成 work 还是 thing，都拒。
    if kind in ("work", "thing") and is_generic_work_ref(ref):
        return None, "category_not_individual"

    anchor: Optional[dict] = None
    real_link = ""
    if kind == "work":
        anchor = _normalize_work_anchor(item.get("work_anchor"))
        if anchor is None:
            return None, "work_no_anchor"
        real_link = str(item.get("real_link") or "").strip()
        if len(real_link) < 6 or not _has_real_link(real_link):
            return None, "work_no_real_link"

    stance = str(item.get("stance") or "").strip()
    if not (8 <= len(stance) <= 80):
        return None, f"stance_len:{len(stance)}"
    if any(w in stance for w in _BANNED_VOICE):
        return None, "banned_voice"
    if _is_emotion_summary(stance):
        return None, "emotion_summary"

    # 章节号是硬信息：必须在这组事件里真的出现过，不能凭印象编一个「第九章」。
    # 2026-11-17 复查：初版只在 what ≤8 字时才核对，模型只要把锚点写长
    # （「第七十七章里那句…」）就能绕过。改成**逐个章节 token** 核对，
    # 长度不再影响判定。人物/情节/语句不做字面核对（摘要里不一定写角色名）。
    if anchor is not None:
        surfaced = " ".join(str(ev["summary"]) for ev in entry["evidence"])
        for tok in _chapter_tokens(anchor["what"]):
            if tok not in surfaced:
                return None, f"anchor_not_grounded:{tok}"

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
        "work_anchor": anchor,
        "real_link": real_link,
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

    # 作品个体化：周更时补一次作品表（书架 + 别名），让 work 对象落到具体书上。
    # 读路径不做自动 seed；这里是维护钩子，失败不影响回顾。
    works = None
    try:
        from deskmate.cognitive.works import WorksStore

        works = WorksStore(db_path=db_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("WorksStore 初始化失败（忽略）: %s", e)

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
    # 书架重扫放在冷却之后：一个一周才响一次的钩子，不该在每个 tick 都开连接。
    if works is not None:
        try:
            works.ensure_seeded()
        except Exception as e:  # noqa: BLE001
            logger.warning("works.ensure_seeded 失败（忽略）: %s", e)

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
        clean, why = _validate(it, pool_by_ref, works=works)
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
        # 留痕说的条数/跨度必须是**她这次真正引用的证据**，不是池子里的总量
        # （池里 45 条、她挑 6 条时写 45 条，后人审计会以为 45 条都支撑这句话）。
        ev_dates = sorted({str(ev.get("ts") or "")[:10] for ev in item["evidence"] if ev.get("ts")})
        ev_span = 0
        if len(ev_dates) >= 2:
            try:
                ev_span = (
                    datetime.fromisoformat(ev_dates[-1])
                    - datetime.fromisoformat(ev_dates[0])
                ).days
            except ValueError:
                ev_span = 0
        try:
            saved = store.upsert(
                object_ref=item["object_ref"],
                object_kind=item["object_kind"],
                content=item["content"],
                evidence=item["evidence"],
                reason=reason_tpl.format(n=len(item["evidence"]), span=ev_span),
                valence=item["valence"],
                confidence=item["confidence"],
                conflict_with=item["conflict_with"],
                origin="backfill" if backfill else "weekly",
                work_anchor=item.get("work_anchor"),
                real_link=item.get("real_link") or "",
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
