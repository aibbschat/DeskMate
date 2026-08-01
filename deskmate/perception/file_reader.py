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


def read_file_safe(path: str, max_chars: int = MAX_READ_SIZE) -> str:
    """安全读取文件内容。

    - 只读取文本文件（按扩展名判断）
    - 超过 max_chars 截断
    - 跳过二进制文件
    - 返回内容或错误信息
    """
    p = Path(path).expanduser().resolve()

    if not p.exists():
        return f"（文件不存在：{path}）"
    if not p.is_file():
        return f"（不是文件：{path}）"

    # 扩展名检查
    ext = p.suffix.lower()
    name = p.name.lower()

    # .docx 文件用 python-docx 解析
    if ext in _DOCX_EXTS:
        return _read_docx(p, max_chars)

    # 允许无扩展名的常见文本文件
    allow_no_ext = name in {"makefile", "dockerfile", "readme", "license", "changelog"}
    if ext not in _TEXT_EXTS and not allow_no_ext:
        return f"（不支持读取 {ext or '无扩展名'} 文件，仅支持文本文件）"

    # 大小检查
    try:
        size = p.stat().st_size
    except OSError:
        return f"（无法读取文件信息：{path}）"

    if size > max_chars * 2:
        return f"（文件过大：{size} 字节，超过限制 {max_chars * 2}）"

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"（读取失败：{e}）"

    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n…（截断，共 {len(text)} 字符）"
    return text


def _read_docx(p: Path, max_chars: int) -> str:
    """读取 .docx 文件内容。"""
    try:
        from docx import Document
    except ImportError:
        return "（python-docx 未安装，无法读取 .docx 文件）"

    try:
        doc = Document(str(p))
    except Exception as e:
        return f"（读取 .docx 失败：{e}）"

    paragraphs = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            paragraphs.append(text)

    # 也读取表格中的文本
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                text = cell.text.strip()
                if text:
                    paragraphs.append(text)

    content = "\n".join(paragraphs)
    if len(content) > max_chars:
        return content[:max_chars] + f"\n\n…（截断，共 {len(content)} 字符）"
    return content


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
