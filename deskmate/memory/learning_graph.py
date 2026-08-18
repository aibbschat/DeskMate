"""「学习可视化」图数据（对标 Hermes learning_graph.py，适配桌搭）。

把桌搭实际学到的东西组装成一张图，回答「我学到的技能和记住的事之间
有什么关联」：
- 技能节点：SkillStore.list_skills()（自研扁平 .md + imported 社区包），
  带 use_count / last_used / category / source（agent 自研 or imported）。
- 记忆节点：~/.deskmate 下 MEMORY.md / USER.md / longterm.md 按「## 块」切成卡片。
- 技能↔技能边：共享 tags（桌搭技能无 related_skills 字段，tags 即显式关联声明）。
- 记忆↔技能边：技能 id 出现在记忆文本（强边 +6）+ 中文 2-gram 重叠 ≥2（弱边）。

输出与 Hermes 同构的 payload：{nodes, edges, clusters, memory, stats}，
前端「学习面板」可直接消费；也可 `python -m deskmate.memory.learning_graph`
直接打印统计。纯文件扫描 + 字符串运算，零 LLM 调用，fail-open。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# 记忆文件 -> 来源标签（顺序即展示优先级）
_MEMORY_FILES = (
    ("USER.md", "profile"),
    ("MEMORY.md", "memory"),
    ("longterm.md", "longterm"),
)

_EN_WORD = re.compile(r"[a-z0-9]{2,}")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _default_base() -> Path:
    return Path(os.environ.get("DESKMATE_DIR", str(Path.home() / ".deskmate")))


def build_skill_nodes(base_dir: Path | None = None) -> list[dict[str, Any]]:
    """从 SkillStore 读全部技能，组装图节点（纯文件扫描，零 LLM）。"""
    base = base_dir or _default_base()
    try:
        from deskmate.skills.skill_store import SkillStore

        store = SkillStore(skills_dir=base / "skills")
        skills = store.list_skills(limit=10_000)
    except Exception:
        return []
    nodes: list[dict[str, Any]] = []
    for s in skills:
        sid = str(s.get("name", "")).strip()
        if not sid:
            continue
        path = str(s.get("path", ""))
        imported = "imported" in path.replace("\\", "/").split("/")
        nodes.append(
            {
                "id": sid,
                "label": sid,
                "kind": "skill",
                "category": str(s.get("category", "general")),
                "source": "imported" if imported else "agent",
                "useCount": int(s.get("uses", 0) or 0),
                "lastUsed": str(s.get("last_used", "")),
                "description": str(s.get("description", ""))[:200],
                "tags": [str(t) for t in (s.get("tags") or [])][:8],
            }
        )
    return nodes


def _memory_cards(base_dir: Path) -> list[dict[str, Any]]:
    """记忆文件按「## 标题块」切成卡片；无标题时整文件作一张卡。"""
    cards: list[dict[str, Any]] = []
    for fname, source in _MEMORY_FILES:
        path = base_dir / fname
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            ts = path.stat().st_mtime
        except OSError:
            continue
        if not text:
            continue
        chunks = [c.strip() for c in re.split(r"\n(?=## )", text) if c.strip()]
        if not chunks:
            chunks = [text]
        for idx, chunk in enumerate(chunks):
            lines = chunk.splitlines()
            title = lines[0].lstrip("# ").strip() if lines else source
            cards.append(
                {
                    "source": source,
                    "timestamp": int(ts) + idx,
                    "title": (title[:80] + "…") if len(title) > 80 else title,
                    "body": chunk[:1200],
                }
            )
    return cards


def _bigrams(text: str) -> set[str]:
    """中文 2-gram：单字太碎、词表又缺，bigram 是跨文本重叠检测的务实折中。"""
    chars = _CJK.findall(text.lower())
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}


