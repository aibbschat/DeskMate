"""安全文件读取 — 读取文件内容，限制大小和格式。支持 .txt/.md/.docx 等。"""

from __future__ import annotations

import re
from pathlib import Path

# 允许读取的文本文件扩展名
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".text", ".rst",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java", ".c", ".cpp", ".h",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".html", ".css", ".scss", ".less",
    ".sh", ".bash", ".zsh", ".fish",
    ".sql", ".csv", ".tsv",
    ".xml", ".svg", ".env",
    ".gitignore", ".dockerignore", ".editorconfig",
    ".novel",  # 小说文本
}

# Word 文档扩展名（需要 python-docx 解析）
_DOCX_EXTS = {".docx"}

# 最大读取大小（100KB）
MAX_READ_SIZE = 100_000

# 长稿定位类读取（read_chapter / list_chapters）的抽取大小上限。
# 它们最终只输出一章 / 一个目录（输出天然受限，不会爆上下文），
# 抽取全文只是为了切章定位——因此放宽到 64MB，容纳百万字级长篇小说。
# 窗口模式 read_file_safe 仍保持 2MB（MAX_READ_SIZE*20）对话安全默认。
_LONG_TEXT_BYTES = 64 * 1024 * 1024

# 单次读取的默认返回上限。默认值刻意远小于 MAX_READ_SIZE：一次吞下整本长稿会把
# 上下文顶过压缩触发线，压缩器随即清掉工具返回，模型「读完就忘」进而编造情节。
# 借鉴 Hermes（tools/file_tools.py:745 read_file_tool 默认 limit=500 行）的思路 —
# 默认就少读，需要更多由调用方显式索取。
# 20K 字符 ≈ 18K tokens，够读两三个正常章节；整本长稿必然超限，从而被导向按章读取。
DEFAULT_READ_CHARS = 20_000

# 章节标题识别：第X章/回/节（可带标题）、楔子、序章、尾声、番外、后记。
_CHAPTER_RE = re.compile(
    r"^[\s\u3000]*("
    r"第[〇零一二三四五六七八九十百千两0-9]{1,12}[章回节][^\n]{0,40}"
    r"|(?:楔子|序章|序言|自序|引子|尾声|终章|番外|后记|附录)[^\n]{0,40}"
    r")[\s\u3000]*$",
    re.MULTILINE,
)


def read_file_safe(
    path: str,
    max_chars: int = DEFAULT_READ_CHARS,
    offset: int = 0,
) -> str:
    """安全读取文件内容。

    - 只读取文本文件（按扩展名判断），.docx 经 python-docx 抽取正文
    - 超过 max_chars **不再静默截断**：返回明确的超限说明 + 总字数 + 后续动作建议
    - offset 支持从指定字符位置续读，配合超限提示实现分页

    为什么改成「拒绝」而不是「截断」：静默截断会让模型拿到一段看起来完整的开头，
    它据此宣称「全读完了」，再用记忆补齐没读到的部分——这正是编造人物的成因。
    明确报错并告知真实规模，模型才会去分章/分页读。
    """
    p = Path(path).expanduser().resolve()

    if not p.exists():
        return f"（文件不存在：{path}）"
    if not p.is_file():
        return f"（不是文件：{path}）"

    text = _extract_text(p)
    if text is None:
        ext = p.suffix.lower()
        return f"（不支持读取 {ext or '无扩展名'} 文件，仅支持文本文件与 .docx）"
    if text.startswith("（") and text.endswith("）") and "\n" not in text:
        return text  # 抽取阶段的错误信息，原样返回

    total = len(text)
    offset = max(0, int(offset or 0))
    if offset >= total and total:
        return f"（起始位置 {offset} 超过文件总长 {total} 字符）"

    window = text[offset : offset + max_chars]
    remaining = total - (offset + len(window))

    if remaining <= 0:
        return window

    # 超出窗口——如实告知，并给出章节目录，让下一步是「读某一章」而不是「继续瞎猜」。
    # 提示放在正文之前：模型读到内容时已经知道这不是全文。
    chapters = split_chapters(text)
    if len(chapters) > 1:
        toc = "；".join(f"{i}.{t}（{len(b):,}字）" for i, (t, b) in enumerate(chapters, 1))
        nav = f"\n本稿分 {len(chapters)} 节：{toc}\n建议改用 read_chapter 只读需要的一节。"
    else:
        nav = ""
    return (
        f"⚠️ 未读完：本次仅返回第 {offset:,}–{offset + len(window):,} 字符，"
        f"全文共 {total:,} 字符，还有 {remaining:,} 字符未读。\n"
        f"不得据此声称「读完了」，也不得推测未读部分的内容。"
        f"继续顺读用 offset={offset + len(window)}。{nav}\n\n"
        f"—— 以下为本次读到的片段 ——\n{window}"
    )


