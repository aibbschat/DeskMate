"""编辑专家工具 — review_chapter / generate_report。

供 editor agent 通过原生 Agent Loop 调用，替代硬编码的 generate_review_report 同步函数。
每个工具内部调 LLM，返回结构化 JSON，Agent 可以在聊天中实时展示结果。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import aisuite as ai

from deskmate.memory.llm import chat, llm_available
from deskmate.report import (
    _extract_json,
    _as_str,
    _as_list,
    format_bible_for_prompt,
    _CHAPTER_MAX_CHARS,
)
from deskmate.writing import format_profile_for_prompt, load_manuscript


# ---------------------------------------------------------------------------
# review_chapter — 评析单章
# ---------------------------------------------------------------------------

_REVIEW_CHAPTER_SYSTEM = (
    "你是资深文学编辑与小说评阅人，尤其熟悉历史小说的笔法与考据要求。"
    "请对单章小说做深度文学评析，聚焦文学性而非字句校对。"
    "评析须具体、可读，像写给作者看的章节审阅笔记。只输出 JSON，不要解释。"
)

_REVIEW_CHAPTER_USER = (
    "【作者风格画像】\n<<<PROFILE>>>\n\n"
    "【全书设定圣经（来自 analyze_novel，作为一致性准绳；若内容为空表示本书未生成圣经，跳过即可）】\n"
    "<<<BIBLE>>>\n\n"
    "【<<<TITLE>>>】\n"
    "正文：\n<<<TEXT>>>\n\n"
    "请输出 JSON：\n"
    "{\n"
    '  "summary": "本章内容概括（2-4句，并点明它在全书中的功能定位，如铺垫/转折/高潮）",\n'
    '  "analysis": "本章深度评析（300-500字，聚焦：人物塑造是否立体、情节推进是否自然、文笔亮点与不足、节奏张弛）",\n'
    '  "issues": [\n'
    "    {\n"
    '      "type": "人物|情节|文笔|节奏|设定|其他",\n'
    '      "excerpt": "原文相关短片段（尽量不超过40字，原文照抄）",\n'
    '      "comment": "针对该处的评析与可操作建议"\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "issues 控制在 3-6 条最关键的，不要凑数。"
)


# 单章送审的正文上限：复用 report.py 的 _CHAPTER_MAX_CHARS（20000），
# 保证 1.8 万字以内的章节完整送审，不被静默截断。
MAX_CHAPTER_CHARS = _CHAPTER_MAX_CHARS


def review_chapter(
    chapter_title: str,
    chapter_text: str,
    style_profile: str = "",
    bible: str = "",
) -> dict:
    """评析单章小说，返回结构化 JSON（summary / analysis / issues）。

    Args:
        chapter_title: 章节标题（如"第一章：碧蹄馆"）
        chapter_text: 章节正文
        style_profile: 作者风格画像文本（可选，来自 style_profile.json）
        bible: 全书设定圣经文本（可选，来自 analyze_novel）
    """
    if not llm_available():
        return {"error": "LLM 未就绪（未配置可用 provider）"}

    text = chapter_text
    if len(text) > MAX_CHAPTER_CHARS:
        text = text[:MAX_CHAPTER_CHARS] + "\n…（本章较长，已截断至前段供评析）"

    user_prompt = (
        _REVIEW_CHAPTER_USER
        .replace("<<<PROFILE>>>", style_profile or "（无风格画像）")
        .replace("<<<BIBLE>>>", bible or "（本书未生成 analyze_novel 圣经，跳过设定一致性核对）")
        .replace("<<<TITLE>>>", chapter_title)
        .replace("<<<TEXT>>>", text)
    )

    try:
        raw = chat(_REVIEW_CHAPTER_SYSTEM, user_prompt, temperature=0.4, max_tokens=4000)
        data = _extract_json(raw)
        if not isinstance(data, dict):
            data = {}
    except Exception as e:
        logging.warning("review_chapter failed for %s: %s", chapter_title, e)
        return {"error": f"评析失败：{e}"}

    # 规范化 issues
    issues = []
    for it in _as_list(data.get("issues")):
        if not isinstance(it, dict):
            continue
        excerpt = _as_str(it.get("excerpt"))
        if not excerpt:
            continue
        issues.append({
            "type": _as_str(it.get("type"), "其他"),
            "excerpt": excerpt,
            "comment": _as_str(it.get("comment")),
        })

    # P2-10：单章审稿完成 → 行为日志（供「常审哪些书/问题类型」规律积累）
    try:
        from deskmate.proactivity.behavior_patterns import log_event
        log_event("review_done", {"title": chapter_title, "issues": len(issues)})
    except Exception:
        pass

    return {
        "title": chapter_title,
        "summary": _as_str(data.get("summary")),
        "analysis": _as_str(data.get("analysis")),
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# generate_report — 生成总报告
# ---------------------------------------------------------------------------

_OVERALL_SYSTEM = (
    "你是资深文学编辑与出版策划。基于一份长篇小说的各章摘要与重点问题，"
    "撰写整书审阅总报告，要求客观、专业、有建设性，先肯定优点再指出不足。"
    "若给出『全书设定圣经』（来自 analyze_novel），请在 character_consistency 与 recommendations "
    "中结合圣经给出更有针对性的全书一致性判断（如某人物关系与圣经冲突、时间线错位等）。"
    "只输出 JSON，不要解释。"
)

_OVERALL_USER = (
    "【作者风格画像】\n<<<PROFILE>>>\n\n"
    "【全书设定圣经（来自 analyze_novel，作为一致性准绳；若内容为空表示本书未生成圣经）】\n"
    "<<<BIBLE>>>\n\n"
    "【各章摘要与重点问题】\n<<<CHAPTERS>>>\n\n"
    "请输出 JSON：\n"
    "{\n"
    '  "summary": "整书总评（400-600字，先肯定主要优点，再指出最该改进的 2-4 个方向）",\n'
    '  "scores": {"人物": 0-10, "情节": 0-10, "文笔": 0-10, "设定": 0-10, "节奏": 0-10},\n'
    '  "strengths": ["优点1", "优点2", "优点3"],\n'
    '  "weaknesses": ["不足1", "不足2", "不足3"],\n'
    '  "character_consistency": ["人物名：一致性观察", "..."],\n'
    '  "recommendations": ["具体修改建议1", "具体修改建议2", "具体修改建议3"]\n'
    "}\n"
    "scores 为 0-10 的整数或半整数（如 8.5）。"
)


def _build_chapters_brief(chapter_reviews: list[dict]) -> str:
    lines: list[str] = []
    for c in chapter_reviews:
        lines.append(f"■ {c.get('title', '?')}")
        if c.get("summary"):
            lines.append(f"  概括：{c['summary']}")
        for it in c.get("issues", [])[:3]:
            lines.append(f"  · [{it.get('type', '')}] {it.get('excerpt', '')} —— {it.get('comment', '')}")
    return "\n".join(lines)


def generate_report(
    chapter_reviews: list[dict],
    style_profile: str = "",
    bible: str = "",
) -> dict:
    """基于逐章评析结果生成整书审阅总报告。

    Args:
        chapter_reviews: review_chapter 返回的 JSON 列表
        style_profile: 作者风格画像文本（可选）
        bible: 全书设定圣经文本（可选）
    """
    if not llm_available():
        return {"error": "LLM 未就绪（未配置可用 provider）"}
    if not chapter_reviews:
        return {"error": "无逐章评析数据，无法生成总报告"}

    brief = _build_chapters_brief(chapter_reviews)
    user_prompt = (
        _OVERALL_USER
        .replace("<<<PROFILE>>>", style_profile or "（无风格画像）")
        .replace("<<<BIBLE>>>", bible or "（本书未生成 analyze_novel 圣经）")
        .replace("<<<CHAPTERS>>>", brief)
    )

    try:
        raw = chat(_OVERALL_SYSTEM, user_prompt, temperature=0.4, max_tokens=4000)
        data = _extract_json(raw)
        if not isinstance(data, dict):
            data = {}
    except Exception as e:
        logging.warning("generate_report failed: %s", e)
        return {"error": f"生成总报告失败：{e}"}

    scores = data.get("scores")
    if not isinstance(scores, dict):
        scores = {}
    norm_scores: dict[str, float] = {}
    for k in ("人物", "情节", "文笔", "设定", "节奏"):
        v = scores.get(k)
        try:
            norm_scores[k] = round(float(v), 1)
        except (TypeError, ValueError):
            norm_scores[k] = 0.0

    return {
        "summary": _as_str(data.get("summary")),
        "scores": norm_scores,
        "strengths": [_as_str(s) for s in _as_list(data.get("strengths")) if _as_str(s)],
        "weaknesses": [_as_str(s) for s in _as_list(data.get("weaknesses")) if _as_str(s)],
        "character_consistency": [_as_str(s) for s in _as_list(data.get("character_consistency")) if _as_str(s)],
        "recommendations": [_as_str(s) for s in _as_list(data.get("recommendations")) if _as_str(s)],
    }


# ---------------------------------------------------------------------------
# load_manuscript_file — 读取书稿并自动分章
# ---------------------------------------------------------------------------

def load_manuscript_file(path: str) -> dict:
    """读取本地文件或文件夹，自动分章，返回章节列表。

    Args:
        path: 本地 .txt/.docx 文件路径，或小说文件夹路径
    Returns:
        {"chapters": [{"idx": 1, "title": "...", "text": "..."}, ...], "bible": "..."}
    """
    from pathlib import Path as _P
    pp = _P(path).expanduser()

    if pp.is_dir():
        from deskmate.novel_memory.core import read_bible, scan_novel
        try:
            raw_text, _ = scan_novel(str(pp))
        except FileNotFoundError as e:
            return {"error": str(e)}
        if not raw_text.strip():
            return {"error": f"该文件夹未找到可读的小说文本：{path}"}
        bible_text = ""
        try:
            bible = read_bible(str(pp))
            bible_text = format_bible_for_prompt(bible)
        except Exception:
            pass
    elif pp.is_file():
        from deskmate.writing import read_text_from_path
        try:
            raw_text = read_text_from_path(str(pp))
        except (FileNotFoundError, ValueError) as e:
            return {"error": str(e)}
        bible_text = ""
        try:
            from deskmate.novel_memory.core import read_bible
            bible = read_bible(str(pp.parent))
            bible_text = format_bible_for_prompt(bible)
        except Exception:
            pass
    else:
        # 兼容「已存书稿」：path 实为书名时，按书名调出已存书稿。
        # 已存书稿存于 ~/.deskmate/manuscripts/，不在 Agent 的工作区，故不能走文件/目录分支。
        name = path.strip().strip("《》").strip()
        chapters = load_manuscript(name) if name else None
        if not chapters:
            return {"error": f"路径不存在：{path}"}
        # 已存书稿同样喂书稿桶（写作侧文风指纹只认书稿，零 LLM、去重、fail-open）
        try:
            from deskmate.style_profile_store import update_style_profile

            joined = "\n".join(str(c.get("text", "")) for c in chapters)
            update_style_profile(joined, source=f"书稿-{name}", bucket="manuscript")
        except Exception:
            pass
        return {
            "chapters": [
                {"idx": c["idx"], "title": c["title"], "text": c["text"]} for c in chapters
            ],
            "bible": "",
            "char_count": sum(len(c.get("text", "")) for c in chapters),
        }

    from deskmate.writing import split_into_chapters

    # 书稿样本：读他自己的稿子时，顺手把真实书稿文风喂进「书稿桶」——
    # 写作/审稿的文风指纹只认书稿桶（聊天语感不参与）。零 LLM、按内容去重、fail-open。
    try:
        from deskmate.style_profile_store import update_style_profile

        update_style_profile(raw_text, source=f"书稿-{pp.name}", bucket="manuscript")
    except Exception:
        pass

    chapters = split_into_chapters(raw_text)
    if not chapters:
        return {"error": "未能从文稿中识别章节（需含「第X章/回/节」等标题）"}

    return {
        "chapters": [{"idx": c["idx"], "title": c["title"], "text": c["text"]} for c in chapters],
        "bible": bible_text,
        "char_count": len(raw_text),
    }
