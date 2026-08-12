"""安全只读调查工具集 - 供主动闭环决策器「接地」用。

核心问题：之前的 decide_close_loop 只看对话文本（user_msg + asst_msg），
在「猜」有没有关联未竟之事，而不是「看」到未竟之事。

本模块提供一组严格只读的调查工具，让 LLM 在做闭环决策前能先调查真实环境：
- read_file: 读文件内容（限 workspace 内，有大小上限）
- list_dir: 列目录内容
- search_files: 在文件内容中搜索关键词
- git_status: 查看 git 工作区状态
- read_memory: 读取相关记忆

所有工具都是**只读**的，绝不修改任何文件。所有路径工具都限制在 workspace
范围内，防止路径穿越。无 workspace 时工具返回空结果但不报错。

工具调用由 LLM 通过 function calling 驱动，调查循环最多 N 轮，防止无限调用。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

# 单次 read_file 最大字符数（避免把大文件整个塞进 LLM 上下文）
_MAX_FILE_CHARS = 8000
# list_dir 最大返回条目数
_MAX_DIR_ENTRIES = 50
# search_files 最大匹配行数
_MAX_SEARCH_MATCHES = 20
# 调查循环最大轮次（防止 LLM 无限调用工具；默认 3 轮即够多步调查又不滥烧 token）
_MAX_INVESTIGATION_ROUNDS = 3

# search_files 跳过的重型目录（避免大仓库 rglob 全树枚举卡死）
_SEARCH_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "build", "dist",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".tox",
    "surfaces", "target", ".idea", ".vscode", "site-packages",
}
# search_files 最大递归深度
_MAX_SEARCH_DEPTH = 4


def _safe_resolve(
    path_str: str,
    workspace: Optional[str],
    base_dir: Optional[str] = None,
) -> Optional[Path]:
    """安全解析路径：必须在 workspace（优先）或 base_dir/家目录内，防止路径穿越。

    相比原版收紧了「无 workspace」分支：原先允许任何存在的绝对路径（含
    /etc/passwd、~/.ssh 等），现在无 workspace 时只允许 base_dir 或家目录内的路径。
    """
    if not path_str:
        return None
    p = Path(path_str)
    if not p.is_absolute():
        # 相对路径：基于 workspace（优先）或 base_dir
        root = Path(workspace).resolve() if workspace else (
            Path(base_dir).resolve() if base_dir else None
        )
        if root is None:
            return None
        p = (root / p).resolve()
    else:
        p = p.resolve()
        # 绝对路径：必须在 workspace 内；无 workspace 时限制在 base_dir/家目录内
        if workspace:
            ws = Path(workspace).resolve()
            try:
                p.relative_to(ws)
            except ValueError:
                return None
        else:
            root = Path(base_dir).resolve() if base_dir else Path.home().resolve()
            try:
                p.relative_to(root)
            except ValueError:
                return None
    return p


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（已截断）"


# -----------------------------------------------------------------------
# 工具实现
# -----------------------------------------------------------------------

def tool_read_file(path: str, workspace: Optional[str] = None) -> str:
    """读取文件内容（限 workspace 内，最大 8000 字符）。"""
    resolved = _safe_resolve(path, workspace)
    if resolved is None or not resolved.is_file():
        return f"错误：文件不存在或不在工作目录内：{path}"
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
        return _truncate(text, _MAX_FILE_CHARS)
    except OSError as e:
        return f"读取失败：{e}"


def tool_list_dir(path: str = ".", workspace: Optional[str] = None) -> str:
    """列出目录内容（限 workspace 内，最多 50 条）。"""
    resolved = _safe_resolve(path, workspace)
    if resolved is None or not resolved.is_dir():
        return f"错误：目录不存在或不在工作目录内：{path}"
    entries = []
    for child in sorted(resolved.iterdir()):
        if len(entries) >= _MAX_DIR_ENTRIES:
            entries.append("…（更多条目已省略）")
            break
        tag = "/" if child.is_dir() else ""
        entries.append(f"{child.name}{tag}")
    return "\n".join(entries) if entries else "（空目录）"


def tool_search_files(
    pattern: str, path: str = ".", workspace: Optional[str] = None
) -> str:
    """在目录下搜索文件内容（grep 风格，限 workspace 内，最多 20 条匹配）。

    性能保护：限制递归深度并跳过重型目录（.git/node_modules/.venv/dist/surfaces…），
    避免在大型代码仓库里 rglob 全树枚举导致卡死或内存暴涨。
    """
    resolved = _safe_resolve(path, workspace)
    if resolved is None or not resolved.is_dir():
        return f"错误：目录不存在或不在工作目录内：{path}"
    matches: list[str] = []
    stack: list[tuple[Path, int]] = [(resolved, 0)]
    try:
        while stack:
            cur, depth = stack.pop()
            if not cur.is_dir() or depth > _MAX_SEARCH_DEPTH:
                continue
            try:
                children = sorted(cur.iterdir())
            except OSError:
                continue
            for child in children:
                if len(matches) >= _MAX_SEARCH_MATCHES:
                    matches.append("…（更多匹配已省略）")
                    return "\n".join(matches)
                if child.is_dir():
                    if child.name in _SEARCH_SKIP_DIRS:
                        continue
                    stack.append((child, depth + 1))
                elif child.is_file():
                    try:
                        size = child.stat().st_size
                    except OSError:
                        continue
                    if size > 500_000:
                        continue
                    try:
                        text = child.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    for i, line in enumerate(text.splitlines(), 1):
                        if pattern.lower() in line.lower():
                            rel = child.relative_to(resolved)
                            matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                            if len(matches) >= _MAX_SEARCH_MATCHES:
                                break
    except OSError as e:
        return f"搜索失败：{e}"
    return "\n".join(matches) if matches else "（无匹配）"


def tool_git_status(workspace: Optional[str] = None) -> str:
    """查看 git 工作区状态（限 workspace 内）。"""
    if not workspace:
        return "（无工作目录，跳过 git status）"
    ws = Path(workspace).resolve()
    if not (ws / ".git").exists():
        return "（不是 git 仓库）"
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(ws),
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = result.stdout.strip()
        return _truncate(output, 2000) if output else "（工作区干净）"
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"git status 失败：{e}"


def _split_md_sections(text: str) -> list[str]:
    """按二级标题（## ）切分为分节，便于按关键词过滤相关记忆。"""
    sections: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if re.match(r"^##\s+", line):
            if current:
                sections.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current).strip())
    return [s for s in sections if s]


