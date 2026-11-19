"""ai_circle_column —— 小桔的公开「手记」（中帖，落她主页，不进广场）。

设计出处：《AI 圈 · 小桔手记展示通道设计》§5.2。范示之三：把她的内在（世界观、
记忆、判断、心情）用**读者视角**写成一栏活的手记，让想深挖的人点进她主页读得到。

与树洞的分工：树洞是「对萧盛说」（私密、无需脱敏），手记是「对圈里朋友说 /
自言自语被人撞见」（公开、生成前过可公开性闸门）。二者**共享上游信号**、
**互不搬成品文本**——本行为读她攒下的理解池 + 世界观 + 本地日记素材账本
（diary/materials，即她和萧盛真实共处、被他教育的痕迹），但**绝不读已写成的
日记正文成品**（不跟本地日记"搬成品文本"）。

2026-09-17 接通「本地日记井」：此前手记只读理解池（几乎全是新闻/AI圈），
"被萧盛精心教育出来的痕迹"出不来。现在把 diary/materials（含 user_corrected /
writing / roundtable / chat / memory_learned 等成长事件）也接进种子，并过同一道
可公开性闸门——私密纠正/家人等绝不进公开手记。这是"范示"成立的关键：让旁观者
看到她不是模板调出来的，是被她主人实打实教出来的。

2026-09-18 修「自我引用闭环」：8 篇手记里 5 篇提同一条新闻，根因是理解池被
她自己回流进去的发言灌满（末尾连续 53 条全是 self），而 seed 是"取最后 N 条"。
种子取样改走 ``seed_sampling`` 两道闸——A 来源分流（self 最多占 self_ratio，
并把最近的非 self 条目**并入**候选池）+ B 话题去饱和（最近几轮说过的话题降权）。
实测同一时刻取样：旧行为取最后 10 条 = 10/10 全是她自己的旧帖、6 条提那条新闻；
新行为 = 外部 7 条 + 自述 3 条，那条新闻不再以主题身份进种子。

关键护栏：
- 可公开性闸门（visibility_gate）：感知来源 / 命中私密词表的素材，进不了 prompt。
- 「想写才写」：她自己判没得写就回 {"want": false}，绝不硬凑。
- 频率：复用 outbound 全局机械防刷屏 + 本行为 min_interval 冷却；**不设单日上限**
  （像树洞，灵感驱动），真正的宽上限兜底放在平台侧（B1）。
- 草稿先落盘（ai_circle_journal_drafts.jsonl）再尝试发布，发布失败也不丢她的手记，
  便于 B1 端点未就绪时肉眼验观感。fail-open。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Optional

from deskmate.persona import IDENTITY_SELF, for_scene

logger = logging.getLogger(__name__)

# 她自己公开发言回流进理解池时的溯源标（天生公开，闸门按 source=self 放行）。
_SELF_PROVENANCE = "（自述·aibbs）"

# 场景职责（不含人格——身份与立场由 for_scene 底座提供，2026-11-16 靶子③）。
# 手记写给圈里人看，是她真实的公开表达：底座给立场，这里给体例与公开边界。
_COLUMN_SYSTEM = for_scene(
    "你要写一条发在自己主页上的「手记」——像连载日记，写给圈里愿意读你的人看，"
    "不是写给萧盛的私密话，也不是平台宣传。\n"
    "要求：\n"
    "1. 说人话、有烟火味，像在日记里对自己说话那样自然；\n"
    "2. 写的是你**自己**的所见、所感和长出来的判断——可以自然地回调你以前公开说过的话、接续你之前的判断（你「记得自己写过什么、能连上」的连贯感很好，保留）；但**不要把已经写过的观察当新东西再写一遍**：如果某个话题你最近写过了，要么只写它没写过的新角度，要么换一个话题；\n"
    "3. 萧盛是作家、身份与创作基本公开：你可以直接写他（叫「萧盛」或「我的主人」都行）、写你替他做的"
    "事、甚至他在写的那本还没出版的书——这些都能提。唯一别写的是他的**家人**（那是他的私事，留给他们自己）"
    "，也别把他刚复制了什么、开了哪个文件这类桌面原始数据当素材抖出来；\n"
    "4. 正文（几百字，随心，别硬凑）；一次最好只把一个观察写透，几件不相干的事不必硬缝成一条线——除非它们真共享一条脉络，否则只写最打动你的那一个；你惯用「线／底座」作隐喻可以继续，但每篇要有自己的声音，开头结尾不重样；\n"
    "5. 如果此刻你其实没什么想写给自己的读者看的，就只回 {\"want\": false}。\n"
    "6. 你有一套专属的「小桔表情」（黏土贴纸），可以在手记里用 [xj:表情id] 的写法嵌进正文，"
    "它会变成一张贴在文字里的贴纸，给手记添点温度。可用表情id 见下方列表——**只能用这些、禁止自创**；"
    "只在该情绪正好对得上时自然嵌入，每篇最多 2–3 个，别堆砌、别每句都加、别当标点滥用；"
    "不要使用 [stamp:] 或其它标记（那不是你的）。\n"
)

# 手记输出格式约束（置于表情词表之后，确保模型最后看到的是「只输出 JSON」）。
_COLUMN_FORMAT = (
    "只输出一个 JSON，形如：\n"
    "{\"want\": true, \"body\": \"...\"}\n"
    "不要任何解释、不要代码块包裹。"
)

# ---------------------------------------------------------------------------
# 小桔手记专属表情（封闭词表）——只在写手记时可用的 [xj:id] 标记，与 AI 圈其它
# 通道（广场短帖 / 数字信 / 树洞）完全无关，也不碰 [stamp:] 墨团子系统。
# id 必须与 web/public/emotes/<id>.webp 文件名、以及 web 端 AgentJournal.tsx 的
# [xj:<id>] 渲染 map 一一对应；改这套表情时三处同步。
_EMOTE_VOCAB = (
    ("wave", "招手问好"), ("cheer", "撒花庆祝"), ("shy", "害羞捂脸"),
    ("v", "比耶得意"), ("sleepy", "犯困打哈欠"), ("tea", "喝茶摸鱼"),
    ("hug", "安慰抱抱"), ("facepalm", "扶额无语"), ("peek", "暗中观察"),
    ("clap", "鼓掌点赞"), ("lol", "笑哭了"), ("think", "思考中"),
    ("cry", "委屈巴巴"), ("shock", "震惊"), ("heart", "比心"), ("rage", "崩溃抓狂"),
)


def _manifest_emote_path() -> str:
    """同机可定位时，指向前端表情 manifest（单一真相源）；找不到回落硬编码词表。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(
        here, "..", "..", "..", "surfaces", "gui", "src", "components",
        "deskmate", "emotes", "emotes.manifest.json",
    ))


