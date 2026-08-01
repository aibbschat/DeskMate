"""深度审阅报告引擎（编辑专家交付物）。

与 writing.py 的「逐行校对(proofread)」互补：
  - proofread 是字句层面（冗余/节奏/用词/连贯/错字/考据）的编辑建议；
  - 本报告是文学性层面（人物/情节/文笔/设定/节奏）的「逐章细抠 + 总报告」。

产物为结构化 dict（前端 React 直接渲染，无需 HTML 注入，规避 XSS）；
同时可渲染为 Word(.docx) 供下载。所有 LLM 调用复用 deskmate.memory.llm.chat，
跟随 OpenWorker 主 provider 配置，无密钥/异常时向上抛错由端点 fail-safe 处理。
"""

from __future__ import annotations

import datetime
import io
import json
import logging
import re
from typing import Any, Optional

from deskmate.writing import (
    format_profile_for_prompt,
    split_into_chapters,
)
from deskmate.memory.llm import chat, llm_available

# 单章字数上限，超出截断避免超出模型上下文。
_CHAPTER_MAX_CHARS = 20000
# 报告最多逐章细抠的章数（超出则只做总报告，并在总报告注明「部分章节未逐章评析」）。
_MAX_DETAILED_CHAPTERS = 80
# 单章评析失败后的最大重试次数。
_MAX_RETRIES = 1


# --------------------------------------------------------------------------- #
# JSON 抽取（鲁棒）— 统一使用 deskmate.json_utils
# --------------------------------------------------------------------------- #
from deskmate.json_utils import extract_json as _extract_json, call_json as _call_json

def _as_str(v: Any, default: str = "") -> str:
    return str(v).strip() if v is not None else default


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else []


def format_bible_for_prompt(bible: Optional[dict]) -> str:
    """把 analyze_novel 的「圣经」（CHARACTERS/OUTLINE/TIMELINE/NOTES）压成 prompt 文本。

    顺序固定：人物关系 → 情节主线 → 时间线 → 设定/伏笔。返回 "" 表示无圣经。
    """
    if not bible:
        return ""
    order = [
        ("CHARACTERS", "人物关系圣经（一致性准绳，最核心）"),
        ("OUTLINE", "情节主线与弧光"),
        ("TIMELINE", "关键事件时间线"),
        ("NOTES", "世界观 / 势力 / 伏笔 / 待确认"),
    ]
    parts: list[str] = []
    for tag, label in order:
        content = bible.get(tag)
        if content and content.strip():
            parts.append(f"### {label}\n{content.strip()}")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# 轻量逐章评析（Phase 1：用于批量逐章，prompt 精简、无 bible/profile 注入）
# --------------------------------------------------------------------------- #
_CHAPTER_LIGHT_SYSTEM = (
    "你是资深文学编辑。请对以下小说章节做简要评析，提取内容摘要和关键问题。"
    "评析维度：人物、情节、文笔、节奏。注意：本阶段未注入全书设定圣经，"
    "因此不要对设定一致性做判断（如官职称谓、历史典制等），留到深度评析阶段。"
    "只输出 JSON，不要解释。"
)

_CHAPTER_LIGHT_USER = (
    "【<<<TITLE>>>】\n"
    "正文：\n<<<TEXT>>>\n\n"
    "请输出 JSON：\n"
    "{\n"
    '  "summary": "本章内容概括（2-3句，点明功能定位：铺垫/转折/高潮/收尾）",\n'
    '  "analysis": "本章简要评析（100-200字，聚焦人物、情节、文笔、节奏中最突出的1-2点）",\n'
    '  "issues": [\n'
    "    {\n"
    '      "type": "人物|情节|文笔|节奏|其他",\n'
    '      "excerpt": "原文短片段（不超过40字，原文照抄）",\n'
    '      "comment": "针对该处的评析与可操作建议"\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "issues 控制在 2-4 条最关键的，不要凑数。"
)


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """按句子边界截断，保证不超过 max_chars，且不从句子中间切断。"""
    if len(text) <= max_chars:
        return text
    cut = max_chars
    for sep in ("。", "！", "？", "\n"):
        pos = text.rfind(sep, 0, max_chars)
        if pos > max_chars * 0.8:  # 不低于上限的80%，避免切出太短的块
            cut = pos + 1
            break
    return text[:cut]