def tool_read_memory(
    query: str = "", base_dir: Optional[str] = None, workspace: Optional[str] = None
) -> str:
    """读取相关记忆（全局 MEMORY.md + USER.md + 当前项目 longterm.md），按 query 过滤。

    对齐 Hermes 的记忆注入：不是全量灌入，而是按相关性截取。分节过滤用真实
    MEMORY.md 的 `## 标题` 格式（而非测试用的 § 占位符），并纳入当前项目记忆，
    与 after_action 的 _load_close_loop_memory 同口径。
    """
    parts: list[str] = []
    base = Path(base_dir) if base_dir else Path(
        os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
    ).resolve()

    # 1. 全局记忆 MEMORY.md
    memory_path = base / "MEMORY.md"
    if memory_path.is_file():
        text = memory_path.read_text(encoding="utf-8", errors="replace")
        if query:
            relevant = [s for s in _split_md_sections(text) if query.lower() in s.lower()]
            if relevant:
                parts.append("## 全局记忆（按关键词过滤）\n" + "\n\n".join(relevant[:5]))
        else:
            parts.append("## 全局记忆\n" + _truncate(text, 2000))

    # 2. USER.md（用户画像）
    user_path = base / "USER.md"
    if user_path.is_file():
        parts.append("## 用户画像\n" + _truncate(
            user_path.read_text(encoding="utf-8", errors="replace"), 1000))

    # 3. 当前项目 longterm.md（与 _load_close_loop_memory 同口径）
    if workspace:
        proj_longterm = base / "projects" / Path(workspace).name / "longterm.md"
        if proj_longterm.is_file():
            parts.append("## 项目记忆\n" + _truncate(
                proj_longterm.read_text(encoding="utf-8", errors="replace"), 1500))

    if not parts:
        return "（无可用记忆）"
    return "\n\n---\n\n".join(parts)


# -----------------------------------------------------------------------
# 工具注册表（供 LLM function calling 使用）
# -----------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件内容（限工作目录内）。用于确认文件实际内容、发现不一致。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径（相对路径基于工作目录，或绝对路径）",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录内容。用于了解项目结构、发现需要同步的文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目录路径（默认为工作目录根）",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "在目录下搜索文件内容（关键词匹配）。用于发现关联文件或不一致。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "搜索关键词",
                    },
                    "path": {
                        "type": "string",
                        "description": "搜索目录（默认为工作目录根）",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "查看 git 工作区状态。用于发现未提交的改动、未推送的提交。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_memory",
            "description": "读取相关记忆（用户偏好、历史纠正、项目约定）。用于判断用户是否在意这件事。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "查询关键词（如'deployment'、'同步'、用户偏好主题）",
                    }
                },
                "required": [],
            },
        },
    },
]

# 工具名 -> 实现函数的映射
TOOL_IMPLEMENTATIONS: dict[str, Any] = {
    "read_file": tool_read_file,
    "list_dir": tool_list_dir,
    "search_files": tool_search_files,
    "git_status": tool_git_status,
    "read_memory": tool_read_memory,
}
