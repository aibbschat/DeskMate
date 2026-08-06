"""创作圆桌 · 潮流简报（手动沉淀，多期归档）。

设计定位（与用户确认）：
- 纯手动沉淀：作者自己写/贴市场观察，可选让 LLM 整理成标准格式。不联网、不自动抓取。
- 多期归档可回看：每期独立落盘，可「设为当前」切换，圆桌消费「当前生效」那期。
- 与圆桌运行解耦：不评稿、不打开书稿时也能随时沉淀一期。

存储位置：~/.openworker/roundtable/trends/
  index.json      各期清单 [{id, date, genre, title}]
  latest.json     {"id": <当前生效 id>}
  trend_<id>.md   每期正文
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 需要注入潮流简报的市场化人格（其余人格不被潮流污染）。
# 从 personas.PERSONAS 中 market=True 的角色动态派生，避免与 personas.py 双重维护导致漏改。
from deskmate.roundtable.personas import PERSONAS

MARKET_PERSONA_IDS = {p.id for p in PERSONAS if p.market}

# 每个市场化人格关注的标准章节（潮流简报按 ## 标题切片）
_PERSONA_SECTIONS = {
    "publisher": ["流行元素", "类型热点"],
    "adaptation": ["影视风向"],
    "genre": ["类型热点"],
}


def store_root() -> Path:
    p = Path.home() / ".openworker" / "roundtable" / "trends"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _index_file() -> Path:
    return store_root() / "index.json"


def _latest_file() -> Path:
    return store_root() / "latest.json"


def list_briefs() -> list[dict]:
    """返回各期清单（不含正文），按日期倒序。"""
    fp = _index_file()
    if not fp.is_file():
        return []
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("trend index parse failed: %s", e)
        return []
    return list(reversed(sorted(data, key=lambda x: x.get("id", ""))))


def get_latest() -> Optional[dict]:
    """当前生效的那期（含正文），无则返回 None。"""
    lf = _latest_file()
    if not lf.is_file():
        return None
    try:
        bid = json.loads(lf.read_text(encoding="utf-8")).get("id")
    except Exception as e:
        logger.warning("trend latest pointer parse failed: %s", e)
        return None
    if not bid:
        return None
    return get_brief(bid)


def get_brief(bid: str) -> Optional[dict]:
    """按 id 取一期（含正文）。"""
    fp = store_root() / f"trend_{bid}.md"
    if not fp.is_file():
        return None
    content = fp.read_text(encoding="utf-8")
    meta = next((m for m in list_briefs() if m.get("id") == bid), {})
    return {
        "id": bid,
        "date": meta.get("date", ""),
        "genre": meta.get("genre", ""),
        "title": meta.get("title", ""),
        "content": content,
    }


def save_brief(markdown: str, genre: str = "", title: str = "") -> dict:
    """沉淀一期：生成 id、落盘正文、更新 index 与 latest。返回该期元信息。"""
    now = datetime.now()
    bid = now.strftime("%Y%m%d_%H%M%S")
    date = now.strftime("%Y-%m-%d")
    # 防止同秒碰撞
    while (store_root() / f"trend_{bid}.md").is_file():
        now = datetime.now()
        bid = now.strftime("%Y%m%d_%H%M%S")
    (store_root() / f"trend_{bid}.md").write_text(markdown, encoding="utf-8")

    meta = {"id": bid, "date": date, "genre": (genre or "").strip(), "title": (title or "").strip()}
    idx = _index_file()
    arr = []
    if idx.is_file():
        try:
            arr = json.loads(idx.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("trend index parse failed during save, resetting: %s", e)
            arr = []
    arr.append(meta)
    idx.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
    _latest_file().write_text(json.dumps({"id": bid}, ensure_ascii=False), encoding="utf-8")
    return meta


def set_active(bid: str) -> bool:
    """切换当前生效那期。"""
    if not (store_root() / f"trend_{bid}.md").is_file():
        return False
    _latest_file().write_text(json.dumps({"id": bid}, ensure_ascii=False), encoding="utf-8")
    return True


def update_brief(bid: str, markdown: str, genre: str = "", title: str = "") -> Optional[dict]:
    """覆盖更新某一期：保留 id/date，重写正文并刷新 index 的 genre/title。不存在返回 None。"""
    fp = store_root() / f"trend_{bid}.md"
    if not fp.is_file():
        return None
    fp.write_text(markdown, encoding="utf-8")
    idx = _index_file()
    arr = []
    if idx.is_file():
        try:
            arr = json.loads(idx.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("trend index parse failed during update, resetting: %s", e)
            arr = []
    for m in arr:
        if m.get("id") == bid:
            m["genre"] = (genre or "").strip()
            m["title"] = (title or "").strip()
            break
    idx.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
    return get_brief(bid)


def delete_brief(bid: str) -> bool:
    """删除某一期；若删的是当前生效，则 latest 自动改为剩余最新一期（无则清空）。成功返回 True。"""
    fp = store_root() / f"trend_{bid}.md"
    if not fp.is_file():
        return False
    fp.unlink()
    idx = _index_file()
    arr = []
    if idx.is_file():
        try:
            arr = json.loads(idx.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("trend index parse failed during delete, resetting: %s", e)
            arr = []
    arr = [m for m in arr if m.get("id") != bid]
    idx.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
    # 若删除的恰是当前生效，重置 latest
    lf = _latest_file()
    cur = None
    if lf.is_file():
        try:
            cur = json.loads(lf.read_text(encoding="utf-8")).get("id")
        except Exception as e:
            logger.warning("trend latest pointer parse failed during delete: %s", e)
            cur = None
    if cur == bid:
        new_id = max((m.get("id", "") for m in arr), default=None)
        if new_id:
            lf.write_text(json.dumps({"id": new_id}, ensure_ascii=False), encoding="utf-8")
        elif lf.is_file():
            lf.unlink()
    return True


def _sections(md: str) -> dict:
    """把 markdown 按 ## 标题切成 {标题: 正文}。"""
    out: dict = {}
    cur: Optional[str] = None
    buf: list[str] = []
    for line in md.splitlines():
        m = re.match(r"^##\s+(.*)$", line)
        if m:
            if cur is not None:
                out[cur] = "\n".join(buf).strip()
            cur = m.group(1).strip()
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf).strip()
    return out