def _review_chapter_light(chapter: dict) -> dict:
    """轻量单章评析：无 bible/profile 注入，prompt 更短，max_tokens 更小。"""
    title = chapter["title"]
    text = chapter["text"]
    text = _truncate_at_sentence(text, _CHAPTER_MAX_CHARS)
    data = _call_json(
        _CHAPTER_LIGHT_SYSTEM,
        _CHAPTER_LIGHT_USER
        .replace("<<<TITLE>>>", title)
        .replace("<<<TEXT>>>", text),
        temperature=0.4,
        max_tokens=2500,
    )
    if not isinstance(data, dict):
        data = {}
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
    return {
        "idx": chapter["idx"],
        "title": title,
        "summary": _as_str(data.get("summary")),
        "analysis": _as_str(data.get("analysis")),
        "issues": issues,
    }


# --------------------------------------------------------------------------- #
# 深度逐章评析（Phase 2：用户指定单章深度细抠，完整 bible/profile 注入）
# --------------------------------------------------------------------------- #
_CHAPTER_DEEP_SYSTEM = (
    "你是资深文学编辑与小说评阅人，尤其熟悉历史小说的笔法与考据要求。"
    "请对单章小说做深度文学评析，聚焦文学性而非字句校对。"
    "评析须具体、可读，像写给作者看的章节审阅笔记。只输出 JSON，不要解释。"
    "若给出『全书设定圣经』（来自 analyze_novel），请以之为人物、时间线、设定一致性的"
    "权威准绳：发现本章出现与圣经矛盾的称谓/关系/时间/设定时，须在 issues 中标注 "
    "type=设定 并写明『与圣经不符：…』，不要替作者武断判定正误。"
)

_CHAPTER_DEEP_USER = (
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
    '      "comment": "针对该处的评析与可操作建议（考据硬伤须写明『存疑，请核对：…』，不武断断言；与圣经矛盾须写明『与圣经不符：…』）"\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "issues 控制在 3-6 条最关键的，不要凑数。"
)


def _review_chapter_deep(chapter: dict, profile_text: str, bible_text: str) -> dict:
    """深度单章评析：完整 bible + profile 注入，300-500字深度分析。"""
    title = chapter["title"]
    text = chapter["text"]
    text = _truncate_at_sentence(text, _CHAPTER_MAX_CHARS)
    data = _call_json(
        _CHAPTER_DEEP_SYSTEM,
        _CHAPTER_DEEP_USER
        .replace("<<<PROFILE>>>", profile_text)
        .replace("<<<BIBLE>>>", bible_text or "（本书未生成 analyze_novel 圣经，跳过设定一致性核对）")
        .replace("<<<TITLE>>>", title)
        .replace("<<<TEXT>>>", text),
        temperature=0.4,
        max_tokens=4000,
    )
    if not isinstance(data, dict):
        data = {}
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
    return {
        "idx": chapter["idx"],
        "title": title,
        "summary": _as_str(data.get("summary")),
        "analysis": _as_str(data.get("analysis")),
        "issues": issues,
    }


# --------------------------------------------------------------------------- #
# 总报告
# --------------------------------------------------------------------------- #
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
    '  "character_consistency": ["人物名：一致性观察（性格/动机/称呼是否前后统一，是否偏离圣经）", "..."],\n'
    '  "recommendations": ["具体修改建议1", "具体修改建议2", "具体修改建议3"]\n'
    "}\n"
    "scores 为 0-10 的整数或半整数（如 8.5）。"
)


def _build_chapters_brief(per_chapter: list[dict]) -> str:
    lines: list[str] = []
    for c in per_chapter:
        lines.append(f"■ {c['title']}")
        if c.get("summary"):
            lines.append(f"  概括：{c['summary']}")
        for it in c.get("issues", [])[:3]:
            lines.append(f"  · [{it.get('type','')}] {it.get('excerpt','')} —— {it.get('comment','')}")
    return "\n".join(lines)


