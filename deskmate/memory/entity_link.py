"""确定性实体链接（B-1）：surface → (kind, canonical)，零 LLM。

设计依据
========
``reports/2026-11-11-记忆层改进总方案.md`` §三段 2 的折中方案：
**不上全量 OpenIE + PPR，只做「实体链接 + 一跳扩展」**。本模块只负责前半段：
把 query / chunk 里出现的实体表面形式，归一到一个正名上。

词典来源（全部已有数据，不新增 LLM 调用）
- ``known_persons``（active）：正名 + ``aliases`` 列（当前为空，留给以后）；
- ``learned_history.entity``：``书:XXX`` → 书名；
- ``memory_anchors``：person / book / topic 锚值；
- 可选 ``<base>/entity_aliases.json``：``{"别名": "正名"}``，手工维护的称呼归一
  （``known_persons.aliases`` 全空时，这是唯一的别名来源）。

红线
====
- 不打标、不改写、不新增存储以外的东西；只读词典 + 写 ``memory_chunk_entities``。
- 同优先级冲突（同一 surface 指向两个正名）→ 丢弃该 surface，宁缺不猜。
- 任何异常 fail-open：词典为空 → 调用方回落旧锚通道。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

#: 实体表版本。词典口径变化（新增来源/过滤规则）时 bump，触发一次回填。
ENTITY_VERSION = "2026-11-12-v1"

#: 与锚通道保持一致：过长的锚值（新闻标题）检索价值低且易误命中
MAX_SURFACE = 24
MIN_SURFACE = 2

#: 同一 surface 的优先级：显式别名 > 人名 > 书名 > 主题。
#: 冲突处理只用优先级比较，不做模糊合并。
_PRIORITY = {"person": 3, "book": 2, "topic": 1}
_EXPLICIT = 4

_SPLIT_RE = re.compile(r"[,，;；/|、\s]+")
_NON_WORD_RE = re.compile(r"^[\W_]+$", re.UNICODE)

#: 词典缓存：(ttl 桶, alias_mtime_ns) → 词典。库写入频繁（情绪日志等），
#: 用 db mtime 做 key 会让每轮检索都重建 3700 条词典；改为 5 分钟 TTL。
_TTL_SECONDS = 300.0
_CACHE: dict[str, tuple[tuple[float, int], dict[str, tuple[str, str]]]] = {}


def _norm(text: str) -> str:
    return "".join((text or "").split())


def _valid(surface: str) -> bool:
    if not (MIN_SURFACE <= len(surface) <= MAX_SURFACE):
        return False
    # 纯标点/纯符号不做实体
    return not _NON_WORD_RE.match(surface)


def _read_alias_file(base: Path) -> dict[str, str]:
    """``entity_aliases.json``：{别名: 正名} 或 {别名: {"canonical": 正名}}。"""
    path = base / "entity_aliases.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    if isinstance(data, dict):
        for alias, value in data.items():
            if isinstance(value, str) and value.strip():
                out[str(alias)] = value.strip()
            elif isinstance(value, dict) and value.get("canonical"):
                out[str(alias)] = str(value["canonical"]).strip()
    return out


def load_lexicon(base: Optional[Path | str] = None, force: bool = False) -> dict[str, tuple[str, str]]:
    """构建 surface → (kind, canonical) 词典；结果按 5 分钟 TTL + 别名文件 mtime 缓存。"""
    b = Path(base) if base else _default_base()
    db = b / "deskmate.db"
    alias_path = b / "entity_aliases.json"
    key = str(b)
    stamp = (
        time.time() // _TTL_SECONDS,
        alias_path.stat().st_mtime_ns if alias_path.is_file() else 0,
    )
    cached = _CACHE.get(key)
    if cached and cached[0] == stamp and not force:
        return cached[1]

    mapping: dict[str, tuple[str, str]] = {}
    conflicts: set[str] = set()

    def add(surface: str, canonical: str, kind: str, priority: int) -> None:
        s, c = _norm(surface), _norm(canonical)
        if not _valid(s) or not c or s in conflicts:
            return
        # 书名/主题只收 3 字以上：learned_history 里混进了「书:可能」「书:AI」
        # 这类短词，2 字时几乎全是普通词，做实体通道只会往检索里灌噪声。
        if kind in ("book", "topic") and len(s) < 3:
            return
        old = mapping.get(s)
        if old is None or priority > _PRIORITY.get(old[0], 0):
            mapping[s] = (kind, c)
            return
        if priority == _PRIORITY.get(old[0], 0) and old[1] != c:
            # 同优先级指向不同正名 → 这个 surface 有歧义，整条丢弃
            mapping.pop(s, None)
            conflicts.add(s)

    if db.is_file():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                for name, aliases in con.execute(
                    "SELECT name, COALESCE(aliases,'') FROM known_persons "
                    "WHERE status='active'"
                ):
                    add(name, name, "person", _PRIORITY["person"])
                    for alias in _SPLIT_RE.split(aliases or ""):
                        add(alias, name, "person", _PRIORITY["person"])
                for (entity,) in con.execute(
                    "SELECT DISTINCT entity FROM learned_history "
                    "WHERE entity LIKE '书:%'"
                ):
                    book = (entity or "")[2:].strip()
                    add(book, book, "book", _PRIORITY["book"])
                for kind, value in con.execute(
                    "SELECT DISTINCT anchor_kind, anchor_value FROM memory_anchors "
                    "WHERE anchor_kind IN ('person','book','topic')"
                ):
                    add(value, value, kind, _PRIORITY.get(kind, 1))
            finally:
                con.close()
        except sqlite3.Error as exc:  # 老库没有这些表 → 只用别名文件
            logger.debug("实体词典读取失败（fail-open）：%s", exc)

    for alias, canonical in _read_alias_file(b).items():
        add(canonical, canonical, "person", _EXPLICIT)
        add(alias, canonical, "person", _EXPLICIT)

    _CACHE[key] = (stamp, mapping)
    return mapping


def _default_base() -> Path:
    from deskmate.paths import deskmate_base_dir

    return deskmate_base_dir()


def _first_char_index(
    lexicon: dict[str, tuple[str, str]]
) -> dict[str, list[tuple[str, str, str]]]:
    out: dict[str, list[tuple[str, str, str]]] = {}
    for surface, (kind, canonical) in lexicon.items():
        out.setdefault(surface[0], []).append((surface, kind, canonical))
    return out


def match_entities(
    text: str,
    lexicon: dict[str, tuple[str, str]],
    limit: int = 8,
) -> list[tuple[str, str, str]]:
    """返回 ``[(kind, canonical, surface)]``；按 surface 长度降序，同正名只留最长。

    只用子串扫描（实体名普遍 2~6 字，QPS 足够）；首字索引把 3000+ 条词典
    收敛到实际出现的十几个首字上。
    """
    t = _norm(text)
    if not t or not lexicon:
        return []
    if len(t) > 2000:  # chunk 上限 600，超长输入（误用）直接放弃，避免拖慢检索
        t = t[:2000]
    index = _first_char_index(lexicon)
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for ch in set(t):
        for surface, kind, canonical in index.get(ch, ()):
            if surface in t and canonical not in seen:
                seen.add(canonical)
                found.append((kind, canonical, surface))
    found.sort(key=lambda x: len(x[2]), reverse=True)
    return found[:limit]


def iter_chunk_entities(
    bodies: Iterable[tuple[str, str]],
    lexicon: dict[str, tuple[str, str]],
) -> Iterable[tuple[str, str, str, str]]:
    """``(chunk_id, body)`` → ``(chunk_id, kind, canonical, surface)``。"""
    if not lexicon:
        return
    for chunk_id, body in bodies:
        for kind, canonical, surface in match_entities(body, lexicon, limit=24):
            yield chunk_id, kind, canonical, surface