def read_full_text(path: str, max_bytes: int | None = None) -> str:
    """读取文件全文（不截断、不做 max_chars 窗口），供需要整本处理的上游使用。

    与 read_file_safe 的区别：read_file_safe 默认只回一个窗口并如实告知超限；
    read_full_text 一次性返回全文，适合「建索引 / 生成圣经」这类必须看全局的场景。
    仍保留类型校验；大小上限默认沿用 MAX_READ_SIZE*20（2MB，对话安全默认），
    批处理场景（如建原著索引）可显式传更大 max_bytes——那是离线一次性成本，不占上下文。
    错误情况返回 （...） 包裹的说明文本，调用方按「以（ 开头且无换行」判定失败。
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return f"（文件不存在：{path}）"
    if not p.is_file():
        return f"（不是文件：{path}）"
    text = _extract_text(p, max_bytes=max_bytes)
    if text is None:
        ext = p.suffix.lower()
        return f"（不支持读取 {ext or '无扩展名'} 文件，仅支持文本文件与 .docx）"
    return text


def _extract_text(p: Path, max_bytes: int | None = None) -> str | None:
    """把文件抽成纯文本全文（不截断）。不支持的类型返回 None。"""
    ext = p.suffix.lower()
    name = p.name.lower()

    if ext in _DOCX_EXTS:
        return _extract_docx(p)

    allow_no_ext = name in {"makefile", "dockerfile", "readme", "license", "changelog"}
    if ext not in _TEXT_EXTS and not allow_no_ext:
        return None

    try:
        limit = max_bytes or (MAX_READ_SIZE * 20)
        if p.stat().st_size > limit:
            return f"（文件过大：{p.stat().st_size} 字节，超过上限 {limit}，请改用支持分段的工具）"
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"（读取失败：{e}）"


def extract_docx_text(path) -> str:
    """公开入口：抽取 .docx 正文（段落 + 表格），不截断。

    给 agent 的 `read_file`（coworker/tools/files.py）复用——那边按文本行读文件，
    .docx 是 ZIP，直接读只会拿到 `PK\\x03\\x04` 开头的二进制，worker 据此断定
    「这个环境读不了正文」。抽取逻辑只留这一份，避免两处漂移。
    """
    return _extract_docx(Path(path).expanduser().resolve())


def _extract_docx(p: Path) -> str:
    """抽取 .docx 全文（段落 + 表格），不截断。"""
    try:
        from docx import Document
    except ImportError:
        return "（python-docx 未安装，无法读取 .docx 文件）"

    try:
        doc = Document(str(p))
    except Exception as e:
        return f"（读取 .docx 失败：{e}）"

    paragraphs = [t for para in doc.paragraphs if (t := para.text.strip())]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if t := cell.text.strip():
                    paragraphs.append(t)
    return "\n".join(paragraphs)




def split_chapters(text: str) -> list[tuple[str, str]]:
    """按章节标题把正文切成 [(标题, 正文), ...]。

    识别「第X章/回/节」「楔子」「尾声」「番外」等常见中文小说体例。
    标题前的内容归入伪章节「（卷首）」，没有任何标题时返回单条「（全文）」。
    """
    if not text:
        return []
    marks = [(m.start(), m.group(1).strip()) for m in _CHAPTER_RE.finditer(text)]
    if not marks:
        return [("（全文）", text)]

    out: list[tuple[str, str]] = []
    head = text[: marks[0][0]].strip()
    if head:
        out.append(("（卷首）", head))
    for i, (pos, title) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.append((title, text[pos:end].strip()))
    return out


def list_chapters(path: str, max_bytes: int | None = None) -> str:
    """列出稿件的章节目录与各章字数——先看目录，再决定读哪一章。"""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        return f"（文件不存在：{path}）"
    text = _extract_text(p, max_bytes=max_bytes or _LONG_TEXT_BYTES)
    if text is None:
        return f"（不支持的文件类型：{p.suffix or '无扩展名'}）"
    if text.startswith("（") and "\n" not in text:
        return text

    chapters = split_chapters(text)
    if not chapters:
        return "（文件为空）"
    lines = [f"《{p.stem}》共 {len(chapters)} 节，全文 {len(text):,} 字："]
    lines += [f"  {i}. {title}（{len(body):,} 字）" for i, (title, body) in enumerate(chapters, 1)]
    lines.append("用 read_chapter(path, chapter) 读取其中一节，chapter 可填序号或标题。")
    return "\n".join(lines)


def read_chapter(path: str, chapter: str | int, max_chars: int = MAX_READ_SIZE, max_bytes: int | None = None) -> str:
    """只读取指定的一章——与全书长度无关，因此长稿也不会顶爆上下文。

    chapter 可以是序号（1 起）、完整标题，或标题片段（如「第二章」「财当道」）。
    抽取全文用于切章定位，大小上限放宽到 _LONG_TEXT_BYTES（64MB，容纳长篇）。
    """
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        return f"（文件不存在：{path}）"
    text = _extract_text(p, max_bytes=max_bytes or _LONG_TEXT_BYTES)
    if text is None:
        return f"（不支持的文件类型：{p.suffix or '无扩展名'}）"
    if text.startswith("（") and "\n" not in text:
        return text

    chapters = split_chapters(text)
    if not chapters:
        return "（文件为空）"

    picked: tuple[str, str] | None = None
    key = str(chapter).strip()
    if key.isdigit() and 1 <= int(key) <= len(chapters):
        picked = chapters[int(key) - 1]
    else:
        norm = key.replace(" ", "").replace("\u3000", "")
        for title, body in chapters:
            if norm and norm in title.replace(" ", "").replace("\u3000", ""):
                picked = (title, body)
                break

    if picked is None:
        titles = "、".join(t for t, _ in chapters[:20])
        return f"（未找到「{chapter}」。本稿章节为：{titles}）"

    title, body = picked
    if len(body) > max_chars:
        return (
            f"【{title}】（本章 {len(body):,} 字，超过单次上限 {max_chars:,}，以下为前半部分，"
            f"勿据此声称读完本章）\n\n{body[:max_chars]}"
        )
    return f"【{title}】（{len(body):,} 字，本章完整）\n\n{body}"


def extract_file_path_from_title(title: str, app: str) -> str | None:
    """从窗口标题中提取文件路径。

    支持常见格式：
    - Microsoft Word: "filename.docx" 或 "filename.docx - Microsoft Word" 或 "filename — Word"
    - Pages: "filename.pages"
    - VS Code: "filename — Folder" 或 "filename - VSCode"
    - Sublime Text: "filename — /path/to/folder"
    - TextEdit: "filename"
    """
    if not title:
        return None

    # Word: 标题格式通常是 "filename.docx" 或 "filename.docx - Word" 或 "filename — Word"
    # 也可能带路径: "/path/to/file.docx — Word"
    # macOS Word 有时省略扩展名: "filename — Word"
    word_patterns = [
        r"^(.+?\.(?:docx?|doc))\s*[-—–]\s*(?:Microsoft\s+)?Word\b",
        r"^(.+?\.(?:docx?|doc))\s*$",
        # 无扩展名 + Word 应用（macOS Word 常省略 .docx）
        r"^(.+?)\s*[-—–]\s*(?:Microsoft\s+)?Word\b",
    ]
    is_word = app.lower() in ("microsoft word", "word")
    for i, pat in enumerate(word_patterns):
        m = re.match(pat, title, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip()
            # 无扩展名的模式（第3个）只在 app 确实是 Word 时才用
            if i == 2 and not is_word:
                continue
            # 如果看起来像完整路径，直接返回
            if candidate.startswith("/"):
                return candidate
            # 无扩展名时尝试 .docx
            result = _search_file_in_common_dirs(candidate, app)
            if not result and "." not in candidate:
                result = _search_file_in_common_dirs(candidate + ".docx", app)
            if result:
                return result

    # Word 窗口标题只有文件名（无扩展名、无"— Word"后缀）
    # 例如: "国子监秘闻录"（app=Microsoft Word）
    if is_word and title and "." not in title:
        result = _search_file_in_common_dirs(title + ".docx", app)
        if result:
            return result

    # Pages: "filename.pages - Pages" 或 "filename.pages"
    pages_pat = r"^(.+?\.pages)\s*[-—–]\s*Pages\b"
    m = re.match(pages_pat, title, re.IGNORECASE)
    if m:
        candidate = m.group(1).strip()
        if candidate.startswith("/"):
            return candidate
        return _search_file_in_common_dirs(candidate, app)

    # VS Code / Sublime: "filename — /path/to/folder" 或 "filename - /path/to/folder"
    editor_pat = r"^(.+?)\s*[-—–]\s*(/.+)$"
    m = re.match(editor_pat, title)
    if m:
        filename = m.group(1).strip()
        folder = m.group(2).strip()
        candidate = Path(folder) / filename
        if candidate.exists():
            return str(candidate)

    # TextEdit: "filename.ext" （无后缀标识，靠 app 名判断）
    if app.lower() in ("textedit", "备忘录", "notes"):
        ext_match = re.search(r"\.\w+$", title)
        if ext_match:
            return _search_file_in_common_dirs(title.strip(), app)

    return None


def _search_file_in_common_dirs(filename: str, app: str) -> str | None:
    """在常见目录中搜索文件。优先用 macOS Spotlight 快速定位。"""
    # 1. macOS Spotlight 搜索（最快，支持子目录）
    try:
        import subprocess
        # 先搜完整文件名，再搜不带扩展名的（Spotlight 有时不匹配扩展名）
        queries = [filename]
        stem = Path(filename).stem
        if stem != filename:
            queries.append(stem)
        for query in queries:
            result = subprocess.run(
                ["mdfind", "-name", query],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    line = line.strip()
                    if line and Path(line).name == filename and Path(line).exists():
                        return line
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # 2. 回退：直接在常见目录查找（不含子目录）
    search_dirs = [
        Path.home() / "Desktop",
        Path.home() / "Documents",
        Path.home() / "Downloads",
    ]
    for d in search_dirs:
        candidate = d / filename
        if candidate.exists():
            return str(candidate)
    return None


def list_dir(path: str, max_items: int = 100) -> str:
    """列出目录内容（不递归），返回格式化的文件列表。"""
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        return f"（不是目录：{path}）"

    items: list[str] = []
    try:
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except OSError as e:
        return f"（读取目录失败：{e}）"

    for entry in entries[:max_items]:
        if entry.is_dir():
            items.append(f"  📁 {entry.name}/")
        else:
            try:
                size = entry.stat().st_size
                size_str = _human_size(size)
            except OSError:
                size_str = "?"
            items.append(f"  📄 {entry.name}  ({size_str})")

    result = f"{p}/\n" + "\n".join(items)
    if len(entries) > max_items:
        result += f"\n  …（共 {len(entries)} 项，显示前 {max_items}）"
    return result


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"