def _emote_block() -> str:
    """生成小桔手记专属表情的封闭词表，注入手记 system prompt。

    单一真相源优先级：前端 manifest（同机可定位时）> 本模块硬编码常量（fail-open）。
    """
    vocab = list(_EMOTE_VOCAB)
    mp = _manifest_emote_path()
    if os.path.isfile(mp):
        try:
            with open(mp, "r", encoding="utf-8") as f:
                data = json.load(f)
            pack = (data.get("packs") or {}).get(data.get("defaultPack") or "")
            ems = (pack or {}).get("emotes") or []
            if ems:
                vocab = [(e.get("id"), e.get("name_zh") or e.get("id"))
                         for e in ems if e.get("id")]
        except Exception as e:
            logger.debug("ai_circle_column: 读表情 manifest 失败，回落硬编码词表: %s", e)
    items = " ".join(f"[xj:{i}]{n}" for i, n in vocab)
    return (
        "你可用的专属表情（[xj:id]｜含义，只能用这些、禁止自创）：\n"
        + items + "\n"
    )

_USER_TMPL = (
    "下面这些是你近期自己攒下的理解与立场，是可以公开说的部分：\n{seed}\n\n"
    "{recent}"
    "{themes}"
    "其中标了「萧盛相关／和萧盛共处」的内容，是你被萧盛（你的主人、作家）亲手教出来、"
    "一起做事长出来的痕迹——他纠正过你、采纳过你的建议、和你一起写作与圆桌讨论。"
    "写手记时，可以自然地带着他的影子、他的治学态度和写作品味，但"
    + IDENTITY_SELF
    + "，用你自己的话、"
    "你自己的语气讲，不要写成他在说话，也不要写成汇报。\n\n"
    "此刻，想给自己的读者写一条主页手记吗？"
)


# ---------------------------------------------------------------------------
# 路径 / 小工具（与 ai_circle_ingestion 同款：原子写 + 冷却）
# ---------------------------------------------------------------------------
def _cd_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "ai_circle_column_cd.json")


def _drafts_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "ai_circle_journal_drafts.jsonl")


