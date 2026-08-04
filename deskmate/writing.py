"""行文编辑（润色台）后端逻辑。

提供两个能力：
  1. extract_style_profile(sample) —— 从作者代表作样本抽风格特征，供日后「按作者声口」润色。
  2. proofread(draft, profile)      —— 对文稿做专业编辑审阅，返回 {letter, items}。

LLM 统一复用 deskmate.memory.llm.chat（跟随 OpenWorker 主 provider 配置，
即 secrets.json 里的 provider:qwen，中国站 dashscope 端点）。所有调用在
「无密钥 / 异常」时向上抛错，由 app.py 端点 fail-safe 处理，绝不阻断主对话。

设计铁律（契合用户「史料存疑须明示」要求）：历史考据类建议一律标
「存疑，请核对：…」，不替作者武断断言。
"""

from __future__ import annotations

import base64
import datetime
import io
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from deskmate.memory.llm import chat, llm_available

# 单次编辑 pass 处理的文稿上限（字符）。超过则分块，每块带少量重叠。
_CHUNK_CHARS = 5000
_CHUNK_OVERLAP = 400

# 编辑审阅的六个维度（顺序即展示优先级）。
_DIMENSIONS = ["冗余", "节奏", "用词", "连贯", "错字", "考据"]


# --------------------------------------------------------------------------- #
# 文件读取
# --------------------------------------------------------------------------- #
def _docx_to_text(doc) -> str:
    """从 python-docx Document 对象抽取纯文本（段落 + 表格）。"""
    parts: list[str] = []
    for para in doc.paragraphs:
        if para.text:
            parts.append(para.text)
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text for c in row.cells if c.text]
            if cells:
                parts.append("｜".join(cells))
    return "\n".join(parts)


def _decode_bytes(filename: str, data: bytes, *, max_chars: int) -> str:
    """从文件名 + 字节内容解析文本（.txt / .docx）。"""
    suffix = Path(filename).suffix.lower()
    if suffix == ".txt":
        text = data.decode("utf-8", errors="ignore")
    elif suffix == ".docx":
        try:
            from docx import Document
        except ImportError as exc:  # pragma: no cover
            raise ValueError("服务端未安装 python-docx，无法解析 .docx") from exc
        text = _docx_to_text(Document(io.BytesIO(data)))
    else:
        raise ValueError(f"不支持的文件类型：{suffix}（仅支持 .txt / .docx）")
    text = text.strip()
    if not text:
        raise ValueError("文件内容为空")
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


