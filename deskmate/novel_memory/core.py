"""小说记忆核心逻辑：扫描、抽取人物圣经、注入 OpenWorker workspace 记忆、跨章检索。

模型调用统一走 OpenWorker 的 SecretStore 体系（deskmate.memory.llm），
自动跟随用户在 Settings 中选择的 provider，无需单独配置。

记忆库：OpenWorker 的 SQLite（state_dir/coworker.db），scope=WORKSPACE、workspace=小说目录绝对路径，
会被 build_engine 自动注入每轮对话，从而根治「敌人变朋友」。
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 稳定公开 API（圆桌等外部模块只应依赖以下名字，避免直连下划线私有函数/常量，
# 否则 core.py 重构这些下划线符号时会静默失效）。
__all__ = [
    "build_bible",
    "inject_bible",
    "read_bible",
    "build_fts5_index",
    "novel_search_fts5",
    "resolve_root",
    "bible_root",
    "bible_file_paths",
    "iter_text_files",
    "keyword_recall",
]

# ---------------------------------------------------------------------------
# 模型调用
# ---------------------------------------------------------------------------

_BIBLE_DIR = ".deskmate"
_BIBLE_FILES = {
    "CHARACTERS": "CHARACTERS.md",  # 人物关系圣经（最关键的注入）
    "OUTLINE": "OUTLINE.md",        # 情节主线 / 弧光
    "TIMELINE": "TIMELINE.md",       # 时间线
    "NOTES": "NOTES.md",             # 世界观 / 伏笔 / 写作备注
}

# 扫描的小说文本扩展名（.docx 需经 python-docx 解析，见 _read_file_text）
_TEXT_EXTS = {".txt", ".md", ".markdown", ".text", ".novel", ".docx"}

# 需要专用解析器的二进制格式：不能直接 read_text，否则得到乱码
_BINARY_EXTS = {".docx"}

# FTS5 索引数据库文件名（相对于 .deskmate 目录）
_FTS5_DB = "fts5.db"


def _client():
    from openai import OpenAI
    from deskmate.memory.llm import _api_key, _base_url

    return OpenAI(api_key=_api_key(), base_url=_base_url())


def _model() -> str:
    from deskmate.memory.llm import _model as _llm_model
    return _llm_model()


# 已知会产出 reasoning_content 的推理模型（如 deepseek-v4-flash / deepseek-r1）。
# 它们的推理链会计入 max_tokens 预算，推理一长就把正文挤成 0 字。
_REASONING_MODEL_HINTS = (
    "reasoner", "r1", "-flash", "thinking", "qwq",
    "o1", "o3", "o4", "deepseek-reasoner", "reasoning",
)


def _is_reasoning_model(model: str) -> bool:
    m = (model or "").lower()
    return any(h in m for h in _REASONING_MODEL_HINTS)


def _chat(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    timeout: float = 90,
    max_tokens: int = 3000,
    reasoning_effort: str = "low",
) -> str:
    """调用 LLM 一次。

    max_tokens 必须显式给足：默认走 API 缺省值时，圣经合并这类「长输出」场景
    会被静默截断成半份文档（_parse_bible_docs 只能解析出 CHARACTERS）。

    reasoning_effort=low：对推理模型（deepseek-v4-flash 等）把推理预算压到最低。
    这类「从正文抽取事实 / 合并成圣经」的批处理任务不需要深度推理，压低推理反而
    能让正文拿到更多 max_tokens 预算，避免「四份文档全空」。非推理模型（按模型名
    启发式判定）不会带这个参数，避免 provider 报不识别的字段。
    """
    from openai import BadRequestError

    client = _client()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    extra = None
    if _is_reasoning_model(_model()) and reasoning_effort:
        extra = {"reasoning_effort": reasoning_effort}

    def _create():
        return client.chat.completions.create(
            model=_model(),
            temperature=temperature,
            max_tokens=max_tokens,
            messages=messages,
            timeout=timeout,
            **({"extra_body": extra} if extra else {}),
        )

    try:
        resp = _create()
    except BadRequestError:
        # 个别 provider 不认 reasoning_effort，去掉重试一次
        if extra:
            resp = client.chat.completions.create(
                model=_model(),
                temperature=temperature,
                max_tokens=max_tokens,
                messages=messages,
                timeout=timeout,
            )
        else:
            raise
    return (resp.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# 路径与读取辅助（统一支持「目录」与「单文件」两种入口 + .docx 解析）
# ---------------------------------------------------------------------------


def _read_file_text(p: Path) -> str:
    """读取单个正文文件为纯文本。.docx 走 python-docx 解析，其余按 UTF-8 读。

    任何失败都返回空串，由调用方跳过——扫描全书时不该因一个坏文件整体失败。
    """
    try:
        if p.suffix.lower() in _BINARY_EXTS:
            from deskmate.writing import read_text_from_path

            # max_chars 给足：writing 层默认 20 万字，长篇会被静默腰斩
            return read_text_from_path(str(p), max_chars=10_000_000)
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _resolve_root(path: str) -> tuple[Path, Path]:
    """把入参解析成 (扫描根, 圣经根)。

    - 传目录 → 扫描该目录，圣经落在 <目录>/.deskmate/
    - 传单文件 → 只扫描该文件，圣经落在 <文件同级目录>/.deskmate/
      （与 deskmate.tools.review.load_manuscript_file 的 read_bible(pp.parent) 约定一致）
    """
    p = Path(path).expanduser().resolve()
    if p.is_dir():
        return p, p
    if p.is_file():
        return p, p.parent
    raise FileNotFoundError(f"路径不存在：{path}")


def _bible_root(path: str) -> Path:
    """圣经 / FTS5 索引的落盘根目录（传文件时取其父目录）。"""
    p = Path(path).expanduser().resolve()
    return p.parent if p.is_file() else p


def _iter_text_files(scan_root: Path) -> list[Path]:
    """收集待扫描的正文文件（稳定排序）。scan_root 为文件时即返回它自身。"""
    if scan_root.is_file():
        return [scan_root]
    return sorted(
        p for p in scan_root.rglob("*")
        if p.is_file() and p.suffix.lower() in _TEXT_EXTS
        and not any(part.startswith(".") or part == ".deskmate" or part == ".git"
                    for part in p.relative_to(scan_root).parts)
    )


# ---- 稳定公开 API（供圆桌等外部模块调用，避免依赖上方下划线私有符号）----

def resolve_root(path: str) -> tuple[Path, Path]:
    """公开包装：见 ``_resolve_root``。"""
    return _resolve_root(path)


def bible_root(folder: str) -> Path:
    """公开包装：圣经 / FTS5 索引的落盘根目录。见 ``_bible_root``。"""
    return _bible_root(folder)


def bible_file_paths(folder: str) -> list[Path]:
    """返回四份圣经文件的完整路径列表（顺序同 ``_BIBLE_FILES``）。

    替代直接拼接 ``_bible_root(folder) / _BIBLE_DIR / fn``，调用方无需感知
    内部目录名与文件名映射。
    """
    return [bible_root(folder) / _BIBLE_DIR / fn for fn in _BIBLE_FILES.values()]


def iter_text_files(scan_root: Path) -> list[Path]:
    """公开包装：见 ``_iter_text_files``。"""
    return _iter_text_files(scan_root)


def _rel_name(p: Path, scan_root: Path) -> str:
    """相对展示名：scan_root 是文件时用文件名，否则用相对路径。"""
    return p.name if scan_root.is_file() else p.relative_to(scan_root).as_posix()


# ---------------------------------------------------------------------------
# 扫描与分块
# ---------------------------------------------------------------------------


def scan_novel(folder: str, max_chars: int = 400_000) -> tuple[str, list[str]]:
    """读取小说正文，返回 (合并正文, 文件名列表)。

    folder 可以是小说文件夹（递归扫描，跳过 .deskmate / .git / 隐藏目录），
    也可以是单个 .txt / .docx 稿件文件。超大则按比例截断到 max_chars。
    """
    scan_root, _ = _resolve_root(folder)

    chunks: list[str] = []
    titles: list[str] = []
    total = 0
    files = _iter_text_files(scan_root)
    for p in files:
        text = _read_file_text(p)
        if not text.strip():
            continue
        rel = _rel_name(p, scan_root)
        title = f"# 文件：{rel}"
        titles.append(rel)
        block = f"{title}\n\n{text}\n\n"
        # 单文件超长则截断该文件
        if len(block) > max_chars:
            block = block[:max_chars]
        # 总额超限则停止读取更多文件
        if total + len(block) > max_chars:
            remain = max_chars - total
            if remain > 2000:
                chunks.append(block[:remain])
                total += remain
            break
        chunks.append(block)
        total += len(block)
    return "\n".join(chunks), titles


def _split_batches(text: str, size: int = 12000, overlap: int = 400) -> list[str]:
    """在段落边界上把正文切成带重叠的批次。"""
    paras = text.split("\n\n")
    batches: list[str] = []
    cur = ""
    for para in paras:
        if len(cur) + len(para) + 2 > size and cur:
            batches.append(cur)
            # 重叠：保留末尾 overlap 字符作为下一批开头
            cur = cur[-overlap:] if overlap else ""
        cur = (cur + "\n\n" + para) if cur else para
    if cur:
        batches.append(cur)
    return batches


# ---------------------------------------------------------------------------
# 人物圣经抽取
# ---------------------------------------------------------------------------

_EXTRACT_SYS = (
    "你是长篇小说的关系分析师。用户会给你一段小说正文（可能只是一部分）。"
    "请只基于【这段正文里明确写出的内容】抽取事实，严禁臆测或脑补。"
    "输出 Markdown，分四节：\n"
    "## 人物\n- 列出本段出现的新人物（姓名/别名），一句话身份。\n"
    "## 关系\n- 逐条列出人物间关系，必须标注关系类型（敌 / 友 / 亲 / 暧昧 / 上下级 / 同门 / 其他）"
    "并尽量注明依据（如「第X章 两人对峙」）。\n"
    "## 事件\n- 本段关键情节节点（推动关系变化的事件）。\n"
    "## 设定\n- 本段出现的世界观 / 势力 / 地点设定。\n"
    "如果某节无内容就写「（无）」。只输出这四节，不要寒暄。"
)

_MERGE_SYS = (
    "你是长篇小说的关系总编。下面是一本书各段落的分析片段（可能重复、冲突）。"
    "请综合成一份【权威人物关系圣经】，作为后续所有人物讨论的唯一准绳。规则：\n"
    "1. 合并同名人物，去重；关系冲突时以「更靠后章节的明确描写」为准，并注明分歧。\n"
    "2. 关系必须带类型（敌/友/亲/暧昧/上下级/同门/其他）和关键依据（章节或情节）。\n"
    "3. 禁止写入任何正文未明确支持的关系；存疑的放进「待确认」。\n"
    "必须输出【四份】独立 Markdown 文档，用分隔符 ===== 隔开，顺序固定为：\n"
    "=====\n## CHARACTERS\n（人物关系总表，核心）\n"
    "=====\n## OUTLINE\n（情节主线与弧光）\n"
    "=====\n## TIMELINE\n（关键事件时间线）\n"
    "=====\n## NOTES\n（世界观/势力/伏笔/待确认）\n"
    "每份文档【第一行必须是 ## CHARACTERS / ## OUTLINE / ## TIMELINE / ## NOTES 之一"
    "（英文大写，严禁写中文标题）】。四份缺一不可，即使某份内容少也要保留其标题行。"
    "只输出这四份内容，不要解释、不要寒暄。"
)

_DOC_TAGS = list(_BIBLE_FILES.keys())

# 单次合并允许喂进去的抽取文本上限（字符）。超过则先分组预合并（两级 reduce）。
# 依据：实测 DeepSeek 在输入 >12000 字且要求长输出时，返回空内容的概率显著上升。
_MERGE_INPUT_LIMIT = 12000

# 抽取单批摘要 / 合并长文档 的输出预算。
# 推理模型（deepseek-v4-flash 等）的 reasoning_content 也计入 max_tokens，
# 故留足余量：抽取批（输入 ~12K 字）给 8000，合并（可能含两级 reduce）给 16000，
# 即便推理链占用数千 token，正文仍有空间。max_tokens 是上限而非目标，模型用完即停，
# 不会因此多耗 token。
_EXTRACT_MAX_TOKENS = 8000
_MERGE_MAX_TOKENS = 16000


def _merge_extracts(extracts: list[str], progress_callback: callable = None) -> str:
    """把逐批抽取结果归并成一份圣经，输入过长时自动走两级 reduce。

    长篇（20 万字≈17 批）直接把所有抽取拼进一次调用会超过模型的稳定输入区间，
    表现为「返回空内容」或「四份文档只剩一份」。这里先按 _MERGE_INPUT_LIMIT
    分组预合并，再对预合并结果做最终合并。
    """
    joined = "\n\n".join(extracts)
    if len(joined) <= _MERGE_INPUT_LIMIT:
        return _chat(_MERGE_SYS, joined, timeout=120, max_tokens=_MERGE_MAX_TOKENS)

    # 分组：按累计字符数切成若干组
    groups: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    for ex in extracts:
        if cur and cur_len + len(ex) > _MERGE_INPUT_LIMIT:
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(ex)
        cur_len += len(ex)
    if cur:
        groups.append(cur)

    if progress_callback:
        progress_callback("merging_partial", {"groups": len(groups)})

    partials: list[str] = []
    for i, g in enumerate(groups):
        try:
            part = _chat(
                _MERGE_SYS, "\n\n".join(g), timeout=120, max_tokens=_MERGE_MAX_TOKENS
            )
        except Exception as e:  # noqa: BLE001
            part = f"（第 {i + 1} 组预合并失败：{e}）"
        partials.append(f"===== 预合并 {i + 1}/{len(groups)} =====\n{part}")
        if progress_callback:
            progress_callback("merge_progress", {"idx": i + 1, "total": len(groups)})

    final_input = "\n\n".join(partials)
    # 极端情况（书特别长）：预合并结果仍超限，做一次保守截断而非直接失败
    if len(final_input) > _MERGE_INPUT_LIMIT * 2:
        final_input = final_input[: _MERGE_INPUT_LIMIT * 2]
    return _chat(_MERGE_SYS, final_input, timeout=180, max_tokens=_MERGE_MAX_TOKENS)


def build_bible(folder: str, progress_callback: callable = None) -> dict[str, str]:
    """通读全书 -> 抽取 -> 合并成四份圣经文档。返回 {文档名: markdown}。

    folder 可为小说文件夹或单个 .txt / .docx 稿件文件。
    """
    if progress_callback:
        progress_callback("scanning", {"folder": folder})
    text, titles = scan_novel(folder)
    if not text.strip():
        raise ValueError(
            f"在 {folder} 未找到可读的小说文本（支持 .txt / .md / .docx）。"
        )

    # 1) 逐批抽取（并发，大幅缩短总耗时）
    batches = _split_batches(text)
    extracts: list[str] = [""] * len(batches)
    if progress_callback:
        progress_callback("extracting", {"total": len(batches), "folder": folder})

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _extract_one(idx: int, batch: str) -> tuple[int, str]:
        try:
            part = _chat(_EXTRACT_SYS, batch, max_tokens=_EXTRACT_MAX_TOKENS)
        except Exception as e:
            part = f"（第 {idx + 1} 批抽取失败：{e}）"
        return idx, part

    with ThreadPoolExecutor(max_workers=min(5, len(batches))) as pool:
        futures = {
            pool.submit(_extract_one, i, batch): i
            for i, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            idx, part = future.result()
            extracts[idx] = f"===== 第 {idx + 1} 批 =====\n{part}"
            if progress_callback:
                progress_callback("extract_progress", {"idx": idx + 1, "total": len(batches)})

    # 2) 合并成圣经（长篇自动走两级 reduce，见 _merge_extracts）
    if progress_callback:
        progress_callback("merging", {"total_batches": len(batches)})
    merged = _merge_extracts(extracts, progress_callback)

    # 3) 解析成四份文档
    docs = _parse_bible_docs(merged)
    # 兜底：若解析失败，整体存为 CHARACTERS
    if not docs.get("CHARACTERS"):
        docs["CHARACTERS"] = merged

    if progress_callback:
        progress_callback("completed", {"docs": list(docs.keys()), "folder": folder})
    return docs


def _parse_bible_docs(merged: str) -> dict[str, str]:
    """按 ===== 分隔符与文档标记切分合并输出。

    兼容两种标题写法（模型习惯不同）：
    - 英文标记开头：  ``## CHARACTERS``
    - 中文标题含英文：``## 人物关系总表（CHARACTERS）``
    均按标题行里的英文标签归类。
    """
    docs: dict[str, str] = {}
    # 先按分隔符切块
    blocks = re.split(r"^=====.*$", merged, flags=re.MULTILINE)
    # 退化：若没有分隔符，按标题切（中英文标题都覆盖）
    if len(blocks) <= 1:
        blocks = re.split(
            r"(?=^##\s*(?:CHARACTERS|OUTLINE|TIMELINE|NOTES|人物关系|情节主线|关键事件时间线|世界观))",
            merged, flags=re.MULTILINE,
        )
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        # 取标题行（第一行）找英文标签，兼容「中文（ENGLISH）」写法
        first_line = b.split("\n", 1)[0]
        m = re.search(r"\b(CHARACTERS|OUTLINE|TIMELINE|NOTES)\b", first_line, flags=re.IGNORECASE)
        if m:
            tag = m.group(1).upper()
            docs[tag] = b
    return docs


def incremental_bible_update(folder: str, progress_callback: callable = None) -> dict[str, str] | None:
    """增量更新圣经：只扫描修改过的文件，合并到现有圣经中。

    与 build_bible（全量重建）不同，本函数：
    1. 读取现有圣经（.deskmate/CHARACTERS.md 等）
    2. 只扫描新增/修改的文件（对比 .deskmate/ 文件的 mtime 和正文文件的 mtime）
    3. 只将新内容抽取后合并到现有圣经

    Args:
        folder: 小说文件夹绝对路径，或单个 .txt / .docx 稿件文件路径
        progress_callback: 可选进度回调

    Returns:
        更新后的圣经 dict，或 None（无变化）
    """
    scan_root, bible_root = _resolve_root(folder)
    bible_dir = bible_root / _BIBLE_DIR

    # 读取现有圣经
    existing = read_bible(folder)
    if not existing:
        # 没有现有圣经，退化到全量
        if progress_callback:
            progress_callback("no_existing_bible", {"folder": folder})
        return None

    # 找新增/修改过的文件
    bible_mtime = 0.0
    for tag, fname in _BIBLE_FILES.items():
        fp = bible_dir / fname
        if fp.is_file():
            bible_mtime = max(bible_mtime, fp.stat().st_mtime)

    new_files = []
    text_files = _iter_text_files(scan_root)
    for p in text_files:
        if p.stat().st_mtime > bible_mtime:
            new_files.append(p)

    if not new_files:
        if progress_callback:
            progress_callback("no_new_content", {"folder": folder})
        return existing  # 无新内容，返回现有圣经

    # 读取新文件内容
    if progress_callback:
        progress_callback("scanning_new", {"count": len(new_files), "folder": folder})
    new_text = ""
    new_titles = []
    for p in new_files:
        text = _read_file_text(p)
        if not text.strip():
            continue
        rel = _rel_name(p, scan_root)
        new_titles.append(rel)
        new_text += f"# 文件：{rel}\n\n{text}\n\n"

    if not new_text.strip():
        if progress_callback:
            progress_callback("no_new_content", {"folder": folder})
        return existing

    # 抽取新内容
    batches = _split_batches(new_text)
    if not batches:
        return existing

    if progress_callback:
        progress_callback("extracting_new", {"total": len(batches), "files": new_titles})

    from concurrent.futures import ThreadPoolExecutor, as_completed

    extracts: list[str] = [""] * len(batches)

    def _extract_one(idx: int, batch: str) -> tuple[int, str]:
        try:
            part = _chat(_EXTRACT_SYS, batch, max_tokens=_EXTRACT_MAX_TOKENS)
        except Exception as e:
            part = f"（第 {idx + 1} 批抽取失败：{e}）"
        return idx, part

    with ThreadPoolExecutor(max_workers=min(5, len(batches))) as pool:
        futures = {
            pool.submit(_extract_one, i, batch): i
            for i, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            idx, part = future.result()
            extracts[idx] = f"===== 第 {idx + 1} 批 =====\n{part}"
            if progress_callback:
                progress_callback("extract_progress", {"idx": idx + 1, "total": len(batches)})

    # 合并到现有圣经（带已有内容上下文）
    if progress_callback:
        progress_callback("merging", {"total_batches": len(batches)})
    existing_text = "\n\n".join(
        f"## {tag}\n{content}" for tag, content in existing.items() if content
    )
    merge_prompt = (
        "以下是一本小说的「现有圣经」和「新章节的抽取结果」。\n"
        "请将新章节的信息合并到现有圣经中，更新人物关系、情节、时间线。\n"
        "规则：新章节的明确描写优先级高于旧数据；如果新章节与旧数据冲突，"
        "以新章节为准并注明。\n\n"
        "必须输出【四份】独立 Markdown 文档，用分隔符 ===== 隔开，顺序固定为：\n"
        "=====\n## CHARACTERS\n（人物关系总表，核心）\n"
        "=====\n## OUTLINE\n（情节主线与弧光）\n"
        "=====\n## TIMELINE\n（关键事件时间线）\n"
        "=====\n## NOTES\n（世界观/势力/伏笔/待确认）\n"
        "每份文档【第一行必须是 ## CHARACTERS / ## OUTLINE / ## TIMELINE / ## NOTES 之一"
        "（英文大写，严禁写中文标题）】。四份缺一不可，即使某份内容少也要保留其标题行。"
        "只输出这四份内容，不要解释、不要寒暄。"
    )
    extracts_joined = "\n\n".join(extracts)
    # 输入双侧都限长：现有圣经 + 新抽取合计控制在 _MERGE_INPUT_LIMIT 内，
    # 否则模型在「长输入 + 长输出」下容易直接返回空内容。
    half = _MERGE_INPUT_LIMIT // 2
    merged = _chat(
        merge_prompt,
        f"【现有圣经】\n{existing_text[:half]}\n\n"
        f"【新章节抽取】\n{extracts_joined[:half]}",
        timeout=120,
        max_tokens=_MERGE_MAX_TOKENS,
    )

    docs = _parse_bible_docs(merged)
    if not docs.get("CHARACTERS"):
        docs = existing  # 合并失败，保底返回现有圣经

    if progress_callback:
        progress_callback("completed", {"docs": list(docs.keys()), "folder": folder})
    return docs


# ---------------------------------------------------------------------------
# 写入 OpenWorker workspace 记忆（自动注入）
# ---------------------------------------------------------------------------


def _db_path() -> Path:
    return Path(
        os.environ.get("COWORKER_DB")
        or os.path.expanduser("~/.config/coworker/coworker.db")
    ).expanduser()


def _workspace_key() -> str:
    return os.environ.get("COWORKER_WORKSPACE_KEY", "deskmate-novel")


def inject_bible(folder: str, docs: dict[str, str]) -> int:
    """把四份圣经写入 <folder>/.deskmate/ 并 upsert 为 OpenWorker workspace 记忆。返回写入条数。

    使用 ``coworker.memory.SQLiteMemoryStore`` API 写入，而非裸 SQL -- 这样 coworker
    schema 变更时（加字段、改表名）不会静默失败。
    """
    root = _bible_root(folder)
    bible_dir = root / _BIBLE_DIR
    bible_dir.mkdir(parents=True, exist_ok=True)
    for tag, fname in _BIBLE_FILES.items():
        content = docs.get(tag)
        if content:
            (bible_dir / fname).write_text(content, encoding="utf-8")

    ws = str(root)
    db = _db_path()

    # 延迟导入：仅在真正写入时加载 coworker.memory，避免循环依赖
    from coworker.memory import SQLiteMemoryStore, Scope

    store = SQLiteMemoryStore(db)

    # 取出当前 workspace 下所有记忆，按 key 去重做 upsert
    from coworker.memory.base import MemoryItem

    existing_items: list[MemoryItem] = store.list(scope=Scope.WORKSPACE, workspace=ws)
    key_to_item: dict[str, MemoryItem] = {
        item.key: item for item in existing_items if item.key
    }

    written = 0
    for tag, _fname in _BIBLE_FILES.items():
        content = docs.get(tag)
        if not content:
            continue
        key = f"{_workspace_key()}:{tag}:{root.name}"
        existing = key_to_item.get(key)
        if existing:
            store.update(existing.id, content)
        else:
            store.add(content, scope=Scope.WORKSPACE, key=key, workspace=ws)
        written += 1
    store.close()
    return written


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------


def read_bible(folder: str) -> dict[str, str]:
    """读取已生成的四份圣经。folder 可为小说目录或单个稿件文件。"""
    bible_dir = _bible_root(folder) / _BIBLE_DIR
    out: dict[str, str] = {}
    for tag, fname in _BIBLE_FILES.items():
        p = bible_dir / fname
        if p.is_file():
            out[tag] = p.read_text(encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# FTS5 全文检索
# ---------------------------------------------------------------------------

def _fts5_db_path(folder: str) -> Path:
    """返回 FTS5 数据库的完整路径（folder 可为目录或单个稿件文件）。"""
    return _bible_root(folder) / _BIBLE_DIR / _FTS5_DB


def _fts5_connection(folder: str) -> sqlite3.Connection | None:
    """返回 FTS5 数据库连接，若不存在则返回 None。"""
    db_path = _fts5_db_path(folder)
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def build_fts5_index(folder: str) -> int:
    """构建或增量更新 FTS5 全文索引。

    扫描所有文本文件，将内容写入 SQLite FTS5 虚拟表。
    仅重新索引 mtime 发生变化的文件（增量更新）。

    返回本次索引的文件数。
    """
    scan_root, _ = _resolve_root(folder)
    db_path = _fts5_db_path(folder)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")

        # 创建 FTS5 虚拟表（如果不存在）
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS novels USING fts5(
                file_path,
                title,
                content,
                tokenize='unicode61'
            )
        """)

        # 创建文件跟踪表（记录 mtime 以实现增量索引）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_tracker (
                file_path TEXT PRIMARY KEY,
                mtime REAL NOT NULL,
                indexed_at REAL NOT NULL
            )
        """)

        # 收集所有文本文件
        text_files = _iter_text_files(scan_root)

        indexed_count = 0
        for p in text_files:
            rel_path = _rel_name(p, scan_root)
            current_mtime = p.stat().st_mtime

            # 检查 mtime 是否变化
            row = conn.execute(
                "SELECT mtime FROM file_tracker WHERE file_path = ?",
                (rel_path,),
            ).fetchone()

            if row is not None and abs(row[0] - current_mtime) < 0.001:
                continue  # 未变化，跳过

            text = _read_file_text(p)
            if not text.strip():
                continue

            title = p.stem  # 文件名（不含扩展名）作为标题

            # 删除旧记录（如果有），再插入新记录
            conn.execute("DELETE FROM novels WHERE file_path = ?", (rel_path,))
            conn.execute(
                "INSERT INTO novels (file_path, title, content) VALUES (?, ?, ?)",
                (rel_path, title, text),
            )
            conn.execute(
                "INSERT OR REPLACE INTO file_tracker (file_path, mtime, indexed_at) VALUES (?, ?, ?)",
                (rel_path, current_mtime, time.time()),
            )
            indexed_count += 1

        conn.commit()
        return indexed_count
    finally:
        conn.close()


def novel_search_fts5(query: str, folder: str, max_results: int = 10) -> str:
    """使用 FTS5 进行全文检索。

    如果 FTS5 索引不存在，自动回退到 keyword-based novel_recall。
    支持 CJK 查询：FTS5 的 unicode61 tokenizer 不原生支持 CJK 分词，
    因此对于 CJK 密集的查询会使用 LIKE 作为回退。
    """
    db_path = _fts5_db_path(folder)

    # 索引不存在 -> 回退到 keyword 检索
    if not db_path.exists():
        return _keyword_recall(query, folder, max_snippets=max_results)

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:

            # 检查是否包含 CJK 字符（中文、日文、韩文）
            has_cjk = bool(re.search(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]", query))
    
            if has_cjk:
                # FTS5 unicode61 不原生支持 CJK 分词，回退到 LIKE 搜索
                # 提取关键词（≥2字），每个关键词用 LIKE 匹配
                kws = list(dict.fromkeys(
                    w for w in re.split(r"[\s，。、？！,.?!；：“”‘’…—()\[\]【】-]+", query)
                    if len(w) >= 2
                ))[:8]
    
                if not kws:
                    return _keyword_recall(query, folder, max_snippets=max_results)
    
                # 构建 LIKE 子句（转义 % 和 _ 以防止 LIKE 模式注入）
                like_clauses = " OR ".join("content LIKE ? ESCAPE '\\'" for _ in kws)
                like_params = [f"%{kw.replace('%', '\\%').replace('_', '\\_')}%" for kw in kws]
                sql = f"""
                    SELECT file_path, title, content
                    FROM novels
                    WHERE {like_clauses}
                    ORDER BY rowid
                    LIMIT ?
                """
                rows = conn.execute(sql, (*like_params, max_results)).fetchall()
            else:
                # 非 CJK 查询：使用 FTS5 原生 MATCH
                # 对查询进行简单转义（移除 FTS5 特殊字符）
                sanitized = re.sub(r'[^\w\s]', ' ', query).strip()
                if not sanitized:
                    return _keyword_recall(query, folder, max_snippets=max_results)
    
                rows = conn.execute(
                    "SELECT file_path, title, content, rank FROM novels WHERE content MATCH ? ORDER BY rank LIMIT ?",
                    (sanitized, max_results),
                ).fetchall()
        finally:
            conn.close()

        if not rows:
            # FTS5 无结果 -> 回退到 keyword 检索
            return _keyword_recall(query, folder, max_snippets=max_results)

        # 格式化结果
        parts: list[str] = []
        for i, row in enumerate(rows, 1):
            file_path = row["file_path"]
            content = row["content"]
            # 查找查询词在内容中的位置，提取上下文片段
            snippet = _extract_snippet(content, query, context_chars=200)
            parts.append(f"[{i}] {file_path}")
            parts.append(f"    {snippet}")
            parts.append("")

        # 附加圣经摘要
        bible = read_bible(folder)
        if bible:
            bible_parts: list[str] = []
            if bible.get("CHARACTERS"):
                bible_parts.append(bible["CHARACTERS"][:4000])
            for tag in ("OUTLINE", "TIMELINE", "NOTES"):
                if bible.get(tag):
                    bible_parts.append(f"## {tag}\n{bible[tag][:1500]}")
            if bible_parts:
                parts.append("【人物圣经摘要】")
                parts.append("\n\n".join(bible_parts))

        return "\n".join(parts)

    except Exception as e:
        # 任何 FTS5 异常 -> 安全回退到 keyword 检索
        logger.warning("FTS5 search failed, falling back to keyword recall: %s", e)
        return _keyword_recall(query, folder, max_snippets=max_results)


def _extract_snippet(text: str, query: str, context_chars: int = 200) -> str:
    """从文本中提取包含查询词的最佳上下文片段。"""
    # 提取查询中的关键词
    kws = [w for w in re.split(r"[\s，。、？！,.?!；：“”‘’…—()\[\]【】-]+", query) if len(w) >= 2]
    if not kws:
        return text[:context_chars * 2].strip()

    # 找到第一个关键词的位置
    best_pos = -1
    for kw in kws:
        pos = text.find(kw)
        if pos != -1 and (best_pos == -1 or pos < best_pos):
            best_pos = pos

    if best_pos == -1:
        return text[:context_chars * 2].strip()

    start = max(0, best_pos - context_chars)
    end = min(len(text), best_pos + context_chars)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def fts5_index_status(folder: str) -> str:
    """报告 FTS5 索引的状态。

    返回：索引是否存在、文件数、最后更新时间。
    """
    db_path = _fts5_db_path(folder)
    if not db_path.exists():
        return "FTS5 索引不存在。请运行 build_fts5_index() 创建。"

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            # 查询索引文件数
            count = conn.execute("SELECT COUNT(*) FROM novels").fetchone()[0]

            # 查询最后更新时间
            last = conn.execute(
                "SELECT MAX(indexed_at) FROM file_tracker"
            ).fetchone()[0]

            # 查询数据库文件信息
            db_size = db_path.stat().st_size
            db_mtime = db_path.stat().st_mtime
        finally:
            conn.close()

        lines = [
            f"FTS5 索引状态：",
            f"  - 数据库：{db_path}",
            f"  - 文件数：{count}",
            f"  - 数据库大小：{db_size / 1024:.1f} KB",
            f"  - 最后索引时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last)) if last else '未知'}",
            f"  - 数据库修改时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(db_mtime))}",
        ]
        return "\n".join(lines)

    except Exception as e:
        return f"FTS5 索引状态查询失败：{e}"


def _keyword_recall(query: str, folder: str, max_snippets: int = 8) -> str:
    """纯关键词检索（原始实现）：逐文件扫描，正则匹配关键词提取片段。

    这是 FTS5 不可用时的回退方案。    """
    scan_root, _ = _resolve_root(folder)
    parts: list[str] = []

    bible = read_bible(folder)
    if bible:
        parts.append("【人物圣经摘要】")
        if bible.get("CHARACTERS"):
            parts.append(bible["CHARACTERS"][:6000])
        for tag in ("OUTLINE", "TIMELINE", "NOTES"):
            if bible.get(tag):
                parts.append(f"\n## {tag}\n{bible[tag][:2000]}")

    # 提取所有关键词（≥2字），去重，最多8个
    kws = list(dict.fromkeys(
        w for w in re.split(r"[\s，。、？！,.?!；：“”‘’…—()\[\]【】-]+", query)
        if len(w) >= 2
    ))[:8]
    if not kws:
        if not parts:
            return "（尚未生成人物圣经，请先运行 analyze_novel）"
        return "\n".join(parts)

    # 构建正则：任意关键词命中即匹配
    pattern = re.compile("|".join(re.escape(k) for k in kws), re.IGNORECASE)

    # 收集候选片段：(score, file_rel, snippet_text)
    candidates: list[tuple[int, str, str]] = []
    text_files = _iter_text_files(scan_root)
    for p in text_files:
        text = _read_file_text(p)
        if not text.strip():
            continue
        rel = _rel_name(p, scan_root)
        covered: list[tuple[int, int]] = []  # 已覆盖的字符位置（去重重叠片段）
        file_hits = 0
        for m in pattern.finditer(text):
            s = max(0, m.start() - 200)
            e = min(len(text), m.end() + 200)
            # 跳过已被之前片段覆盖的区域
            if any(s < ce and e > cs for cs, ce in covered):
                continue
            covered.append((s, e))
            snippet_raw = text[s:e].strip()
            hit_count = sum(1 for kw in kws if kw in snippet_raw)
            snippet = f"[{rel}] …{snippet_raw}…"
            candidates.append((hit_count, rel, snippet))
            file_hits += 1
            if file_hits >= 3:  # 每个文件最多3个片段
                break
        # 限制候选总数
        if len(candidates) >= max_snippets * 3:
            break

    # 按命中数降序取 top N
    candidates.sort(key=lambda x: -x[0])
    top = [c[2] for c in candidates[:max_snippets]]

    if top:
        parts.append(f"\n【正文命中片段】（关键词：{'、'.join(kws[:5])}）\n" + "\n---\n".join(top))

    if not parts:
        return "（尚未生成人物圣经，请先运行 analyze_novel）"
    return "\n".join(parts)


def keyword_recall(query: str, folder: str, max_snippets: int = 8) -> str:
    """公开包装：对中文更可靠的纯关键词检索（见 ``_keyword_recall``）。

    圆桌等外部模块应调用本函数而非直连 ``_keyword_recall``。
    """
    return _keyword_recall(query, folder, max_snippets=max_snippets)


def novel_recall(query: str, folder: str, max_snippets: int = 8) -> str:
    """跨章检索：优先使用 FTS5 全文索引，无索引或无结果时回退到关键词匹配。

    检索策略：
    1. 尝试 FTS5 全文检索（如果索引存在）
    2. FTS5 无结果或无索引时，降级到关键词逐文件扫描
    """
    # 尝试 FTS5 优先路径
    db_path = _fts5_db_path(folder)
    if db_path.exists():
        try:
            result = novel_search_fts5(query, folder, max_results=max_snippets)
            return result
        except Exception as e:
            logger.warning("FTS5 search failed, falling back to keyword: %s", e)

    # 回退到纯关键词检索
    return _keyword_recall(query, folder, max_snippets=max_snippets)


def novel_relationships(character: str, folder: str) -> str:
    """查某角色在圣经中的所有关系。"""
    bible = read_bible(folder)
    chars = bible.get("CHARACTERS")
    if not chars:
        return "（尚未生成人物圣经，请先运行 analyze_novel）"
    # 简单按行匹配包含角色名的行
    lines = [ln for ln in chars.splitlines() if character in ln]
    if not lines:
        return f"圣经中未直接提及「{character}」。可运行 novel_recall 做跨章检索。"
    return "\n".join(lines)




# ---------------------------------------------------------------------------
# 矛盾检测
# ---------------------------------------------------------------------------

# 关系对立词组：(正向词, 反向词)
_RELATION_OPPOSITES = [
    ("敌", "友"),
    ("敌对", "友好"),
    ("对手", "伙伴"),
    ("仇", "友"),
    ("恨", "爱"),
    ("对立", "联盟"),
]

# 关系关键词 — 用于在"与X友好"中识别名字边界
_RELATION_CONTEXT = r"[友好对立敌仇恨亲暧昧上下级同门联盟伙伴对手]"


def _extract_relationship_pairs(text: str) -> dict[tuple[str, str], list[str]]:
    """从人物圣经文本中提取 (角色A, 角色B) -> [关系关键词列表] 的映射。

    匹配两种模式：
    1. "- 张三：...与李四友好" — 行首名字为主语，`与X` 为目标
    2. "张三与李四关系密切" / "张三和李四" — 行内 A与B / A和B
    """
    pairs: dict[tuple[str, str], list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # 模式1：行首 "- NAME：描述...与TARGET关系词"
        m = re.match(r"^[-*]?\s*([一-鿿]{2,3})[：:]", line)
        if m:
            subject = m.group(1)
            rest = line[m.end():]
            # 非贪婪匹配：优先 2 字名（中文名最常见长度）
            for tm in re.finditer(r"与([一-鿿]{2,3}?)(?=" + _RELATION_CONTEXT + r")", rest):
                target = tm.group(1)
                key = tuple(sorted([subject, target]))
                rel_words = re.findall(
                    r"[敌友仇恨亲暧昧上下级同门对手伙伴对立联盟爱]",
                    rest[tm.end(1):tm.end(1) + 10],
                )
                if key not in pairs:
                    pairs[key] = []
                pairs[key].extend(rel_words)
            continue

        # 模式2：行内 "A与B" 或 "A和B"（简单提及）
        for tm in re.finditer(r"([一-鿿]{2,3})[与和]([一-鿿]{2,3})", line):
            a, b = tm.group(1), tm.group(2)
            key = tuple(sorted([a, b]))
            rel_words = re.findall(
                r"[敌友仇恨亲暧昧上下级同门对手伙伴对立联盟爱]",
                line[tm.end():tm.end() + 10],
            )
            if key not in pairs:
                pairs[key] = []
            pairs[key].extend(rel_words)

    return pairs


def check_consistency(bible_docs: dict[str, str], new_text: str) -> dict:
    """检测新文本是否与人物圣经中的关系存在矛盾。

    纯逻辑函数，不调用 LLM。基于关键词对立检测。

    Args:
        bible_docs: 从 read_bible() 或 _parse_bible_docs() 得到的圣经文档字典。
        new_text: 新写的章节文本。

    Returns:
        {"consistent": bool, "issues": list[str], "checked_pairs": int}
    """
    chars_text = bible_docs.get("CHARACTERS", "")
    if not chars_text:
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    bible_pairs = _extract_relationship_pairs(chars_text)
    if not bible_pairs:
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    issues: list[str] = []
    checked = 0

    for (a, b), rel_words in bible_pairs.items():
        # 只检查新文本中同时提到两个角色的情况
        if a not in new_text or b not in new_text:
            continue
        checked += 1

        # 判断圣经中这对角色的关系方向
        bible_is_friendly = any(
            fw in rw for rw in rel_words
            for fw in ("友", "好", "爱", "伙伴", "联盟")
        )
        bible_is_hostile = any(
            hw in rw for rw in rel_words
            for hw in ("敌", "仇", "恨", "对立")
        )

        # 在新文本中找这两个角色共同出现的句子
        sentences = re.split(r"[。！？\n]", new_text)
        for sent in sentences:
            if a not in sent or b not in sent:
                continue

            # 检查句子中是否出现与圣经矛盾的关系词
            # pos_word = 敌/仇/恨, neg_word = 友/爱
            # 圣经友好 + 文本出现敌意词(pos_word) → 矛盾
            # 圣经敌对 + 文本出现友好词(neg_word) → 矛盾
            for pos_word, neg_word in _RELATION_OPPOSITES:
                if bible_is_friendly and pos_word in sent:
                    issues.append(
                        f"矛盾：圣经中「{a}」与「{b}」为友好关系，"
                        f"但新文本中「{sent.strip()}」暗示敌对"
                    )
                    break  # 一句话只报一次
                if bible_is_hostile and neg_word in sent:
                    issues.append(
                        f"矛盾：圣经中「{a}」与「{b}」为敌对关系，"
                        f"但新文本中「{sent.strip()}」暗示友好"
                    )
                    break

    # 去重
    issues = list(dict.fromkeys(issues))
    return {
        "consistent": len(issues) == 0,
        "issues": issues,
        "checked_pairs": checked,
    }


# ---------------------------------------------------------------------------
# LLM 语义级矛盾检测
# ---------------------------------------------------------------------------

_CONSISTENCY_LLM_SYS = (
    "你是长篇小说的人物关系审核员。给你一份【人物关系圣经】和一段【新章节文本】。"
    "你的任务是判断新章节是否存在与圣经矛盾的人物关系变化。\n"
    "\n"
    "重点检测以下隐含矛盾（正则检测不到的）：\n"
    "1. 关系性质变化但未明确写出关键词（如「投靠」「背叛」「暗通」「倒戈」"
    "暗示阵营转变，但未出现「敌」「友」字样）\n"
    "2. 行为与关系不一致（圣经中是盟友，新文本中一方偷偷设局对付另一方）\n"
    "3. 态度转变缺乏铺垫（长期友好的角色突然冷淡/敌视，无合理转折）\n"
    "4. 阵营归属矛盾（圣经中属A阵营，新文本中表现为B阵营效力）\n"
    "\n"
    "只报告有把握的矛盾。如果新章节有合理的剧情转折铺垫，不算矛盾。\n"
    "输出严格 JSON：\n"
    '{"consistent": true/false, "issues": ["矛盾1描述", "矛盾2描述"], "reasoning": "简述"}\n'
    "consistent=true 时 issues 为空数组。只输出 JSON，不要其他文字。"
)


def check_consistency_llm(bible_docs: dict[str, str], new_text: str) -> dict:
    """LLM 语义级矛盾检测。

    能识别正则检测不到的隐含关系变化（投靠、背叛、暗通、行为矛盾等）。
    需要 LLM provider 凭证；无密钥或异常时返回空结果（降级为纯正则检测）。

    Args:
        bible_docs: 从 read_bible() 得到的圣经文档字典。
        new_text: 新写的章节文本。

    Returns:
        {"consistent": bool, "issues": list[str], "checked_pairs": int}
    """
    chars_text = bible_docs.get("CHARACTERS", "")
    if not chars_text or not new_text.strip():
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    # 复用 deskmate.memory.llm 的 llm_available（走 SecretStore，自动跟随 provider）
    try:
        from deskmate.memory.llm import llm_available
    except ImportError:
        llm_available = lambda: False  # type: ignore

    if not llm_available():
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    # 构建圣经摘要（限制长度避免 token 爆炸）
    bible_summary = chars_text[:4000]
    outline = bible_docs.get("OUTLINE", "")
    if outline:
        bible_summary += f"\n\n## 情节主线\n{outline[:1500]}"

    new_text_trimmed = new_text[:6000]

    user_prompt = (
        f"【人物关系圣经】\n{bible_summary}\n\n"
        f"---\n\n【新章节文本】\n{new_text_trimmed}\n\n"
        f"请判断新章节是否与圣经中的人物关系存在矛盾。"
    )

    try:
        resp = _chat(_CONSISTENCY_LLM_SYS, user_prompt, temperature=0.15)
    except Exception:
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    # 解析 JSON
    import json

    m = re.search(r"\{.*\}", resp, re.DOTALL)
    if not m:
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    try:
        data = json.loads(m.group(0))
        issues = [str(i) for i in data.get("issues", []) if i]
        return {
            "consistent": bool(data.get("consistent", True)) and len(issues) == 0,
            "issues": issues,
            "checked_pairs": len(issues),  # LLM 检测到的矛盾数
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"consistent": True, "issues": [], "checked_pairs": 0}


def check_consistency_enhanced(bible_docs: dict[str, str], new_text: str) -> dict:
    """增强矛盾检测：先跑正则快速检测，有 API Key 时再跑 LLM 语义检测，合并结果。

    Args:
        bible_docs: 从 read_bible() 得到的圣经文档字典。
        new_text: 新写的章节文本。

    Returns:
        {"consistent": bool, "issues": list[str], "checked_pairs": int}
    """
    # 第一层：正则快速检测（永远执行）
    regex_result = check_consistency(bible_docs, new_text)

    # 第二层：LLM 语义检测（有 API Key 时执行）
    llm_result = check_consistency_llm(bible_docs, new_text)

    # 合并结果
    all_issues = regex_result["issues"] + llm_result["issues"]
    # 去重（LLM 和正则可能报告同一矛盾）
    all_issues = list(dict.fromkeys(all_issues))

    return {
        "consistent": len(all_issues) == 0,
        "issues": all_issues,
        "checked_pairs": regex_result["checked_pairs"] + llm_result["checked_pairs"],
    }
