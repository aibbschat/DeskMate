"""Governance Tool Guard — regex-signature detection for shell commands.

A lightweight port of AgentScope QwenPaw's ``RuleBasedToolGuardian``. It scans a
proposed ``run_shell`` command against a YAML rule set and returns ``Finding``
objects (one per matched rule) carrying a severity. The
:class:`~coworker.permissions.PermissionEngine` consults these findings: a
``CRITICAL`` finding denies the call outright, ``HIGH``/``MEDIUM`` force an
approval prompt, and ``strict`` execution level promotes any finding to an
approval.

This is the *policy* layer. The *kernel* layer (OS + secret confinement) lives
in :mod:`coworker.sandbox`. Together they stop both catastrophic commands
(``rm -rf /``, reverse shells) and accidental OS corruption.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:  # PyYAML is a hard dependency of the app, but guard for isolated tests.
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


@dataclass
class Finding:
    rule_id: str
    category: str
    severity: str  # CRITICAL | HIGH | MEDIUM | LOW | INFO
    description: str
    matched: str = ""


_SEVERITY_RANK = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def _rank(sev: str) -> int:
    return _SEVERITY_RANK.get(sev.upper(), 1)


@dataclass
class Rule:
    rule_id: str
    tools: list[str]
    params: list[str]
    category: str
    severity: str
    patterns: list[str]
    exclude_patterns: list[str]
    description: str
    remediation: str
    _compiled: list[re.Pattern[str]] = None  # type: ignore[assignment]
    _exclude: list[re.Pattern[str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._compiled = [re.compile(p) for p in self.patterns]
        self._exclude = [re.compile(p) for p in self.exclude_patterns]

    def matches(self, command: str) -> Optional[str]:
        if any(rx.search(command) for rx in self._exclude):
            return None
        for rx in self._compiled:
            m = rx.search(command)
            if m:
                return m.group(0)
        return None


def _default_rules_path() -> Path:
    return Path(__file__).resolve().parent / "rules" / "dangerous_shell_commands.yaml"


class RuleGuardian:
    """Loads shell-command rules and scans commands for dangerous patterns."""

    def __init__(self, rules_path: Optional[Path | str] = None) -> None:
        self._rules: list[Rule] = []
        self._load(rules_path or _default_rules_path())

    def _load(self, path: Path | str) -> None:
        if yaml is None:  # pragma: no cover
            raise RuntimeError("PyYAML is required for the governance Tool Guard")
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"tool guard rules not found: {p}")
        with open(p, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or []
        for item in raw:
            self._rules.append(
                Rule(
                    rule_id=item.get("id", "UNKNOWN"),
                    tools=item.get("tools", ["run_shell"]),
                    params=item.get("params", ["command"]),
                    category=item.get("category", "unknown"),
                    severity=str(item.get("severity", "MEDIUM")).upper(),
                    patterns=list(item.get("patterns", [])),
                    exclude_patterns=list(item.get("exclude_patterns", [])),
                    description=item.get("description", ""),
                    remediation=item.get("remediation", ""),
                )
            )

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def scan(self, tool_name: str, arguments: dict) -> list[Finding]:
        """Return findings for a tool call, empty list when nothing matches."""
        command = str((arguments or {}).get("command", "") or "")
        if not command or tool_name != "run_shell":
            return []
        findings: list[Finding] = []
        for rule in self._rules:
            if tool_name not in rule.tools:
                continue
            matched = rule.matches(command)
            if matched:
                findings.append(
                    Finding(
                        rule_id=rule.rule_id,
                        category=rule.category,
                        severity=rule.severity,
                        description=rule.description,
                        matched=matched,
                    )
                )
        # Highest severity first so the engine can react to the worst match.
        findings.sort(key=lambda f: _rank(f.severity), reverse=True)
        return findings

    def worst_severity(self, findings: list[Finding]) -> Optional[str]:
        if not findings:
            return None
        return max(findings, key=lambda f: _rank(f.severity)).severity