def _skill_skill_edges(skill_nodes: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """共享 tags 的两个技能连边（双向都存在、去重）。"""
    edges: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for i, a in enumerate(skill_nodes):
        a_tags = set(a.get("tags", []))
        if not a_tags:
            continue
        for b in skill_nodes[i + 1 :]:
            if a_tags & set(b.get("tags", [])):
                key = tuple(sorted((a["id"], b["id"])))
                if key not in seen:
                    seen.add(key)
                    edges.append(key)
    return edges


def _memory_skill_edges(
    memory_cards: list[dict[str, Any]], skill_nodes: list[dict[str, Any]]
) -> list[tuple[str, str]]:
    """记忆↔技能边：技能 id 出现在记忆文本（强 +6）；中文 bigram 重叠 ≥2（弱）。

    每张记忆卡最多连 4 条边（对齐 Hermes），避免图被弱关联刷爆。
    """
    edges: list[tuple[str, str]] = []
    for idx, card in enumerate(memory_cards):
        mem_id = f"memory:{card['source']}:{idx}"
        text = f"{card.get('title', '')}\n{card.get('body', '')}".lower()
        text_bigrams = _bigrams(text)
        scored: list[tuple[int, str]] = []
        for n in skill_nodes:
            score = 0
            sid = n["id"].lower()
            if sid and sid in text:
                score += 6
            overlap = len(_bigrams(n["id"] + " " + n["description"]) & text_bigrams)
            if overlap >= 2:
                score += overlap
            if score > 0:
                scored.append((score, n["id"]))
        scored.sort(key=lambda x: (-x[0], x[1]))
        for _, sid in scored[:4]:
            edges.append((mem_id, sid))
    return edges


def _density_stats(
    skill_nodes: list[dict[str, Any]], skill_edges: list[tuple[str, str]]
) -> dict[str, Any]:
    linked: set[str] = set()
    for a, b in skill_edges:
        linked.add(a)
        linked.add(b)
    cats: dict[str, int] = {}
    for n in skill_nodes:
        cats[n["category"]] = cats.get(n["category"], 0) + 1
    n = len(skill_nodes) or 1
    return {
        "skills": len(skill_nodes),
        "skill_edges": len(skill_edges),
        "edges_per_skill": round(len(skill_edges) / n, 3),
        "linked_skills": len(linked),
        "isolated_pct": round(100 * (n - len(linked)) / n, 1),
        "categories": len(cats),
        "used": sum(1 for x in skill_nodes if x["useCount"] > 0),
        "top_categories": sorted(cats.items(), key=lambda kv: -kv[1])[:8],
    }


def build_learning_graph(base_dir: Path | None = None) -> dict[str, Any]:
    """完整图 payload（节点 / 边 / 簇 / 记忆卡 / 统计）。"""
    base = base_dir or _default_base()
    skills = build_skill_nodes(base)
    skill_edges = _skill_skill_edges(skills)
    cards = _memory_cards(base)
    memory_edges = _memory_skill_edges(cards, skills)
    edges = skill_edges + memory_edges

    clusters: dict[str, int] = {}
    for n in skills:
        clusters[n["category"]] = clusters.get(n["category"], 0) + 1
    if cards:
        clusters["memory"] = len(cards)

    nodes: list[dict[str, Any]] = [
        {
            "id": n["id"],
            "label": n["id"],
            "kind": "skill",
            "category": n["category"],
            "source": n["source"],
            "useCount": n["useCount"],
            "lastUsed": n["lastUsed"],
            "description": n["description"],
            "tags": n["tags"],
        }
        for n in skills
    ]
    for i, card in enumerate(cards):
        nodes.append(
            {
                "id": f"memory:{card['source']}:{i}",
                "label": card["title"],
                "kind": "memory",
                "memorySource": card["source"],
                "category": "memory",
                "source": card["source"],
                "useCount": 0,
                "lastUsed": "",
                "description": card["body"],
                "tags": [],
            }
        )

    return {
        "nodes": nodes,
        "edges": [{"source": a, "target": b} for a, b in edges],
        "clusters": [
            {"category": c, "count": n}
            for c, n in sorted(clusters.items(), key=lambda kv: -kv[1])
        ],
        "memory": cards,
        "stats": {
            **_density_stats(skills, skill_edges),
            "memory_nodes": len(cards),
            "memory_skill_edges": len(memory_edges),
        },
    }


if __name__ == "__main__":
    import json

    g = build_learning_graph()
    print(json.dumps(g.get("stats", {}), ensure_ascii=False, indent=2))
