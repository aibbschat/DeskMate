"""Skill loading — Anthropic SKILL.md format with progressive disclosure.

A skill is a folder containing `SKILL.md` (YAML frontmatter: name, description,
optional allowed-tools) + a markdown body of instructions + optional resources/scripts.

Progressive disclosure: at session start only the catalog (name + description) is injected
into the agent's context; the full body is loaded on demand via the `load_skill` tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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
        self._skills: dict[str, Skill] = {}
        for directory in dirs:
            self._discover(Path(directory))

    def _discover(self, directory: Path) -> None:
        if not directory.is_dir():
            return
        for sub in sorted(directory.iterdir()):
            md = sub / "SKILL.md"
            if md.is_file():
                skill = _parse_skill(md)
                self._skills[skill.name] = skill

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


def skill_catalog_text(loader: SkillLoader) -> str:
    catalog = loader.catalog()
    if not catalog:
        return ""
    lines = [f"- {c['name']}: {c['description']}" for c in catalog]
    return (
        "Available skills — call load_skill(name) to load one's full instructions when "
        "it's relevant to the task:\n" + "\n".join(lines)
    )


def skill_tools(loader: SkillLoader) -> list:
    def load_skill(name: str) -> dict:
        """Load a skill's full instructions + resources path by name. Call this when a
        skill from the catalog is relevant to the current task."""
        skill = loader.get(name)
        if skill is None:
            return {"error": f"unknown skill: {name}", "available": loader.names()}
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
