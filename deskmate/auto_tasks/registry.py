"""任务注册表：管理所有自动任务的元数据，决定分类和展示。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .models import TaskRegistryEntry


class TaskRegistry:
    """任务注册表。持久化到 auto_tasks/registry.json。"""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.registry_file = self.base_dir / "registry.json"
        self._entries: dict[str, TaskRegistryEntry] = {}
        self._load()

    def _load(self):
        if self.registry_file.exists():
            try:
                data = json.loads(self.registry_file.read_text(encoding="utf-8"))
                for task_type, entry_data in data.items():
                    self._entries[task_type] = TaskRegistryEntry(**entry_data)
            except Exception:
                self._entries = {}

    def _save(self):
        self.base_dir.mkdir(parents=True, exist_ok=True)
        data = {k: v.__dict__ for k, v in self._entries.items()}
        self.registry_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def register(self, entry: TaskRegistryEntry) -> TaskRegistryEntry:
        """登记一个新任务。如果已存在则更新（不覆盖 run_count/last_run_at）。"""
        existing = self._entries.get(entry.task_type)
        if existing:
            entry.run_count = existing.run_count
            entry.last_run_at = existing.last_run_at
            entry.created_at = existing.created_at or entry.created_at
        self._entries[entry.task_type] = entry
        self._save()
        return entry

    def get(self, task_type: str) -> Optional[TaskRegistryEntry]:
        return self._entries.get(task_type)

    def set_enabled(self, task_type: str, enabled: bool) -> bool:
        """P1-3 用户可配置：切换任务启用/暂停状态，持久化到 registry.json。

        返回 False 表示任务不存在（未登记），调用方应提示用户。
        """
        entry = self._entries.get(task_type)
        if entry is None:
            return False
        entry.enabled = bool(enabled)
        self._save()
        return True

    def all(self) -> list[TaskRegistryEntry]:
        return list(self._entries.values())

    def by_category(self, category: str) -> list[TaskRegistryEntry]:
        return [e for e in self._entries.values() if e.category == category]

    def categories(self) -> list[str]:
        return sorted(set(e.category for e in self._entries.values()))

    def touch_run(self, task_type: str, date: str):
        """记录一次执行，更新 run_count 和 last_run_at。"""
        entry = self._entries.get(task_type)
        if entry:
            entry.run_count += 1
            entry.last_run_at = date
            self._save()

    def to_dict(self) -> dict:
        return {k: v.__dict__ for k, v in self._entries.items()}


# 全局单例
_default_registry: Optional[TaskRegistry] = None


def get_registry(base_dir: Optional[str | Path] = None) -> TaskRegistry:
    global _default_registry
    if _default_registry is None:
        base_dir = base_dir or os.path.join(os.path.expanduser("~"), ".deskmate", "auto_tasks")
        _default_registry = TaskRegistry(base_dir)
    return _default_registry


def register_task(
    task_type: str,
    task_name: str,
    category: str = "writing",
    description: str = "",
    insight_template: str = "",
) -> TaskRegistryEntry:
    """便捷函数：登记一个任务。"""
    from datetime import date as _date
    entry = TaskRegistryEntry(
        task_type=task_type,
        task_name=task_name,
        category=category,
        description=description,
        insight_template=insight_template,
        created_at=_date.today().isoformat(),
    )
    return get_registry().register(entry)
