"""P2-6「它眼中的我」画像聚合——6 个栏目数据聚合。

把分散在各模块的学习成果聚合成统一的「它眼中的我」画像，供前端页面展示。

6 个栏目（网站式架构：栏目 → 文章）：
1. writing_style      写作风格（P1-5 style_profile.json）
2. decision_preference 决策偏好（P2-6 decision_profile.json）
3. behavior_patterns   行为规律（P2-3 behavior_patterns）
4. reminder_preferences 提醒偏好（P1-2 preferences.json）
5. dream_activity      后台整理（P2-1 dream_report + traces/dream.jsonl）
6. privacy_cross_project 隐私与跨项目（8.4 敏感分级 + 8.5 跨项目注入）

每个栏目结构：
- title / icon / status（ready/learning/empty）
- headline（头条摘要，首页展示）
- articles（具体"文章"列表，点击栏目后展示，朋友圈式设计）

零 LLM、纯读取聚合、fail-open。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


def _base_dir() -> Path:
    return Path(os.environ.get("DESKMATE_DIR", str(Path.home() / ".deskmate")))


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _read_jsonl(path: Path, limit: int = 20) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        results = []
        for line in lines[-limit:]:
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    results.append(obj)
            except (ValueError, json.JSONDecodeError):
                continue
        return results
    except OSError:
        return []


def _fmt_date(ts: str | float | None) -> str:
    if not ts:
        return ""
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        if isinstance(ts, str):
            m = __import__("re").match(r"(\d{4}-\d{2}-\d{2})", ts)
            return m.group(1) if m else ts[:10]
    except Exception:
        pass
    return str(ts)[:10]


# ---------------------------------------------------------------------------
# 栏目 1：写作风格
# ---------------------------------------------------------------------------
def _writing_style_section() -> dict[str, Any]:
    style = _read_json(_base_dir() / "style_profile.json")
    metrics = style.get("metrics") or {}
    n = metrics.get("n", 0)
    chars = metrics.get("chars", 0)

    articles: list[dict[str, Any]] = []

    # 句长分布
    sent = metrics.get("sentences") or {}
    if sent:
        parts = []
        if sent.get("avg_len"):
            parts.append(f"平均 {sent['avg_len']:.0f} 字")
        if sent.get("median_len"):
            parts.append(f"中位 {sent['median_len']:.0f} 字")
        if sent.get("short_pct") is not None:
            parts.append(f"短句 {sent['short_pct']:.0f}%")
        if sent.get("long_pct") is not None:
            parts.append(f"长句 {sent['long_pct']:.0f}%")
        if parts:
            articles.append({
                "title": "句长偏好",
                "content": "，".join(parts),
                "evidence": f"从 {n} 个样本（{chars:.0f} 字）学习",
                "tags": ["量化"],
            })

    # 文言/白话
    classical = metrics.get("classical_per_1k")
    modern = metrics.get("modern_per_1k")
    if classical is not None or modern is not None:
        parts = []
        if classical is not None:
            parts.append(f"文言虚词 {classical:.1f}/千字")
        if modern is not None:
            parts.append(f"现代语气词 {modern:.1f}/千字")
        if parts:
            articles.append({
                "title": "文白倾向",
                "content": "，".join(parts),
                "evidence": "文言密度越高，文风越偏古典",
                "tags": ["量化"],
            })

    # 四字格（兼容两种结构：直接数值 或 dict）
    four_raw = metrics.get("four_char")
    if isinstance(four_raw, (int, float)) and four_raw > 0:
        articles.append({
            "title": "四字格倾向",
            "content": f"四字格密度 {four_raw:.1f}",
            "evidence": "四字格多 = 文风简练、有节奏感",
            "tags": ["量化"],
        })
    elif isinstance(four_raw, dict) and four_raw.get("pct_of_chunk") is not None:
        articles.append({
            "title": "四字格倾向",
            "content": f"{four_raw['pct_of_chunk']:.1f}% 的词块为四字格",
            "evidence": "四字格多 = 文风简练、有节奏感",
            "tags": ["量化"],
        })

    # 对话占比（兼容两种结构：直接数值 或 dict）
    dialogue_raw = metrics.get("dialogue_pct")
    dialogue = metrics.get("dialogue") or {}
    if dialogue_raw is not None:
        articles.append({
            "title": "对话占比",
            "content": f"{dialogue_raw:.0f}% 文字为对话",
            "evidence": "对话多 = 偏叙事、人物驱动",
            "tags": ["量化"],
        })
    else:
        dialogue = metrics.get("dialogue") or {}
        if dialogue and dialogue.get("pct") is not None:
            articles.append({
                "title": "对话占比",
                "content": f"{dialogue['pct']:.0f}% 文字为对话",
                "evidence": "对话多 = 偏叙事、人物驱动",
                "tags": ["量化"],
            })

    # 定性描述（features 可能是 dict 或 list）
    summary = style.get("summary")
    features_raw = style.get("features") or {}
    if isinstance(features_raw, dict):
        feature_items = list(features_raw.items())
    elif isinstance(features_raw, list):
        feature_items = [(f"特征{i+1}", f) for i, f in enumerate(features_raw) if isinstance(f, str)]
    else:
        feature_items = []

    if summary:
        articles.append({
            "title": "风格总评",
            "content": summary,
            "evidence": "AI 提炼的定性描述",
            "tags": ["定性"],
        })
    dim_labels = {
        "sentence_style": "句式风格",
        "register": "文白语体",
        "rhythm": "叙事节奏",
        "word_preference": "用词偏好",
        "taboos": "避讳禁忌",
    }
    for dim, desc in feature_items[:4]:
        if isinstance(desc, str) and desc.strip():
            articles.append({
                "title": dim_labels.get(dim, dim),
                "content": desc.strip()[:120],
                "evidence": "从样本中提炼",
                "tags": ["定性"],
            })

    # 状态判断
    if n >= 2 and articles:
        status = "ready"
        # 头条：取最显著的指标
        headline_parts = []
        if sent and sent.get("long_pct", 0) > 40:
            headline_parts.append("句长偏长")
        elif sent and sent.get("short_pct", 0) > 40:
            headline_parts.append("短句为主")
        if classical is not None and classical > 3:
            headline_parts.append("文言密度高")
        dlg = metrics.get("dialogue_pct")
        if dlg is not None and dlg > 30:
            headline_parts.append("对话驱动")
        elif dialogue and dialogue.get("pct", 0) > 30:
            headline_parts.append("对话驱动")
        headline = " · ".join(headline_parts) if headline_parts else "正在形成中"
    elif n >= 1:
        status = "learning"
        headline = "样本不足，还在学习中"
    else:
        status = "empty"
        headline = "还没有写作风格数据，多创作就会长出来"

    return {
        "id": "writing_style",
        "title": "写作风格",
        "icon": "✍️",
        "status": status,
        "headline": headline,
        "articles": articles,
        "updated_at": _fmt_date(style.get("updated_at") or style.get("last_updated")),
    }


# ---------------------------------------------------------------------------
# 栏目 2：决策偏好
# ---------------------------------------------------------------------------
def _decision_preference_section() -> dict[str, Any]:
    from deskmate.alter_ego.decision_profile import load_profile

    profile = load_profile()
    articles: list[dict[str, Any]] = []

    # 风险偏好
    if profile.risk:
        vals = [f"{v}（{c} 次）" for v, c in profile.risk.most_common()]
        articles.append({
            "title": "风险取舍",
            "content": "、".join(vals),
            "evidence": "从「保守一点/直接做吧」等表述学习",
            "tags": ["决策"],
        })

    # 先后顺序
    if profile.sequence:
        vals = [f"{v}（{c} 次）" for v, c in profile.sequence.most_common()]
        articles.append({
            "title": "做事顺序",
            "content": "、".join(vals),
            "evidence": "从「先…再…」「首先…然后…」学习",
            "tags": ["决策"],
        })

    # 价值权衡
    if profile.tradeoff:
        vals = [f"{v}（{c} 次）" for v, c in profile.tradeoff.most_common()]
        articles.append({
            "title": "价值权衡",
            "content": "、".join(vals),
            "evidence": "从「比起X我更看重Y」学习",
            "tags": ["决策"],
        })

    # 改动倾向
    if profile.change:
        vals = [f"{v}（{c} 次）" for v, c in profile.change.most_common()]
        articles.append({
            "title": "改动倾向",
            "content": "、".join(vals),
            "evidence": "从「尽量不改/彻底重构」学习",
            "tags": ["决策"],
        })

    # 信息风格
    if profile.info_style:
        vals = [f"{v}（{c} 次）" for v, c in profile.info_style.most_common()]
        articles.append({
            "title": "信息偏好",
            "content": "、".join(vals),
            "evidence": "从「对比一下/直接告诉我结论」学习",
            "tags": ["决策"],
        })

    if profile.total_signals >= 3 and articles:
        status = "ready"
        # 头条：取最显著的维度
        top = articles[0]
        headline = f"{top['title']}：{top['content'][:20]}"
    elif profile.total_signals >= 1:
        status = "learning"
        headline = f"已学 {profile.total_signals} 个信号，还在积累中"
    else:
        status = "empty"
        headline = "还没有决策偏好数据，做选择时多说几句就会长出来"

    return {
        "id": "decision_preference",
        "title": "决策偏好",
        "icon": "🎯",
        "status": status,
        "headline": headline,
        "articles": articles,
        "updated_at": _fmt_date(profile.updated_at),
    }


# ---------------------------------------------------------------------------
# 栏目 3：行为规律
# ---------------------------------------------------------------------------
def _behavior_patterns_section() -> dict[str, Any]:
    try:
        from deskmate.proactivity.behavior_patterns import mine_patterns

        res = mine_patterns(base_dir=str(_base_dir()), days=120)
        patterns = res.get("patterns", [])
        event_count = res.get("event_count", 0)
    except Exception:
        patterns = []
        event_count = 0

    articles: list[dict[str, Any]] = []
    for p in patterns[:8]:
        if isinstance(p, dict):
            kind = p.get("kind", "")
            desc = p.get("description") or p.get("pattern") or ""
            confidence = p.get("confidence")
            evidence = p.get("evidence") or f"从 {event_count} 个行为事件学习"
            kind_label = {
                "weekday_cadence": "星期规律",
                "hour_cadence": "时段规律",
                "recurrence": "稳定节奏",
                "deferral": "推迟规律",
            }.get(kind, kind)
            articles.append({
                "title": kind_label,
                "content": desc,
                "evidence": evidence + (f"（置信度 {confidence:.0%}）" if confidence else ""),
                "tags": ["行为"],
            })

    if articles and event_count >= 10:
        status = "ready"
        headline = articles[0]["content"][:30]
    elif articles:
        status = "learning"
        headline = f"发现 {len(articles)} 条规律，样本还在积累"
    else:
        status = "empty"
        headline = "还没有行为规律，用久了就会长出来"

    return {
        "id": "behavior_patterns",
        "title": "行为规律",
        "icon": "📅",
        "status": status,
        "headline": headline,
        "articles": articles,
        "updated_at": _fmt_date(datetime.now().isoformat()),
    }


# ---------------------------------------------------------------------------
# 栏目 4：提醒偏好
# ---------------------------------------------------------------------------
def _reminder_preferences_section() -> dict[str, Any]:
    prefs = _read_json(_base_dir() / "proactivity" / "preferences.json")
    items = prefs.get("preferences") or prefs.get("items") or []

    articles: list[dict[str, Any]] = []
    if isinstance(items, dict):
        iterable = items.items()
    elif isinstance(items, list):
        iterable = [(it.get("type", ""), it) for it in items if isinstance(it, dict)]
    else:
        iterable = []

    for stype, data in iterable:
        if not stype:
            continue
        count = data.get("count", data.get("accepted", 0)) if isinstance(data, dict) else 0
        last = data.get("last_accepted_at", data.get("last_ts", "")) if isinstance(data, dict) else ""
        if count >= 2:
            articles.append({
                "title": f"常接受「{stype}」提醒",
                "content": f"已接受 {count} 次",
                "evidence": f"最近一次：{_fmt_date(last) or '近期'}" if last else "从接受记录学习",
                "tags": ["偏好"],
            })

    if articles:
        status = "ready"
        headline = articles[0]["title"]
    elif items:
        status = "learning"
        headline = "有一些提醒记录，还在观察中"
    else:
        status = "empty"
        headline = "还没有提醒偏好，多接受建议就会长出来"

    return {
        "id": "reminder_preferences",
        "title": "提醒偏好",
        "icon": "🔔",
        "status": status,
        "headline": headline,
        "articles": articles,
        "updated_at": _fmt_date(prefs.get("last_updated")),
    }


# ---------------------------------------------------------------------------
# 栏目 5：后台整理（Dreaming）
# ---------------------------------------------------------------------------
def _dream_activity_section() -> dict[str, Any]:
    dream_log = _read_jsonl(_base_dir() / "traces" / "dream.jsonl", limit=10)
    dream_report = _base_dir() / "dream_report.md"

    articles: list[dict[str, Any]] = []
    for entry in reversed(dream_log):
        ts = entry.get("ts", entry.get("time", ""))
        merged = entry.get("merged", entry.get("merged_count", 0))
        contradictions = entry.get("contradictions_resolved", entry.get("contradictions", 0))
        stale = entry.get("stale_superseded", entry.get("stale", 0))
        parts = []
        if merged:
            parts.append(f"合并重复 {merged} 条")
        if contradictions:
            parts.append(f"标注矛盾 {contradictions} 条")
        if stale:
            parts.append(f"更新过时 {stale} 条")
        if parts:
            articles.append({
                "title": "记忆整理",
                "content": "，".join(parts),
                "evidence": _fmt_date(ts),
                "tags": ["后台"],
            })

    if articles:
        status = "ready"
        headline = articles[0]["content"]
    elif dream_report.is_file():
        status = "learning"
        headline = "有整理报告，正在读取"
    else:
        status = "empty"
        headline = "还没有后台整理记录，AI 会在空闲时自动整理记忆"

    return {
        "id": "dream_activity",
        "title": "它在后台整理",
        "icon": "🧹",
        "status": status,
        "headline": headline,
        "articles": articles,
        "updated_at": _fmt_date(dream_log[-1].get("ts") if dream_log else None),
    }


# ---------------------------------------------------------------------------
# 栏目 6：隐私与跨项目
# ---------------------------------------------------------------------------
def _privacy_cross_project_section() -> dict[str, Any]:
    # 隐私：读 MEMORY.md 中的 #private / #sensitive 标签
    memory_path = _base_dir() / "MEMORY.md"
    private_count = 0
    sensitive_count = 0
    private_items: list[str] = []
    if memory_path.is_file():
        try:
            content = memory_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                if "#private" in line or "#no-sync" in line:
                    private_count += 1
                    if len(private_items) < 3:
                        clean = line.replace("#private", "").replace("#no-sync", "").replace("#sensitive", "").strip()
                        if clean and len(clean) < 60:
                            private_items.append(clean)
                elif "#sensitive" in line:
                    sensitive_count += 1
        except OSError:
            pass

    # 跨项目：检查 global_profile 是否存在
    global_profile_exists = (_base_dir() / "style_profile.json").is_file()

    articles: list[dict[str, Any]] = []

    if private_count > 0:
        articles.append({
            "title": "隐私记忆保护",
            "content": f"{private_count} 条记忆标记为隐私，不会同步到其他设备",
            "evidence": "敏感信息自动分级保护",
            "tags": ["隐私"],
        })
    if sensitive_count > 0:
        articles.append({
            "title": "敏感记忆",
            "content": f"{sensitive_count} 条记忆标记为敏感",
            "evidence": "日志中自动脱敏",
            "tags": ["隐私"],
        })
    for item in private_items:
        articles.append({
            "title": "隐私条目",
            "content": item,
            "evidence": "已标记 #private #no-sync",
            "tags": ["隐私"],
        })

    if global_profile_exists:
        articles.append({
            "title": "跨项目延续",
            "content": "开新书时自动注入你的一贯创作偏好",
            "evidence": "全局风格画像 + 写作风格专档",
            "tags": ["跨项目"],
        })

    total = private_count + sensitive_count
    if articles:
        status = "ready"
        headline = f"{total} 条隐私记忆受保护 · 跨项目自动延续" if total else "跨项目偏好自动注入"
    elif global_profile_exists:
        status = "learning"
        headline = "跨项目偏好已就绪"
    else:
        status = "empty"
        headline = "还没有隐私或跨项目数据"

    return {
        "id": "privacy_cross_project",
        "title": "隐私与跨项目",
        "icon": "🔒",
        "status": status,
        "headline": headline,
        "articles": articles,
        "updated_at": _fmt_date(datetime.now().isoformat()),
    }


# ---------------------------------------------------------------------------
# 栏目 7：作品与创作（auto_tasks 统一框架——每日读书/进度跟踪/未来新任务）
# ---------------------------------------------------------------------------
def _auto_tasks_section() -> dict[str, Any]:
    """从 auto_tasks 统一框架聚合「作品与创作」栏目。

    涵盖：每日读书（已出版作品阅读心得）、进度跟踪（正在写的书的伏笔/人物状态）、
    以及未来新设的任何与"我/写作/作品"相关的自动任务——自动落地、自动分类、自动展示。
    """
    try:
        from deskmate.auto_tasks import get_recorder
        recorder = get_recorder()
        records = recorder.get_timeline(limit=30)
        insights = recorder.get_insights(limit=20)
    except Exception:
        records = []
        insights = []

    articles: list[dict[str, Any]] = []

    # 时间线记录 → 文章（朋友圈式）
    for rec in records:
        # 动作标签
        action_labels = {
            "start": "开始读",
            "continue": "继续读",
            "finish": "读完了",
            "analyze": "跟踪检查",
            "remind": "主动提醒",
        }
        action_label = action_labels.get(rec.action, rec.action)

        # 组装内容
        content_parts = []
        if rec.summary:
            content_parts.append(rec.summary)
        if rec.insights:
            insight_texts = [i.text for i in rec.insights[:3]]
            if insight_texts:
                content_parts.append("洞察：" + "；".join(insight_texts))

        content = "\n".join(content_parts) if content_parts else f"{action_label} {rec.target}"

        tags = [rec.task_name] + (rec.tags or [])
        # 去重
        tags = list(dict.fromkeys(tags))[:4]

        articles.append({
            "title": f"{action_label} {rec.target}",
            "content": content,
            "evidence": rec.date,
            "tags": tags,
        })

    # 统计
    stats = recorder.get_stats() if records else {}
    by_type = stats.get("by_type", {})
    unique_targets = stats.get("unique_targets", 0)
    total_insights = len(insights)

    # 计算持续天数
    if records:
        dates = sorted(set(r.date for r in records))
        duration_days = len(dates)
    else:
        duration_days = 0

    if articles:
        status = "ready"
        headline = f"已读 {by_type.get('daily_reading', 0)} 本 / 跟踪 {by_type.get('progress_tracking', 0)} 次 / 沉淀 {total_insights} 条洞察 / 持续 {duration_days} 天"
    elif insights:
        status = "learning"
        headline = f"沉淀了 {total_insights} 条洞察，正在整理"
    else:
        status = "empty"
        headline = "还没有作品阅读与跟踪记录。设定每日读书和进度跟踪后，小桔会持续学习你的作品。"

    return {
        "id": "auto_tasks",
        "title": "作品与创作",
        "icon": "📖",
        "status": status,
        "headline": headline,
        "articles": articles,
        "updated_at": _fmt_date(records[0].date if records else None),
    }


# ---------------------------------------------------------------------------
# 栏目 8：它理解的我（认知层——立场/论证模式/价值观/思维框架）
# ---------------------------------------------------------------------------
def _cognitive_section() -> dict[str, Any]:
    """认知画像栏目——从认知层（cognitive_profile）聚合数据。"""
    try:
        from deskmate.cognitive import get_profile_manager
        profile = get_profile_manager().get()
    except Exception:
        profile = None

    articles: list[dict[str, Any]] = []

    if profile:
        for s in profile.active_stances():
            source_names = [src.title for src in s.sources[:2]]
            articles.append({
                "title": f"立场：{s.topic or '未分类'}",
                "content": s.content,
                "evidence": f"置信度 {s.confidence:.0%} · 来源：{', '.join(source_names) if source_names else '未知'}",
                "tags": ["立场"] + s.tags[:2],
                "item_id": s.id,
                "dimension": "stance",
            })
        for p in profile.active_patterns():
            source_names = [src.title for src in p.sources[:2]]
            articles.append({
                "title": f"论证模式：{p.pattern_type or '未分类'}",
                "content": p.content,
                "evidence": f"置信度 {p.confidence:.0%} · 来源：{', '.join(source_names) if source_names else '未知'}",
                "tags": ["论证"] + p.tags[:2],
                "item_id": p.id,
                "dimension": "argument",
            })
        for v in profile.active_values():
            source_names = [src.title for src in v.sources[:2]]
            ranking_str = " > ".join(v.ranking) if v.ranking else "未排序"
            content = f"{v.content}\n\n优先级：{ranking_str}"
            if v.tradeoff_notes:
                content += f"\n\n权衡：{v.tradeoff_notes}"
            articles.append({
                "title": f"价值观：{v.domain or '未分类'}",
                "content": content,
                "evidence": f"置信度 {v.confidence:.0%} · 来源：{', '.join(source_names) if source_names else '未知'}",
                "tags": ["价值观"] + v.tags[:2],
                "item_id": v.id,
                "dimension": "value",
            })
        for f in profile.active_frameworks():
            source_names = [src.title for src in f.sources[:2]]
            steps_str = " → ".join(f.steps) if f.steps else "未整理"
            articles.append({
                "title": f"思维框架：{f.domain or '未分类'}",
                "content": f"{f.content}\n\n分析路径：{steps_str}",
                "evidence": f"置信度 {f.confidence:.0%} · 来源：{', '.join(source_names) if source_names else '未知'}",
                "tags": ["思维框架"] + f.tags[:2],
                "item_id": f.id,
                "dimension": "thinking",
            })

    if articles:
        status = "ready"
        dims: dict[str, int] = {}
        for a in articles:
            d = a.get("dimension", "other")
            dims[d] = dims.get(d, 0) + 1
        dim_labels = {"stance": "立场", "argument": "论证", "value": "价值观", "thinking": "思维框架"}
        dim_str = "、".join(f"{dim_labels.get(k, k)}{v}条" for k, v in dims.items())
        headline = f"已理解 {len(articles)} 条认知特征（{dim_str}），置信度待后续阅读和校准提高"
    elif profile and profile.open_gaps():
        status = "learning"
        headline = f"有 {len(profile.open_gaps())} 个认知盲区待澄清，建议通过深度访谈补充"
    else:
        status = "empty"
        headline = "还没有认知画像。触发冷启动或让它多读你的作品，它会逐步理解你的思想、立场和思维方式。"

    return {
        "id": "cognitive",
        "title": "它理解的我",
        "icon": "🧠",
        "status": status,
        "headline": headline,
        "articles": articles,
        "updated_at": _fmt_date(profile.last_updated if profile else None),
    }


# ---------------------------------------------------------------------------
# 聚合入口
# ---------------------------------------------------------------------------
def build_portrait() -> dict[str, Any]:
    """构建「它眼中的我」完整画像（8 个栏目）。"""
    sections = [
        _writing_style_section(),
        _decision_preference_section(),
        _behavior_patterns_section(),
        _reminder_preferences_section(),
        _dream_activity_section(),
        _privacy_cross_project_section(),
        _auto_tasks_section(),
        _cognitive_section(),
    ]

    # 按学习深度排序：ready > learning > empty，同状态按文章数降序
    status_order = {"ready": 0, "learning": 1, "empty": 2}
    sections.sort(key=lambda s: (status_order.get(s["status"], 3), -len(s.get("articles", []))))

    ready_count = sum(1 for s in sections if s["status"] == "ready")
    learning_count = sum(1 for s in sections if s["status"] == "learning")
    total_articles = sum(len(s.get("articles", [])) for s in sections)

    return {
        "sections": sections,
        "summary": {
            "ready_count": ready_count,
            "learning_count": learning_count,
            "empty_count": len(sections) - ready_count - learning_count,
            "total_articles": total_articles,
        },
        "generated_at": datetime.now().isoformat(),
    }
