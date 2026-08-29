"""Memory settings — the on/off switch and the user's standing rules.

Settings-level state, deliberately outside the memory table (MEMORY-SPEC §2, §4.3, §6):

- ``enabled``: off means engines are built with no memory tools, no memories block, and
  no memory guidance. Existing memories are kept but inert. Read at build time; running
  sessions finish under the mode they started with.
- ``user_rules``: one text blob the user typed into Settings. Injected verbatim above
  auto memories; on conflict the rule wins. **The agent never writes, edits, or deletes
  this** — no tool touches it; the only writer is the Settings UI via the manager.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

# User Rules is a bounded settings field, not a document store: big enough for any
# real rule list, small enough that a paste-accident (or a hostile client) can't
# bloat every future system prompt.
MAX_USER_RULES_CHARS = 20_000


class MemorySettingsStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @property
    def enabled(self) -> bool:
        return bool(self._load().get("enabled", True))  # on by default (spec §5.4)

    @property
    def auto_inject(self) -> bool:
        # P0-1: 记忆每轮自动注入默认开启——"越用越懂你"的前提。
        # 仅当用户在设置中关闭、或显式设 DESKMATE_AUTO_INJECT=0 时才关。
        return bool(self._load().get("auto_inject", True))

    @property
    def user_rules(self) -> str:
        rules = self._load().get("user_rules", "")
        return rules if isinstance(rules, str) else ""

    def set(
        self,
        *,
        enabled: Optional[bool] = None,
        user_rules: Optional[str] = None,
        auto_inject: Optional[bool] = None,
    ) -> dict:
        with self._lock:
            data = self._load()
            if enabled is not None:
                data["enabled"] = bool(enabled)
            if user_rules is not None:
                data["user_rules"] = str(user_rules)[:MAX_USER_RULES_CHARS]
            if auto_inject is not None:
                data["auto_inject"] = bool(auto_inject)
            self._save(data)
        if auto_inject is not None:
            # 仅当显式切换时才镜像到进程环境变量，避免无关的 settings 更新污染进程环境。
            self.apply_auto_inject_env()
        return self.snapshot()

    def apply_auto_inject_env(self) -> None:
        """Mirror persisted ``auto_inject`` into the process env so the per-turn
        injection gate in ``coworker/agent.py`` reflects the default-on state."""
        os.environ["DESKMATE_AUTO_INJECT"] = "1" if self.auto_inject else "0"

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "user_rules": self.user_rules,
            "auto_inject": self.auto_inject,
        }


def format_user_rules(rules: str) -> str:
    """The system-prompt block for user rules. Empty rules -> empty string."""
    text = (rules or "").strip()
    if not text:
        return ""
    return (
        "User rules (written by the user in Settings; always follow these — on any "
        f"conflict they outrank learned memories):\n{text}"
    )