def read_text_from_path(path: str, *, max_chars: int = 200_000) -> str:
    """读取本地 .txt / .docx 文件内容为纯文本。

    Raises:
        FileNotFoundError: 路径不存在。
        ValueError: 不支持的扩展名或解析失败。
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    suffix = p.suffix.lower()
    if suffix == ".txt":
        text = p.read_text(encoding="utf-8", errors="ignore")
    elif suffix == ".docx":
        try:
            from docx import Document
        except ImportError as exc:  # pragma: no cover
            raise ValueError("服务端未安装 python-docx，无法解析 .docx") from exc
        text = _docx_to_text(Document(str(p)))
    else:
        raise ValueError(f"不支持的文件类型：{suffix}（仅支持 .txt / .docx）")
    text = text.strip()
    if not text:
        raise ValueError("文件内容为空")
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


# --------------------------------------------------------------------------- #
# 风格画像自动加载
# --------------------------------------------------------------------------- #
_DEFAULT_STYLE_PROFILE_PATHS = [
    "style_profile.json",
    ".style_profile.json",
    ".deskmate/style_profile.json",
]


def _auto_load_style_profile(workspace: str | None = None) -> str:
    """从工作区目录自动加载风格画像。如果 workspace 未指定，检查当前目录和 ~/.deskmate/。

    Returns:
        格式化的风格画像文本（用于注入 prompt），或空串（无画像）。
    """
    if workspace:
        candidates = [Path(workspace) / p for p in _DEFAULT_STYLE_PROFILE_PATHS]
    else:
        candidates = [Path(p) for p in _DEFAULT_STYLE_PROFILE_PATHS]
        candidates.append(Path.home() / ".deskmate" / "style_profile.json")
    for fp in candidates:
        fp = fp.expanduser().resolve()
        if fp.is_file():
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                return format_profile_for_prompt(data)
            except (OSError, json.JSONDecodeError):
                continue
    return ""


def read_text_from_base64(filename: str, content_base64: str, *, max_chars: int = 200_000) -> str:
    """从前端上传的（base64）文件内容解析文本，用于无 Tauri dialog 时的 .docx 上传。"""
    try:
        data = base64.b64decode(content_base64, validate=True)
    except Exception as exc:
        raise ValueError("base64 解码失败") from exc
    return _decode_bytes(filename, data, max_chars=max_chars)


# --------------------------------------------------------------------------- #
# JSON 抽取（鲁棒）— 统一使用 deskmate.json_utils
# --------------------------------------------------------------------------- #
from deskmate.json_utils import extract_json as _extract_json, call_json as _call_json


# --------------------------------------------------------------------------- #
# 风格画像
# --------------------------------------------------------------------------- #
_STYLE_SYSTEM = (
    "你是一位资深文学编辑与文体分析专家。用户会给你一段他本人的作品样本。"
    "请分析并提炼该作者的写作风格特征，用于日后在保留其个人声口的前提下给出润色建议。"
    "只输出 JSON，不要解释。"
)

_STYLE_USER = (
    "以下是作者的一段代表作样本：\n\n<<<SAMPLE>>>\n\n"
    "请输出 JSON，字段如下：\n"
    "{\n"
    '  "summary": "一句话风格画像（不超过40字）",\n'
    '  "features": {\n'
    '    "sentence_style": "句式习惯（长短、整散、是否爱用四字格/对仗）",\n'
    '    "register": "语体（如：半文半白 / 古雅白话 / 现代白话 / 明清拟古）",\n'
    '    "rhythm": "节奏与句式变化特点",\n'
    '    "word_preference": "用词偏好（典雅/口语/方言语气/史白用词等）",\n'
    '    "taboos": "明显应避免、或作者不喜欢的写法"\n'
    "  }\n"
    "}"
)


def extract_style_profile(sample: str) -> dict:
    """从样本文本抽风格特征，返回可序列化 dict。"""
    sample = sample.strip()
    if len(sample) > 12000:
        sample = sample[:12000]
    data = _call_json(
        _STYLE_SYSTEM,
        _STYLE_USER.replace("<<<SAMPLE>>>", sample),
        temperature=0.2,
        max_tokens=1200,
    )
    if not isinstance(data, dict):
        data = {"summary": "", "features": {}}
    features = data.get("features")
    if not isinstance(features, dict):
        features = {}
    return {
        "summary": str(data.get("summary", "")).strip(),
        "features": {k: str(v).strip() for k, v in features.items() if v},
    }


def format_profile_for_prompt(profile: Optional[dict]) -> str:
    """把风格画像压成给编辑 prompt 的约束文本。"""
    if not profile:
        return "（暂无风格画像，按通用优秀中文写作标准润色，但尽量尊重作者原声口）"
    lines = []
    if profile.get("summary"):
        lines.append(f"整体画像：{profile['summary']}")
    feats = profile.get("features") or {}
    label_map = {
        "sentence_style": "句式习惯",
        "register": "语体",
        "rhythm": "节奏",
        "word_preference": "用词偏好",
        "taboos": "应避免的写法",
    }
    for k, label in label_map.items():
        if feats.get(k):
            lines.append(f"{label}：{feats[k]}")
    return "\n".join(lines) if lines else "（风格画像为空，按通用标准润色）"


# --------------------------------------------------------------------------- #
# 编辑审阅
# --------------------------------------------------------------------------- #
_PROOFREAD_SYSTEM = (
    "你是这位作者的专属文字编辑，熟悉他的写作风格（见下方『作者风格画像』）。"
    "请对给定文稿片段做专业编辑审阅，严格按以下六个维度输出建议：\n"
    "1) 精简冗余：砍掉废话、重复、啰嗦的表达；\n"
    "2) 句式节奏：长短句搭配、避免一排雷同的句式；\n"
    "3) 用词精准：给出更准确/更贴切的词，务必保留作者声口；\n"
    "4) 连贯与视角：人称、时间线、逻辑是否跳脱；\n"
    "5) 错字病句：明显错别字、语法错误；\n"
    "6) 历史考据：若涉及年代/称谓/典制/地名有明显硬伤，必须在该条 reason 写明"
    "『存疑，请核对：…』，不得武断断言「这是错的」——作者可能另有考据。\n"
    "所有 suggestion 必须保留作者原有声口与语体，不要改成现代白话腔。"
    "只输出 JSON，不要解释。"
)

_PROOFREAD_USER = (
    "【作者风格画像】\n<<<PROFILE>>>\n\n"
    "【文稿片段】\n<<<CHUNK>>>\n\n"
    "请输出 JSON：\n"
    "{\n"
    '  "items": [\n'
    "    {\n"
    '      "id": "1",\n'
    '      "excerpt": "原文短片段（尽量不超过50字，原文照抄）",\n'
    '      "type": "冗余|节奏|用词|连贯|错字|考据",\n'
    '      "suggestion": "建议改法（可给整句替换，保留作者声口）",\n'
    '      "reason": "理由（考据类必须写『存疑，请核对：…』）"\n'
    "    }\n"
    "  ]\n"
    "}"
)

_LETTER_SYSTEM = (
    "你是作者的文字编辑。基于下面这份『重点问题清单』，写一段 200 字以内的整体编辑评语，"
    "先肯定作者的主要优点，再点出最该改进的 2-3 点（如冗余、节奏、考据等）。"
    "语气温和、专业，像真人编辑写给作者的信。只输出纯文本，不要 JSON、不要标题。"
)

_LETTER_USER = "【重点问题清单】\n<<<ITEMS>>>\n\n请写整体编辑评语。"


def _chunk_text(text: str) -> list[str]:
    """按句子边界切分，块大小约 _CHUNK_CHARS，带重叠。"""
    if len(text) <= _CHUNK_CHARS:
        return [text]
    sentences = re.split(r"(?<=[。！？!?\n])", text)
    chunks: list[str] = []
    cur = ""
    for s in sentences:
        if not s.strip():
            continue
        if len(cur) + len(s) > _CHUNK_CHARS and cur:
            chunks.append(cur)
            cur = cur[-_CHUNK_OVERLAP:] + s
        else:
            cur += s
    if cur.strip():
        chunks.append(cur)
    return chunks


def _proofread_chunk(chunk: str, profile_text: str) -> list[dict]:
    data = _call_json(
        _PROOFREAD_SYSTEM,
        _PROOFREAD_USER.replace("<<<PROFILE>>>", profile_text).replace("<<<CHUNK>>>", chunk),
        temperature=0.3,
        max_tokens=4000,
    )
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        excerpt = str(it.get("excerpt", "")).strip()
        if not excerpt:
            continue
        out.append({
            "excerpt": excerpt,
            "type": str(it.get("type", "其他")).strip(),
            "suggestion": str(it.get("suggestion", "")).strip(),
            "reason": str(it.get("reason", "")).strip(),
        })
    return out


def _dedupe_items(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        key = it["excerpt"][:40]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


# --------------------------------------------------------------------------- #
# 章节切分（单文件含多章场景）
# --------------------------------------------------------------------------- #
_CHAPTER_RE = re.compile(
    r"^\s*第\s*([0-9零一二三四五六七八九十百千〇]+)\s*(章|回|节)\b",
    re.MULTILINE,
)
_SPECIAL_RE = re.compile(
    r"^\s*(楔子|引子|前言|序言|尾声|后记|附录)\s*$",
    re.MULTILINE,
)


def split_into_chapters(text: str) -> list[dict]:
    """按『第X章 / 第X回 / 第X节』及特殊节（楔子/引子等）切分。

    返回 [{idx, title, text}]，text 为该章正文（不含标题行）。
    无标题时返回单章 {idx:1, title:"全文", text: 全文}。
    """
    text = text.strip()
    if not text:
        return []

    matches: list[tuple[int, str]] = []

    def _add_match(m: "re.Match") -> None:
        # m.group(0) 可能含前导空白/换行（^\s* 在 MULTILINE 下会吞掉行首\n），
        # 故用 lstrip 定位真正的「第/楔子」起始位置，避免标题被算空、章题行混入正文。
        marker = m.group(0).lstrip()
        mpos = text.find(marker, m.start())
        line_start = text.rfind("\n", 0, mpos) + 1
        line_end = text.find("\n", m.end())
        title = (text[line_start:line_end] if line_end != -1 else text[line_start:]).strip()
        matches.append((mpos, title))

    for m in _CHAPTER_RE.finditer(text):
        _add_match(m)
    for m in _SPECIAL_RE.finditer(text):
        _add_match(m)
    matches.sort(key=lambda x: x[0])

    if not matches:
        return [{"idx": 1, "title": "全文", "text": text}]

    chapters: list[dict] = []
    for i, (pos, title) in enumerate(matches):
        line_end = text.find("\n", pos)
        start = line_end + 1 if line_end != -1 else len(text)
        end = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if not body:
            continue
        chapters.append({"idx": len(chapters) + 1, "title": title, "text": body})
    if not chapters:
        return [{"idx": 1, "title": "全文", "text": text}]
    return chapters


def list_chapters(text: str) -> list[dict]:
    """返回章节元信息 [{idx, title, char_count}]（不含正文），供前端下拉。"""
    return [
        {"idx": c["idx"], "title": c["title"], "char_count": len(c["text"])}
        for c in split_into_chapters(text)
    ]


def _select_chapter(chapters: list[dict], chapter) -> Optional[dict]:
    if chapter is None:
        return None
    if isinstance(chapter, int):
        for c in chapters:
            if c["idx"] == chapter:
                return c
        return None
    ch = str(chapter).strip()
    for c in chapters:
        if c["title"] == ch:
            return c
    return None


def proofread_chapter(title: str, text: str, profile_text: str = "", workspace: str = "") -> dict:
    """审阅单章：分块调 LLM，返回 {title, items}。

    如果 profile_text 为空，自动从工作区目录加载风格画像。

    单块瞬时失败（如模型偶发返回空）跳过该块，不拖垮整章；LLM 整体不可用时抛错，
    交由上层（逐章流式路由）写失败提示并继续下一章。

    Args:
        title: 章节标题。
        text: 正文文本。
        profile_text: 可选风格画像文本。为空时自动从 workspace 加载。
        workspace: 工作区目录（用于自动加载风格画像）。
    """
    if not llm_available():
        raise RuntimeError("LLM 未就绪（未配置可用 provider）")
    # 自动加载风格画像
    if not profile_text and workspace:
        auto = _auto_load_style_profile(workspace)
        if auto:
            profile_text = auto
    # 如果仍然为空，使用默认提示
    if not profile_text:
        profile_text = "（暂无风格画像，按通用优秀中文写作标准润色，但尽量尊重作者原声口）"
    text = (text or "").strip()
    if not text:
        return {"title": title, "items": []}
    collected: list[dict] = []
    for ch in _chunk_text(text):
        try:
            for it in _proofread_chunk(ch, profile_text):
                collected.append(it)
        except Exception:
            _log_warning(f"单段编辑审阅失败，已跳过该段（{title}）")
    return {"title": title, "items": _dedupe_items(collected)}


def generate_letter(items: list[dict]) -> str:
    """基于重点问题清单生成 200 字以内的整体编辑评语（纯文本）。失败返回空。"""
    if not items:
        return ""
    try:
        items_json = json.dumps(
            [{"type": i.get("type"), "excerpt": i.get("excerpt"), "reason": i.get("reason")}
             for i in items],
            ensure_ascii=False,
            indent=1,
        )
        return chat(
            _LETTER_SYSTEM,
            _LETTER_USER.replace("<<<ITEMS>>>", items_json),
            temperature=0.4,
            max_tokens=600,
        ).strip()
    except Exception:
        _log_warning("生成编辑评语失败")
        return ""


def format_chapter_block(title: str, items: list[dict]) -> str:
    """单章的【原文】/【建议】/【理由】块，逐章流式时作为一条助手消息发进聊天。"""
    lines: list[str] = [f"## 📖 {title}（{len(items)} 处）", ""]
    if not items:
        lines.append("✅ 本章没有明显可改的地方。")
        return "\n".join(lines).rstrip() + "\n"
    for it in items:
        t = (it.get("type") or "").strip()
        bullet = f"• **【{t}】**" if t else "•"
        lines.append(bullet)
        lines.append(f"> 【原文】{it.get('excerpt', '')}")
        lines.append(f"> 【建议】{it.get('suggestion', '')}")
        reason = (it.get("reason") or "").strip()
        if reason:
            lines.append(f"> 【理由】{reason}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_report_card(
    label: str,
    per_chapter: list,
    total_items: int,
    letter: str,
) -> str:
    """结尾总览报告卡（聊天里的一条助手消息）：各章条数汇总 + 总数 + 编辑评语。

    per_chapter: [(章节标题, 该章条数), ...]
    """
    lines: list[str] = [f"📋 **文稿编校总览 · {label}**", ""]
    for ch_title, n in per_chapter:
        lines.append(f"· {ch_title}：{n} 处")
    lines.append(f"**共 {total_items} 处建议**")
    lines.append("")
    if letter:
        lines.append("**编辑评语**")
        lines.append(letter)
        lines.append("")
    if total_items == 0:
        lines.append("✅ 这篇文稿没有明显可改的地方，或建议都已处理。")
    return "\n".join(lines).rstrip() + "\n"


import logging

def _log_warning(msg: str) -> None:
    logging.warning(msg)


def available() -> bool:
    return llm_available()


# 方便前端展示的维度顺序
DIMENSIONS = _DIMENSIONS


# --------------------------------------------------------------------------- #
# 书稿持久化：选稿弹窗与编辑专家 Agent 调出的书稿库（~/.deskmate/manuscripts）
# --------------------------------------------------------------------------- #
def _manuscript_dir() -> Path:
    d = Path(os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))) / "manuscripts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(name: str) -> str:
    s = name.strip()
    # 只保留字母、数字、下划线、连字符、中文字符（最安全的方式）
    s = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", s)
    # 移除开头的点和空格
    s = s.lstrip(". ")
    return s or "manuscript"


def save_manuscript(name: str, chapters: list[dict]) -> dict:
    """把整本书稿（含各章正文）存到 ~/.deskmate/manuscripts/<name>.json，供日后「审阅第X章」直接调出。"""
    d = _manuscript_dir()
    payload = {
        "name": name,
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "chapters": chapters,
    }
    (d / f"{_safe_name(name)}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return {"name": name, "chapter_count": len(chapters), "saved_at": payload["saved_at"]}


def list_manuscripts() -> list[dict]:
    d = _manuscript_dir()
    out: list[dict] = []
    for p in sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append(
            {
                "name": data.get("name", p.stem),
                "chapter_count": len(data.get("chapters", [])),
                "saved_at": data.get("saved_at", ""),
            }
        )
    return out


def load_manuscript(name: str) -> Optional[list[dict]]:
    d = _manuscript_dir()
    p = d / f"{_safe_name(name)}.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data.get("chapters")