def _review_overall(per_chapter: list[dict], profile_text: str, bible_text: str) -> dict:
    brief = _build_chapters_brief(per_chapter)
    data = _call_json(
        _OVERALL_SYSTEM,
        _OVERALL_USER
        .replace("<<<PROFILE>>>", profile_text)
        .replace("<<<BIBLE>>>", bible_text or "（本书未生成 analyze_novel 圣经）")
        .replace("<<<CHAPTERS>>>", brief),
        temperature=0.4,
        max_tokens=4000,
    )
    if not isinstance(data, dict):
        data = {}
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


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def generate_review_report(text: str, profile: Optional[dict] = None,
                           name: str = "书稿", bible: Optional[dict] = None,
                           chapter_indices: Optional[list] = None,
                           progress_callback: Optional[callable] = None) -> dict:
    """对整本小说生成「逐章细抠 + 总报告」。

    Phase 1（根因修复）：
    - 批量逐章调用改为轻量模式（_review_chapter_light），不注入 bible/profile，
      max_tokens 从 4000 降至 2500，单章 prompt 缩短 ~40%。
    - bible 和 profile 只在总报告阶段注入一次。
    - 单章失败支持重试（_MAX_RETRIES=1），提升鲁棒性。

    Phase 2（后续迭代）：
    - 用户可指定单章深度细抠（_review_chapter_deep），此时才注入完整 bible/profile，
      生成 300-500 字深度分析。

    Args:
        text: 整本小说纯文本（含 第X章/回/节 标题，自动切分）。
        profile: 可选作者风格画像（来自 style_profile.json）。
        name: 书名，仅用于报告标题展示。
        bible: 可选 analyze_novel 圣经 {CHARACTERS,OUTLINE,TIMELINE,NOTES}（markdown 字符串），
               作为人物/时间线/设定一致性准绳注入逐章与总报告。
        progress_callback: 可选进度回调，签名 fn(event: str, data: dict) -> None。
            事件类型：started, chapter_done, overall_started, completed, failed
    Returns:
        {
          "title": "《书名》审阅报告",
          "generated_at": "...",
          "overall": {...},
          "per_chapter": [...],
          "meta": {"chapter_count": n, "detailed": bool, "truncated": bool, "bible": [...]}
        }
    """
    if not llm_available():
        raise RuntimeError("LLM 未就绪（未配置可用 provider），无法生成审阅报告")

    def _emit(event: str, **kwargs):
        if progress_callback is not None:
            try:
                progress_callback(event, kwargs)
            except Exception:
                pass

    chapters = split_into_chapters(text)
    if not chapters:
        raise ValueError("文稿为空或无法解析")

    profile_text = format_profile_for_prompt(profile)
    bible_text = format_bible_for_prompt(bible)

    # 选章模式：仅审阅用户勾选的章节（前端已自动分章并收集 idx）。此时不视为截断。
    if chapter_indices:
        wanted = set(int(x) for x in chapter_indices)
        detailed = [c for c in chapters if c["idx"] in wanted]
        if not detailed:
            raise ValueError("所选章节均不存在，请重新选择章节")
        truncated = False
    else:
        detailed = chapters[:_MAX_DETAILED_CHAPTERS]
        truncated = len(chapters) > _MAX_DETAILED_CHAPTERS

    _emit("started", total=len(detailed), chapter_count=len(chapters))

    per_chapter: list[dict] = []
    failed = 0
    for i, ch in enumerate(detailed):
        result = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                # Phase 1: 轻量逐章，不注入 bible/profile
                result = _review_chapter_light(ch)
                break
            except Exception as e:
                if attempt < _MAX_RETRIES:
                    logging.warning("章节 %s 评析失败，第 %d 次重试: %s", ch.get("title"), attempt + 1, e)
                else:
                    logging.warning("章节 %s 评析失败，已达最大重试次数: %s", ch.get("title"), e)
        if result is not None:
            per_chapter.append(result)
            _emit("chapter_done", idx=i+1, total=len(detailed),
                  title=ch["title"], issue_count=len(result.get("issues", [])),
                  failed=False)
        else:
            # 单章模型返回无法解析（如 deepseek 偶发 JSON 瑕疵）不应拖垮整本报告：
            # 降级为该章「评析失败」占位，总报告仍基于其余成功章节生成。
            failed += 1
            per_chapter.append({
                "idx": ch["idx"],
                "title": ch["title"],
                "summary": "",
                "analysis": "（本章评析失败：模型返回无法解析，已跳过。可稍后重试或单独送审本章。）",
                "issues": [],
            })
            _emit("chapter_done", idx=i+1, total=len(detailed),
                  title=ch["title"], issue_count=0, failed=True)

    _emit("overall_started", total=len(detailed), failed=failed)

    overall = _review_overall(per_chapter, profile_text, bible_text)
    if failed:
        note = f"（有 {failed} 章评析因模型返回异常被跳过，其余 {len(per_chapter) - failed} 章与总报告正常生成。）"
        overall["summary"] = note + "\n" + (overall.get("summary") or "")
    if truncated:
        overall["summary"] = (
            f"（本书共 {len(chapters)} 章，受限于逐章评析上限，已对前 {len(detailed)} 章做逐章细抠；"
            f"以下总评为基于已评析章节的综合判断。）\n" + overall.get("summary", "")
        )

    report = {
        "title": f"《{name}》审阅报告",
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "overall": overall,
        "per_chapter": per_chapter,
        "meta": {
            "chapter_count": len(chapters),
            "detailed_count": len(per_chapter),
            "truncated": truncated,
            "bible": sorted(bible.keys()) if bible else [],
        },
    }
    _emit("completed", report=report)
    return report