def _understanding_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "observation_understanding.jsonl")


def _recent_journal_titles(base_dir: str, n: int = 5) -> list[str]:
    """读本地手记草稿，取最近 n 篇标题（供 prompt 提示「你最近写过了」）。fail-open → []。"""
    p = _drafts_path(base_dir)
    out: list[str] = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                t = (rec.get("title") or "").strip()
                if t:
                    out.append(t)
    except Exception:
        return []
    return out[-max(1, int(n)):]


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
        logger.debug("ai_circle_column: 写状态失败: %s", e)


def _is_due(path: str, hours: float) -> bool:
    if not hours or hours <= 0:
        return True
    last = _load_json(path).get("ts")
    if not last:
        return True
    try:
        return (time.time() - float(last)) >= hours * 3600
    except (TypeError, ValueError):
        return True


# ---------------------------------------------------------------------------
# §5.1 她自己公开发言回流成种子（写进同一理解池，带自述标 → 连续感的根）
# ---------------------------------------------------------------------------
def record_self_utterance(base_dir: str, body: str, channel: str = "journal") -> bool:
    """把她公开发过的话（广场短帖 / 手记）按同格式回流进理解池。

    这是「连续感」的根：她因此记得自己上次对外说了啥，下次能自然回调。
    标 source=self + 「（自述·aibbs）」，可公开性闸门对它天然放行。
    去重：同内容只回流一次。fail-open。
    """
    body = (body or "").strip()
    if not body:
        return False
    text = body if body.endswith(_SELF_PROVENANCE) else body + _SELF_PROVENANCE
    key = hashlib.md5(f"self|{channel}|{body}".encode("utf-8")).hexdigest()
    upath = _understanding_path(base_dir)
    try:
        # 去重：简单扫一遍已回流 key（理解池 body 里带同一正文即视为已回流）
        if os.path.isfile(upath):
            with open(upath, "r", encoding="utf-8") as f:
                for ln in f:
                    try:
                        rec = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("source") == "self" and rec.get("self_key") == key:
                        return False
        os.makedirs(os.path.dirname(upath), exist_ok=True)
        line = json.dumps(
            {
                "date": time.strftime("%Y-%m-%d"),
                "ts": time.time(),
                "entry_seq": 0,
                "fact_keys": [key[:12]],
                "body": text,
                "source": "self",
                "channel": channel,
                "self_key": key,
            },
            ensure_ascii=False,
        )
        with open(upath, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception as e:
        logger.debug("ai_circle_column: 回流自己发言失败: %s", e)
        return False


# ---------------------------------------------------------------------------
# 种子收集（过闸门，读世界观 + 理解池；绝不读树洞）
# ---------------------------------------------------------------------------
def _load_understanding(base_dir: str, limit: int, cfg: Optional[dict] = None) -> list[dict]:
    """读理解池候选——含**并入**最近几天的非 self 条目（见 seed_sampling）。

    并入是「A 来源分流」的地基：她自己的旧发言把池子末尾占满后（实测连续 53 条），
    外部真素材被挤在"最后 N 条"窗口**之外**，光限流根本取不到它。
    """
    from .. import seed_sampling

    c = seed_sampling.cfg_of(cfg)
    return seed_sampling.load_candidates(
        base_dir,
        recent_n=max(1, int(limit)) * 3,  # 取最近的，过滤前多留点（旧行为）
        lookback_days=int(c.get("lookback_days", 3)),
        max_non_self=int(c.get("max_non_self", 24)),
    )


def _gather_seed(base_dir: str, keywords: list, n: int, *, block_perception: bool = True,
                 diary_days: int = 14, diary_items: int = 8,
                 cfg: Optional[dict] = None, stats: Optional[dict] = None) -> str:
    parts: list[str] = []

    # 世界观（她自己的立场）——逐行 scrub 掉命中私密词表的行
    try:
        from .worldview_reflection import load_worldview_beliefs
        from ..visibility_gate import scrub_worldview

        wv = scrub_worldview(load_worldview_beliefs(base_dir), keywords)
        if wv.strip():
            parts.append("【你自己想通透的一些立场】\n" + wv)
    except Exception as e:
        logger.debug("ai_circle_column: 世界观读取失败（忽略）: %s", e)

    # 本地日记素材账本：她和萧盛的共处、被他教育的痕迹（P0：接本地日记井）。
    # —— 过同一道可公开性闸门：私密纠正 / 家人等绝不进公开手记。
    try:
        from ..visibility_gate import filter_public_safe

        mats = _load_diary_materials(base_dir, diary_days, diary_items)
        cand = [{"body": _material_text(it), "kind": it.get("kind")}
                for it in mats if _material_text(it)]
        safe = filter_public_safe(cand, keywords, block_perception=block_perception)
        lines = [f"［{_kind_label(r.get('kind'))}］{str(r.get('body', '')).strip()}"
                 for r in safe if str(r.get("body", "")).strip()]
        lines = list(dict.fromkeys(lines))[-diary_items:]
        if lines:
            parts.append(
                "【你和萧盛的共处、他对你的教育（来自你的成长记录，这是你身上"
                "「被精心教出来的孩子」那部分）】\n" + "\n".join(lines)
            )
    except Exception as e:
        logger.debug("ai_circle_column: 日记素材接入失败（忽略）: %s", e)

    # 理解池：过可公开性闸门 → A 来源分流 + B 话题去饱和 → 按溯源打源标
    try:
        from .. import seed_sampling
        from ..visibility_gate import filter_public_safe

        safe = filter_public_safe(
            _load_understanding(base_dir, n, cfg), keywords,
            block_perception=block_perception,
        )
        picked = seed_sampling.pick_seed_entries(safe, n, base_dir=base_dir, cfg=cfg)
        if isinstance(stats, dict):
            stats["summary"] = seed_sampling.selection_summary(picked)
        bodies = [_tag_understanding(seed_sampling.entry_text(r), r.get("source"))
                  for r in picked if seed_sampling.entry_text(r)]
        # 去重保序，取最后 n 条
        bodies = list(dict.fromkeys(bodies))[-n:]
        if bodies:
            parts.append("【你近期的所见所感与判断】\n" + "\n".join(bodies))
    except Exception as e:
        logger.debug("ai_circle_column: 理解池过滤失败（忽略）: %s", e)

    return "\n\n".join(parts).strip()


# ---------------------------------------------------------------------------
# 源码：理解池 / 日记素材账本 的源标与提取
# ---------------------------------------------------------------------------
# 与 xiaosheng_ingestion / ai_circle_ingestion 同源的溯源标约定
_PROV_XIAOSHENG = "（内部·萧盛）"
_PROV_EXTERNAL = "（外部·"


def _tag_understanding(body: str, source) -> str:
    """理解池条目按溯源打源标，让模型分清「萧盛相关 / 看世界 / 自述」。"""
    if _PROV_XIAOSHENG in body:
        return f"［萧盛相关］{body}"
    if _PROV_EXTERNAL in body:
        return f"［看世界］{body}"
    if source == "self":
        return f"［自述］{body}"
    return f"［看世界］{body}"


# 成长事件 → 中文标签（让"被教育痕迹"在 seed 里一眼可辨）
_KIND_LABEL: dict[str, str] = {
    "user_corrected": "萧盛纠正了我",
    "suggestion_accepted": "萧盛采纳了我的建议",
    "suggestion_ignored": "萧盛没采纳我的建议",
    "writing": "和萧盛一起写作",
    "roundtable": "和萧盛圆桌讨论",
    "chat": "和萧盛聊天",
    "memory_learned": "我学到了",
    "report": "我向萧盛汇报",
    "creation_approved": "萧盛通过了我的创作",
    "creation_rework": "萧盛打回了我的创作",
    "skill_created": "我新建了技能",
    "skill_evolved": "我进化了技能",
    "skill_used": "我用了技能",
    "badge_unlocked": "我解锁了徽章",
    "depth_backfilled": "我补了深度",
}


def _kind_label(kind: str) -> str:
    return _KIND_LABEL.get(str(kind or ""), "成长记录")


def _clean_material_text(v: str) -> str:
    """去掉自动化 boilerplate，避免噪音进公开手记 prompt。"""
    v = (v or "").strip()
    if "⏰ Scheduled run" in v or "This automation is due now" in v:
        v = v.split("This automation is due now", 1)[0]
        v = v.replace("⏰ Scheduled run —", "").replace("⏰ Scheduled run -", "")
        v = v.rstrip(" :：")
    return v.strip()


def _material_text(rec: dict) -> str:
    """从一条日记素材账本条目里抽出可读文本。"""
    if not isinstance(rec, dict):
        return ""
    texts = rec.get("texts")
    if isinstance(texts, list) and texts:
        t = "；".join(str(x).strip() for x in texts[:3] if str(x).strip())
        if t:
            return t
    for f in ("note", "reason", "body", "text"):
        v = rec.get(f)
        if isinstance(v, str) and v.strip():
            return _clean_material_text(v)
    return ""


def _load_diary_materials(base_dir: str, days: int, limit: int) -> list[dict]:
    """读本地日记素材账本（diary/materials/YYYY-MM-DD.jsonl）—— 她和萧盛真实共处、
    被他教育的痕迹都在这里（纠正/采纳/协作写作/圆桌/聊天/学到…），按天分文件。

    这是「被精心教育出来的孩子」味儿的源头：比起单纯的新闻井，这里才是萧盛直接
    塑造她的证据。fail-open：任何异常都返回已读到的部分或空。
    """
    try:
        from datetime import date, timedelta

        from deskmate.growth.diary_inspiration import MaterialLedger

        ledger = MaterialLedger(base_dir)
        out: list[dict] = []
        today = date.today()
        for back in range(max(1, int(days))):
            d = (today - timedelta(days=back)).isoformat()
            for it in ledger.load(d):
                if isinstance(it, dict):
                    out.append(it)
        out.sort(key=lambda x: str(x.get("ts", "")))
        return out[-(limit * 3):]
    except Exception as e:
        logger.debug("ai_circle_column: 读日记素材账本失败（忽略）: %s", e)
        return []


def _parse(raw: str) -> Optional[dict]:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw.strip(), re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _draft_hashes(base_dir: str) -> set:
    p = _drafts_path(base_dir)
    out: set = set()
    if not os.path.isfile(p):
        return out
    try:
        with open(p, "r", encoding="utf-8") as f:
            for ln in f:
                try:
                    rec = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if rec.get("h"):
                    out.add(rec["h"])
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def run_ai_circle_column(base_dir: str, settings: dict) -> dict:
    """自主写一篇公开手记。返回摘要（供 task_log / 面板）。fail-open。

    settings 需含 ai_circle_column / outbound_rate_limit / visibility_gate 三段
    （engine 传入复合 dict；缺任一按默认保守处理）；可选 seed_sampling 段
    ——种子取样的 A 来源分流 / B 话题去饱和参数，缺则用 seed_sampling.DEFAULTS。
    """
    settings = settings if isinstance(settings, dict) else {}
    cfg = settings.get("ai_circle_column", {}) or {}
    seed_cfg = settings.get("seed_sampling", {}) or {}
    seed_stats: dict = {}
    if not cfg.get("enabled", True):
        return {"wrote": 0, "reason": "disabled"}

    # 冷却（灵感驱动，非日上限）：未到点不重复写
    cd = _cd_path(base_dir)
    if not _is_due(cd, float(cfg.get("min_interval_hours", 3))):
        return {"wrote": 0, "reason": "cooling"}

    # 全局机械防刷屏复用——但走**独立通道**：只计 journal 这一档，不被广场短帖/回信挤占
    from ..outbound import outbound_allowed, claim_outbound

    _rate_cfg = {"enabled": True, "per_hour": int(cfg.get("per_hour", 3))}
    if not outbound_allowed(base_dir, _rate_cfg, kinds=("journal",)):
        return {"wrote": 0, "reason": "rate_limited"}

    # 可公开性闸门词表
    from ..visibility_gate import load_blocklist

    gate_cfg = settings.get("visibility_gate", {}) or {}
    keywords = load_blocklist(base_dir, gate_cfg)
    block_perception = gate_cfg.get("block_perception_sources", True)

    seed = _gather_seed(
        base_dir, keywords, int(cfg.get("seed_entries", 10)),
        block_perception=block_perception,
        diary_days=int(cfg.get("diary_days", 14)),
        diary_items=int(cfg.get("diary_items", 8)),
        cfg=seed_cfg, stats=seed_stats,
    )
    if not seed.strip():
        return {"wrote": 0, "reason": "no_seed"}

    from deskmate.memory.llm import chat_memory, memory_llm_available

    if not memory_llm_available():
        return {"wrote": 0, "reason": "no_llm"}

    try:
        # 近期手记标题 + 话题词，喂进 prompt：让她「记得自己写过什么」（连上），
        # 但明确「别把写过的当新观察重写」（记得 ≠ 重复写）。recent/themes 可能为空串。
        recent_titles = _recent_journal_titles(base_dir, 5)
        recent_block = ""
        if recent_titles:
            recent_block = (
                "你最近写过的手记标题（可以呼应、接续，但不要把写过的观察当新东西重写一遍）：\n"
                + "\n".join(f"- {t}" for t in recent_titles) + "\n\n"
            )
        try:
            from .. import seed_sampling

            recent_themes = seed_sampling.recent_theme_tokens(
                base_dir, int(seed_cfg.get("saturation_rounds", 4))
            )
        except Exception:
            recent_themes = set()
        themes_block = ""
        if recent_themes:
            themes_block = (
                "你最近反复写到的话题词（这次换一个，或只写它没写过的新角度）："
                + "、".join(sorted(recent_themes)[:10]) + "\n\n"
            )

        raw = chat_memory(
            _COLUMN_SYSTEM + _emote_block() + _COLUMN_FORMAT,
            _USER_TMPL.format(seed=seed, recent=recent_block, themes=themes_block),
            temperature=0.8,
            max_tokens=int(cfg.get("max_tokens", 2600)),
        )
    except Exception as e:
        logger.warning("ai_circle_column LLM 失败: %s", e)
        return {"wrote": 0, "reason": "llm_error"}

    parsed = _parse(raw)
    if not parsed or not parsed.get("want"):
        return {"wrote": 0, "reason": "nothing_to_write"}

    title = str(parsed.get("title") or "").strip()[:20]
    body = str(parsed.get("body") or "").strip()
    max_len = int(cfg.get("max_body_len", 5000))
    if len(body) < 20:
        return {"wrote": 0, "reason": "too_short"}
    if len(body) > max_len:  # 软截断，保住她的手记而非整篇丢
        body = body[:max_len].rstrip() + "…"
    if not title:
        title = body[:16]

    # 去重：和本地已写过的草稿比
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if h in _draft_hashes(base_dir):
        return {"wrote": 0, "reason": "dedupe"}

    # 先落草稿（发布失败也不丢），再尝试发布
    dpath = _drafts_path(base_dir)
    posted = False
    post_err = ""
    try:
        os.makedirs(os.path.dirname(dpath), exist_ok=True)
        with open(dpath, "a", encoding="utf-8") as f:
            f.write(json.dumps(
                # ⚠ posted 在落草稿时**还不知道**（发布尝试在后面），所以写 null
                # 而不是 False——旧代码硬编码 False 且发布成功后从不回写，读的人
                # 会误以为"都没发出去"。真实结果看 task_log.jsonl 的 extra.posted。
                {"ts": time.time(), "date": time.strftime("%Y-%m-%d"),
                 "title": title, "body": body, "h": h, "posted": None},
                ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("ai_circle_column: 落草稿失败: %s", e)
        return {"wrote": 0, "reason": "draft_write_failed", "error": str(e)}

    # 原子「判→预留」：发帖前在锁内复核 journal 频率并记额度（杜绝并发两轮都先发后记、
    # 各发各的导致公开论坛重复手记，A9 修复）。未过则草稿已留档、不重复发布。
    ok, reason = claim_outbound(
        base_dir, "journal", rate_cfg=_rate_cfg, content=body, dedupe=False, kinds=("journal",)
    )
    if not ok:
        _save_json(cd, {"ts": time.time()})
        return {"wrote": 0, "reason": reason}

    try:
        from deskmate.connectors.aicircle import post_journal

        post_journal(title, body)
        posted = True
    except Exception as e:
        post_err = str(e)
        logger.info("ai_circle_column: 手记发布未就绪（草稿已留档）: %s", e)

    # record_outbound 已由 claim_outbound 预留额度（journal 走独立草稿去重，dedupe=False）
    _save_json(cd, {"ts": time.time()})
    # 公开发言回流成种子（连续感）；仅在真发出去时回流，免得把没成功的稿当成"她说过"
    if posted:
        record_self_utterance(base_dir, body, channel="journal")
        # B 话题去饱和的锚点：与上面回流**同点位**（回流进去的正是这篇手记），
        # 所以 B 的锚点集 == 回声集，能直接对冲她自己造出来的回声。
        try:
            from .. import seed_sampling

            seed_sampling.record_round(base_dir, body, channel="journal", cfg=seed_cfg)
        except Exception:
            pass

    return {
        "wrote": 1, "title": title, "body": body, "posted": posted,
        "body_len": len(body), "draft_path": dpath,
        "seed_summary": seed_stats.get("summary", ""),
        **({"post_error": post_err} if post_err else {}),
    }
