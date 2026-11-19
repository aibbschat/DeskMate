"""Skill loading — Anthropic SKILL.md format with progressive disclosure.

A skill is a folder containing `SKILL.md` (YAML frontmatter: name, description,
optional allowed-tools) + a markdown body of instructions + optional resources/scripts.

Progressive disclosure: at session start only the catalog (name + description) is injected
into the agent's context; the full body is loaded on demand via the `load_skill` tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Union

import aisuite as ai


@dataclass
class Skill:
    name: str
    description: str
    instructions: str = ""  # full body — loaded on demand
    path: Optional[str] = None
    allowed_tools: list[str] = field(default_factory=list)


class SkillLoader:
    def __init__(self, dirs: list[str | Path]) -> None:
        self._dirs = [Path(d) for d in dirs]
        self._skills: dict[str, Skill] = {}
        # PR6：path → (指纹, 已解析对象)。按路径缓存，rescan 只重建 _skills 的
        # 名字索引，不重新读文件；_parsed 跨越多次 rescan 复用。
        self._parsed: dict[str, tuple[tuple[int, int], Skill]] = {}
        self.rescan()

    def rescan(self, *, force: bool = False) -> None:
        """Re-read the skill dirs. load_skill rescans on a miss so a skill created AFTER
        the session's engine was built is still loadable (the catalog line stays static
        until the next session, but an explicitly requested skill must not 404).

        2026-11-19 PR6：每轮 context_provider 都会调 rescan()；旧实现逐目录
        `read_text` 全部 SKILL.md（本机 43 个 ≈2.4ms 热缓存 / 冷盘更高）。
        改为按 (path, mtime_ns, size) 复用已解析对象——目录没变就零文件读取。
        `force=True` 仍全量重读，保持「新建技能立即可用」契约。
        """
        self._skills = {}
        for directory in self._dirs:
            self._discover(directory, force=force)

    def _discover(self, directory: Path, *, force: bool = False) -> None:
        if not directory.is_dir():
            return
        for sub in sorted(directory.iterdir()):
            md = sub / "SKILL.md"
            if not md.is_file():
                continue
            try:
                st = md.stat()
                fingerprint = (st.st_mtime_ns, st.st_size)
            except OSError:
                continue
            key = str(md)
            cached = self._parsed.get(key)
            if not force and cached is not None and cached[0] == fingerprint:
                self._skills[cached[1].name] = cached[1]
                continue
            try:
                skill = _parse_skill(md)
            except Exception:
                continue  # 与原行为一致：单个坏技能不拖垮会话
            self._skills[skill.name] = skill
            self._parsed[key] = (fingerprint, skill)

    def names(self) -> list[str]:
        return list(self._skills)

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def catalog(self) -> list[dict]:
        return [
            {"name": s.name, "description": s.description}
            for s in self._skills.values()
        ]


def _parse_skill(md: Path) -> Skill:
    text = md.read_text(encoding="utf-8")
    name, description, allowed, body = md.parent.name, "", [], text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            frontmatter = text[3:end]
            body = text[end + 4 :].lstrip("\n")
            fields: dict[str, str] = {}
            folded_key: str | None = None
            folded_lines: list[str] = []
            for line in frontmatter.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if folded_key and (line.startswith(" ") or line.startswith("\t")):
                    folded_lines.append(stripped)
                    continue
                if folded_key and folded_lines:
                    fields[folded_key] = " ".join(folded_lines)
                    folded_key = None
                    folded_lines = []
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key, value = key.strip().lower(), value.strip()
                if value in (">", ">-"):
                    folded_key = key
                    folded_lines = []
                    continue
                fields[key] = value
            if folded_key and folded_lines:
                fields[folded_key] = " ".join(folded_lines)
            name = fields.get("name") or name
            description = fields.get("description", "").strip("\"'")
            allowed = [
                t.strip()
                for t in fields.get("allowed-tools", fields.get("allowed_tools", "")).split(",")
                if t.strip()
            ]
    return Skill(
        name=name,
        description=description,
        instructions=body.strip(),
        path=str(md.parent),
        allowed_tools=allowed,
    )


def skill_catalog_text(
    loader: SkillLoader, allowed: Optional[set[str]] = None
) -> str:
    catalog = [
        c for c in loader.catalog() if allowed is None or c["name"] in allowed
    ]
    if not catalog:
        return ""
    lines = [f"- {c['name']}: {c['description']}" for c in catalog]
    # 2026-11-19 技能机制对齐 Hermes：弱建议式引导（"when relevant"）会让模型
    # 在长任务里跳过技能、自由发挥。改成强制 + 宁滥勿缺，与 Hermes 的
    # prompt_builder 「MUST load ... even if you think basic tools suffice」对齐。
    return (
        "## Skills\n"
        "Before replying, scan the skills below. If a skill matches or is even partially "
        "relevant to your task, you MUST load it with load_skill(name) and follow its "
        "instructions. Err on the side of loading — it is always better to have context "
        "you don't need than to miss critical steps, pitfalls, or established workflows. "
        "Skills encode the user's preferred approach, conventions, quality standards, and "
        "step-by-step processes — load them even if you think you could handle the task "
        "with basic tools, and even for tasks you already know how to do.\n"
        "Only proceed without loading a skill if genuinely none are relevant.\n"
        "\n<available_skills>\n" + "\n".join(lines) + "\n</available_skills>"
    )


AllowedSkills = Union[set, Callable[[], set], None]


def skill_tools(loader: SkillLoader, allowed: AllowedSkills = None) -> list:
    """`allowed` gates load_skill: a set is a build-time snapshot; a CALLABLE is consulted
    on every call — the manager passes one so Settings disables apply to live sessions
    immediately, and skills created after the engine was built are still loadable
    (loader rescans on a miss)."""

    def _allowed_now() -> Optional[set]:
        return allowed() if callable(allowed) else allowed

    def load_skill(name: str) -> dict:
        """Load a skill's full instructions + resources path by name. Call this when a
        skill from the catalog is relevant to the current task."""
        skill = loader.get(name)
        if skill is None:
            loader.rescan(force=True)  # created/changed after session start? pick it up now
            skill = loader.get(name)
        gate = _allowed_now()
        if skill is None or (gate is not None and name not in gate):
            available = sorted(
                n for n in loader.names() if gate is None or n in gate
            )
            return {"error": f"unknown skill: {name}", "available": available}
        return {
            "name": skill.name,
            "instructions": skill.instructions,
            "resources_path": skill.path,
        }

    return [
        ai.tool(
            load_skill,
            metadata=ai.ToolMetadata(
                category="skills", risk_level="low", capabilities=["load_skill"]
            ),
        )
    ]
