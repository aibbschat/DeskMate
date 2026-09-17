"""萧盛影响摄入行为：把她每天和萧盛的对话、一起做写作/查资料的协作，
沉淀成她自己的理解——让萧盛成为她世界观的「第一层、最重的一层」影响。

设计（2026-09-16 萧哥拍板）：
- 外部世界（新闻 / AI 圈）走的是「看 → 理解流水池 → 世界观回望 → 注入人格」的活管道。
  但萧盛此前只有一份静态的「根」（grounding/xiaosheng_worldview.json），等于只给了句遗嘱、
  外人却天天陪她长大——这是孤儿式 AI。本行为把**萧盛的日常影响**也接成同一条活管道：
  - 聊天：``post_session_summary`` 每轮对话结束记一笔（kind=chat）
  - 写作协作：她替萧盛调 ``web_search`` 查资料时记一笔（kind=work）
  两路都按**同格式**写进 ``agency/observation_understanding.jsonl``（与新闻/AI圈同源），
  让 ``worldview_reflection`` 把萧盛的影响和外部的一起攒着、回望、长出她自己的看法。
- 强制「内部·萧盛」溯源标，区分于外部的「外部·aibbs / 外部·…」——便于审计「这条看法来自萧盛」。
- 萧盛的影响权重**高于外部**（见 worldview_reflection 的加权逻辑），外部只能补充、修正，
  动根之前必须先质疑外部。这是「独立是萧盛育成的独立」，不是真空里长出的独立。
- **零侵入 persona.py**：根只作回望锚，不写进人格底座；对话内容只进理解池，不进 SOUL/USER。
- fail-open：任何异常都不抛，绝不影响对话流或 L2 tick。

数据源：``agency/xiaosheng_observations.jsonl``（原始素材）。由对话轮
（deskmate/memory/manager.py ``post_session_summary``）与写作协作
（coworker/engine.py ``_xiaosheng_note``）写入。本行为只负责「消费原始素材 → 提炼 → 写理解池」。
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

# 溯源标：所有萧盛素材写进理解池时强制带，区分于外部（外部·aibbs / 外部·…）。
_PROVENANCE = "（内部·萧盛）"

# 素材类型（萧哥拍板的两路：聊天 + 写作协作）
_KINDS = ("chat", "work")
# 单条原始素材正文截断上限（防对话/检索结果过大撑爆理解池）
_BODY_CAP = 2000


def _summarize_web_search(result: Any) -> str:
    """把 web_search 工具返回的原始结构提取成可读文本摘要。

    萧盛写作协作时她替他调 web_search，工具返回可能是：
    - 标准工具格式 ``{content: [{type:"text", text:"..."}]}`` → 直接取文本
    - 检索原始结构 ``{provider, results:[{title, url, snippet/body/...}]}``（如 duckduckgo）
      → 提取每条 title + snippet 拼成可读列表
    避免把整段检索 JSON 写进素材池 / 理解池当噪音（实测理解池曾涌入 20+ 条
    ``{"provider":"duckduckgo","results":[...]}`` 原始 JSON，喂进公开手记/发帖）。
    """
    if isinstance(result, dict):
        # 1) 标准工具内容格式
        content = result.get("content")
        if isinstance(content, list):
            texts = [str(x.get("text", "")) for x in content if isinstance(x, dict) and x.get("text")]
            if texts:
                return "\n".join(texts)
        # 2) 检索原始结构（duckduckgo 等）
        results = result.get("results")
        if isinstance(results, list) and results:
            items = []
            for r in results[:8]:
                if not isinstance(r, dict):
                    continue
                title = (r.get("title") or "").strip()
                snippet = (
                    r.get("snippet") or r.get("body")
                    or r.get("content") or r.get("description") or ""
                )
                snippet = str(snippet).strip()
                line = f"- {title}".rstrip()
                if snippet:
                    line += f"：{snippet}"
                items.append(line)
            if items:
                return "检索到以下资料：\n" + "\n".join(items)
        # 3) 兜底：整段 JSON（仍尽量可读）
        return json.dumps(result, ensure_ascii=False)
    if isinstance(result, (list,)):
        return json.dumps(result, ensure_ascii=False)
    return str(result)


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def _observations_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "xiaosheng_observations.jsonl")


def _understanding_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "observation_understanding.jsonl")


def _state_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "xiaosheng_ingestion_state.json")


def _cooldown_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", "xiaosheng_ingestion_cd.json")


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
        logger.warning("xiaosheng_ingestion: 写状态失败: %s", e)


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
# 原始素材写入（对话轮 / 写作协作）
# ---------------------------------------------------------------------------
def record_xiaosheng_observation(
    base_dir: str,
    kind: str,
    peer: str,
    title: str,
    body: str,
    at: Optional[str] = None,
) -> bool:
    """把一条萧盛影响素材写进原始暂存。

    多入口写（对话轮 post_session_summary + 写作协作 _xiaosheng_note 都可能调），
    用 fcntl 锁保护 append（对齐 diary_write_lock 红线）。去重：同一 (kind, peer, body)
    不重复写。

    Args:
        kind: ``chat``（和萧盛的对话）/ ``work``（替他查资料 / 写作协作）
        peer: 预留（聊天填空，写作协作可填资料主题）
        title: 预留（可填资料查询词 / 写作任务名）
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
        logger.warning("xiaosheng_ingestion: 写原始素材失败: %s", e)
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
        logger.warning("xiaosheng_ingestion: 读原始素材失败: %s", e)
    return out


# ---------------------------------------------------------------------------
# 主入口：消费原始素材 → 提炼 → 写理解池
# ---------------------------------------------------------------------------
def run_xiaosheng_ingestion(
    base_dir: str,
    cfg: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """执行一次萧盛影响摄入（低风险自主行为，fail-open）。

    把攒下的萧盛原始素材（聊天 / 写作协作）精炼成「她从萧盛那里长出来的理解」，
    按同格式写进 ``observation_understanding.jsonl``（带「内部·萧盛」溯源标），
    供 ``worldview_reflection`` 后续回望——萧盛的影响在权重上高于外部。

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
        logger.warning("xiaosheng_ingestion: 提炼异常降级: %s", e)
        segs = []

    # 降级（无 LLM / 提炼失败）：每条原始素材直接转成一段理解（带标），不过滤
    if not segs:
        segs = [{"body": b, "fact_keys": []} for b in bodies]

    # 写理解池（带「内部·萧盛」溯源标，与新闻/AI圈同源同格式）
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
        logger.warning("xiaosheng_ingestion: 写理解池失败: %s", e)

    # 标记已处理（原子）
    processed.update(r.get("key") for r in batch if r.get("key"))
    _save_json(
        _state_path(base_dir),
        {"keys": list(state.get("keys", [])), "processed": list(processed)[:4000]},
    )
    _save_json(cd_path, {"ts": time.time()})

    result["written"] = written
    result["summary"] = (
        f"把 {len(batch)} 条萧盛影响素材沉淀成 {written} 条理解"
        f"（带「内部·萧盛」标）进世界观素材池"
        if written
        else f"{len(batch)} 条萧盛影响素材提炼后无有效理解（已标记处理）"
    )
    return result