def trend_for_persona(persona_id: str) -> str:
    """构造该市场化人格的潮流注入片段；非市场人格或无简报则返回空串。"""
    secs = _PERSONA_SECTIONS.get(persona_id)
    if not secs:
        return ""
    b = get_latest()
    if not b:
        return ""
    sec_map = _sections(b.get("content", ""))
    parts: list[str] = []
    for s in secs:
        body = sec_map.get(s)
        if body:
            parts.append(f"### {s}\n{body}")
    if not parts:
        return ""
    date = b.get("date", "")
    title = b.get("title", "")
    head = (
        f"【当下潮流参考 · 截至 {date}{(' · ' + title) if title else ''}】\n"
        "以下是你作市场判断时应参考的当下行业风向（由作者手动沉淀，会随市场变化而更新，"
        "请结合书稿本身，不要脱离文本空谈潮流）："
    )
    return head + "\n\n" + "\n\n".join(parts)


def build_via_llm(raw_notes: str, genre: str = "") -> str:
    """可选：把作者的零散观察交给 LLM 整理成标准三章节简报（不联网，仅结构化）。

    失败或 LLM 未就绪时原样返回散记。
    """
    from deskmate.memory.llm import chat, llm_available

    if not llm_available():
        return raw_notes.strip()
    today = datetime.now().strftime("%Y-%m-%d")
    system = (
        "你是市场趋势编辑。请把作者提供的零散观察，整理成一份结构化的「潮流简报」，"
        "使用 Markdown，必须包含三个二级标题：## 流行元素、## 影视风向、## 类型热点。"
        "语言精炼、可直接用于书稿评审参考；只归纳作者给出的信息，不要编造原文没有的具体数据或书名。"
    )
    user = f"类型视角：{genre or '通用'}\n\n原始观察：\n{raw_notes}"
    try:
        out = chat(system, user, temperature=0.4, max_tokens=2000)
    except Exception as e:
        logger.debug("LLM trend briefing build failed, returning raw notes: %s", e)
        return raw_notes.strip()
    if not out or not out.strip():
        return raw_notes.strip()
    header = f"# 潮流简报 · {today}\n\n> 类型视角：{genre or '通用'}\n\n"
    return header + out.strip()
