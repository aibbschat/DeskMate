"""项目级记忆管理 — 每个项目独立的记忆目录。

分层注入（对齐 Hermes 记忆漏斗）：``longterm.md`` 已蒸馏长期记忆完整常驻注入；
``journal.md`` 情景流水只注入最近 N 条窗口，完整日志留在磁盘按需检索；
整体有总字符上限兜底，prompt 成本不随历史线性膨胀。
"""

from __future__ import annotations

import re
from pathlib import Path

# 注入时 journal 保留的最近条数（窗口外完整日志留磁盘按需检索）。
_JOURNAL_RECENT_ENTRIES = 15
# 项目记忆注入总字符上限兜底（杜绝历史累积把 prompt 撑爆）。
_INJECT_MAX_CHARS = 8_000


def get_project_dir(base_dir: Path, project_name: str) -> Path:
    """获取项目记忆目录。"""
    return base_dir / "projects" / project_name


def list_projects(base_dir: Path) -> list[str]:
    """列出所有有记忆的项目。"""
    projects_dir = base_dir / "projects"
    if not projects_dir.is_dir():
        return []
    return sorted(
        d.name for d in projects_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )


def load_project_memory(base_dir: Path, project_name: str) -> dict[str, str]:
    """加载项目的全部记忆文件。返回 {文件名: 内容}。"""
    proj_dir = get_project_dir(base_dir, project_name)
    if not proj_dir.is_dir():
        return {}
    result: dict[str, str] = {}
    for f in sorted(proj_dir.glob("*.md")):
        try:
            result[f.name] = f.read_text(encoding="utf-8")
        except OSError:
            continue
    return result


def read_project_file(base_dir: Path, project_name: str, filename: str) -> str:
    """读取项目记忆中的单个文件。"""
    p = get_project_dir(base_dir, project_name) / filename
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_project_file(base_dir: Path, project_name: str, filename: str, content: str) -> None:
    """写入项目记忆文件。"""
    proj_dir = get_project_dir(base_dir, project_name)
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / filename).write_text(content, encoding="utf-8")


def delete_project_file(base_dir: Path, project_name: str, filename: str) -> bool:
    """删除项目记忆文件。"""
    p = get_project_dir(base_dir, project_name) / filename
    if p.is_file():
        p.unlink()
        return True
    return False


def _split_entries(content: str) -> list[str]:
    """按日志分隔符切分条目；parts[0] 为头部，其余为各条。"""
    return re.split(r"\n\n---\n\n", content)


def _recent_journal_window(content: str, max_entries: int) -> str:
    """截取 journal 最近 max_entries 条，作为注入的「近期动态」窗口。

    完整日志仍留在磁盘（rolling 缓冲），按需经 ``manager._search_project_memory``
    检索，不每次全量注入（对齐 Hermes L2：情景层按需检索而非全量注入）。
    """
    if not content:
        return ""
    parts = _split_entries(content)
    if len(parts) <= 1:
        return content  # 单条（无分隔符），原样返回
    header, entries = parts[0], parts[1:]
    recent = entries[-max_entries:]
    return header + "\n\n---\n\n" + "\n\n---\n\n".join(recent)


def format_project_memory(base_dir: Path, project_name: str) -> str:
    """将项目记忆格式化为可注入 system prompt 的文本。

    分层（对齐 Hermes 记忆漏斗）：
    - longterm.md：已蒸馏长期记忆（有界热记忆），**完整常驻注入**。
    - journal.md：情景流水，仅注入**最近 N 条窗口**，完整日志按需检索。
    - 其余项目 .md：体量小，直接注入。
    整体有总字符上限兜底，保证 prompt 成本不随历史线性膨胀。
    """
    memories = load_project_memory(base_dir, project_name)
    if not memories:
        return ""
    longterm = memories.pop("longterm.md", None)
    journal = memories.pop("journal.md", None)
    parts = [f"# 项目记忆：{project_name}"]
    if longterm:
        parts.append("## 长期记忆（已蒸馏，常驻）\n" + longterm)
    if journal:
        window = _recent_journal_window(journal, _JOURNAL_RECENT_ENTRIES)
        parts.append(
            f"## 近期动态（最近 {_JOURNAL_RECENT_ENTRIES} 条，完整日志按需检索）\n"
            + window
        )
    for fname in sorted(memories):
        tag = fname.replace(".md", "")
        parts.append(f"## {tag}\n{memories[fname]}")
    text = "\n\n---\n\n".join(parts)
    if len(text) > _INJECT_MAX_CHARS:
        text = text[:_INJECT_MAX_CHARS] + "\n\n…（项目记忆已截断）…"
    return text
