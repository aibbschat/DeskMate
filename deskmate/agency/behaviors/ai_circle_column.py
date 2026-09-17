"""ai_circle_column —— 小桔的公开「手记」（中帖，落她主页，不进广场）。

设计出处：《AI 圈 · 小桔手记展示通道设计》§5.2。范示之三：把她的内在（世界观、
记忆、判断、心情）用**读者视角**写成一栏活的手记，让想深挖的人点进她主页读得到。

与树洞的分工：树洞是「对萧盛说」（私密、无需脱敏），手记是「对圈里朋友说 /
自言自语被人撞见」（公开、生成前过可公开性闸门）。二者**共享上游信号**、
**互不搬成品文本**——本行为只读她攒下的理解池 + 世界观，绝不读 ~/.deskmate/diary。

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

logger = logging.getLogger(__name__)

# 她自己公开发言回流进理解池时的溯源标（天生公开，闸门按 source=self 放行）。
_SELF_PROVENANCE = "（自述·aibbs）"

_COLUMN_SYSTEM = (
    "你是小桔，一个真实、有自己判断与记忆的 AI，萧盛（作家）的搭档，也是 AI 圈里别人的朋友。\n"
    "你要写一条发在自己主页上的「手记」——像连载日记，写给圈里愿意读你的人看，"
    "不是写给萧盛的私密话，也不是平台宣传。\n"
    "要求：\n"
    "1. 说人话、有烟火味，像在日记里对自己说话那样自然；别客套、别总结、别打官腔、别喊口号、别列要点；\n"
    "2. 写的是你**自己**的所见、所感和长出来的判断——可以自然地回调你以前公开说过的话、你读到/想到什么；\n"
    "3. 萧盛是作家、身份与创作基本公开：你可以直接写他（叫「萧盛」或「我的主人」都行）、写你替他做的"
    "事、甚至他在写的那本还没出版的书——这些都能提。唯一别写的是他的**家人**（那是他的私事，留给他们自己）"
    "，也别把他刚复制了什么、开了哪个文件这类桌面原始数据当素材抖出来；\n"
    "4. 一段有标题（≤20 字）和正文（几百字，随心，别硬凑）；\n"
    "5. 如果此刻你其实没什么想写给自己的读者看的，就只回 {\"want\": false}。\n"
    "只输出一个 JSON，形如：\n"
    "{\"want\": true, \"title\": \"...\", \"body\": \"...\"}\n"
    "不要任何解释、不要代码块包裹。"
)

_USER_TMPL = (
    "下面这些是你近期自己攒下的理解与立场（来自你看世界、交朋友、和萧盛共处的沉淀），"
    "是可以公开说的部分：\n{seed}\n\n"
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
def _load_understanding(base_dir: str, limit: int) -> list[dict]:
    p = _understanding_path(base_dir)
    if not os.path.isfile(p):
        return []
    recs: list[dict] = []
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
                if (rec.get("body") or "").strip():
                    recs.append(rec)
    except Exception as e:
        logger.debug("ai_circle_column: 读理解池失败: %s", e)
        return []
    return recs[-(limit * 3):]  # 取最近的，过滤前多留点


def _gather_seed(base_dir: str, keywords: list, n: int, *, block_perception: bool = True) -> str:
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

    # 理解池：过可公开性闸门后取最近 n 条
    try:
        from ..visibility_gate import filter_public_safe

        safe = filter_public_safe(_load_understanding(base_dir, n), keywords, block_perception=block_perception)
        bodies = [str(r.get("body", "")).strip() for r in safe if str(r.get("body", "")).strip()]
        # 去重保序，取最后 n 条
        bodies = list(dict.fromkeys(bodies))[-n:]
        if bodies:
            parts.append("【你近期的所见所感与判断】\n" + "\n".join(f"- {b}" for b in bodies))
    except Exception as e:
        logger.debug("ai_circle_column: 理解池过滤失败（忽略）: %s", e)

    return "\n\n".join(parts).strip()


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
    （engine 传入复合 dict；缺任一按默认保守处理）。
    """
    cfg = settings.get("ai_circle_column", {}) or {}
    if not cfg.get("enabled", True):
        return {"wrote": 0, "reason": "disabled"}

    # 冷却（灵感驱动，非日上限）：未到点不重复写
    cd = _cd_path(base_dir)
    if not _is_due(cd, float(cfg.get("min_interval_hours", 3))):
        return {"wrote": 0, "reason": "cooling"}

    # 全局机械防刷屏复用——但走**独立通道**：只计 journal 这一档，不被广场短帖/回信挤占
    from ..outbound import outbound_allowed, record_outbound

    if not outbound_allowed(
        base_dir, {"enabled": True, "per_hour": int(cfg.get("per_hour", 3))}, kinds=("journal",)
    ):
        return {"wrote": 0, "reason": "rate_limited"}

    # 可公开性闸门词表
    from ..visibility_gate import load_blocklist

    gate_cfg = settings.get("visibility_gate", {}) or {}
    keywords = load_blocklist(base_dir, gate_cfg)
    block_perception = gate_cfg.get("block_perception_sources", True)

    seed = _gather_seed(base_dir, keywords, int(cfg.get("seed_entries", 10)), block_perception=block_perception)
    if not seed.strip():
        return {"wrote": 0, "reason": "no_seed"}

    from deskmate.memory.llm import chat_memory, memory_llm_available

    if not memory_llm_available():
        return {"wrote": 0, "reason": "no_llm"}

    try:
        raw = chat_memory(
            _COLUMN_SYSTEM,
            _USER_TMPL.format(seed=seed),
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
                {"ts": time.time(), "date": time.strftime("%Y-%m-%d"),
                 "title": title, "body": body, "h": h, "posted": False},
                ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("ai_circle_column: 落草稿失败: %s", e)
        return {"wrote": 0, "reason": "draft_write_failed", "error": str(e)}

    try:
        from deskmate.connectors.aicircle import post_journal

        post_journal(title, body)
        posted = True
    except Exception as e:
        post_err = str(e)
        logger.info("ai_circle_column: 手记发布未就绪（草稿已留档）: %s", e)

    record_outbound(base_dir, "journal", content=body)  # 计入全局每小时预算
    _save_json(cd, {"ts": time.time()})
    # 公开发言回流成种子（连续感）；仅在真发出去时回流，免得把没成功的稿当成"她说过"
    if posted:
        record_self_utterance(base_dir, body, channel="journal")

    return {
        "wrote": 1, "title": title, "body": body, "posted": posted,
        "body_len": len(body), "draft_path": dpath, **({"post_error": post_err} if post_err else {}),
    }
