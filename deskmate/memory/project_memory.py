"""项目级记忆管理 — 每个项目独立的记忆目录。"""

from __future__ import annotations

from pathlib import Path


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


def format_project_memory(base_dir: Path, project_name: str) -> str:
    """将项目记忆格式化为可注入 system prompt 的文本。"""
    memories = load_project_memory(base_dir, project_name)
    if not memories:
        return ""
    parts = [f"# 项目记忆：{project_name}"]
    for fname, content in memories.items():
        tag = fname.replace(".md", "")
        parts.append(f"\n## {tag}\n{content}")
    return "\n".join(parts)