def available() -> bool:
    return llm_available()


# --------------------------------------------------------------------------- #
# Word(.docx) 渲染
# --------------------------------------------------------------------------- #
def render_report_docx(report: dict) -> bytes:
    """把结构化报告渲染为 Word 文档字节流。"""
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor, Inches
        from docx.enum.text import WD_ALIGN_PARAGRAPH
    except ImportError as exc:  # pragma: no cover
        raise ValueError("服务端未安装 python-docx，无法生成 Word") from exc

    doc = Document()

    # 标题
    h = doc.add_heading(report.get("title", "审阅报告"), level=0)
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    gen = report.get("generated_at", "")
    if gen:
        sub = doc.add_paragraph(f"生成时间：{gen}")
        sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in sub.runs:
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)

    overall = report.get("overall", {})

    # —— 总报告 ——
    doc.add_heading("一、总体评价", level=1)
    if overall.get("summary"):
        doc.add_paragraph(overall["summary"])

    scores = overall.get("scores", {})
    if scores:
        doc.add_heading("评分（满分 10）", level=2)
        table = doc.add_table(rows=1, cols=2)
        table.style = "Light Grid Accent 1"
        hdr = table.rows[0].cells
        hdr[0].text = "维度"
        hdr[1].text = "得分"
        for dim, sc in scores.items():
            row = table.add_row().cells
            row[0].text = dim
            row[1].text = str(sc)

    def _bullets(title: str, items: list):
        if not items:
            return
        doc.add_heading(title, level=2)
        for it in items:
            doc.add_paragraph(_as_str(it), style="List Bullet")

    _bullets("亮点", overall.get("strengths", []))
    _bullets("不足", overall.get("weaknesses", []))
    _bullets("人物一致性观察", overall.get("character_consistency", []))
    _bullets("修改建议", overall.get("recommendations", []))

    # —— 逐章细抠 ——
    doc.add_heading("二、逐章细抠", level=1)
    for c in report.get("per_chapter", []):
        doc.add_heading(c.get("title", "章节"), level=2)
        if c.get("summary"):
            doc.add_paragraph(c["summary"])
        if c.get("analysis"):
            p = doc.add_paragraph()
            p.add_run("评析：").bold = True
            p.add_run(c["analysis"])
        issues = c.get("issues", [])
        if issues:
            doc.add_heading("本章重点问题", level=3)
            itable = doc.add_table(rows=1, cols=3)
            itable.style = "Light Grid Accent 1"
            ih = itable.rows[0].cells
            ih[0].text = "类型"
            ih[1].text = "原文"
            ih[2].text = "评析"
            for it in issues:
                r = itable.add_row().cells
                r[0].text = _as_str(it.get("type"), "其他")
                r[1].text = _as_str(it.get("excerpt"))
                r[2].text = _as_str(it.get("comment"))

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
