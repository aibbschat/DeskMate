"""文件系统变更监控 — 记录指定目录下的文件增删改。"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class FileChange:
    path: str                         # 相对路径
    kind: Literal["created", "modified", "deleted"]
    size: int                         # 文件大小（删除时为 0）
    mtime: float                      # 修改时间戳


@dataclass
class FilesystemWatcher:
    """基于轮询的文件变更记录器。轻量，无额外依赖。"""
    root: str
    _snapshot: dict[str, float] = field(default_factory=dict, repr=False)
    _changes: list[FileChange] = field(default_factory=list, repr=False)
    _ignore_dirs: set[str] = field(default_factory=lambda: {
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        ".deskmate", ".next", "dist", "build", ".cache",
    })
    _ignore_exts: set[str] = field(default_factory=lambda: {
        ".pyc", ".pyo", ".o", ".so", ".dylib", ".DS_Store",
    })

    def __post_init__(self):
        self._snapshot = self._scan()

    def _should_ignore(self, rel: str) -> bool:
        parts = rel.split(os.sep)
        if any(p in self._ignore_dirs for p in parts):
            return True
        if any(rel.endswith(ext) for ext in self._ignore_exts):
            return True
        return False

    def _scan(self) -> dict[str, float]:
        """全量扫描，返回 {相对路径: mtime}。"""
        root = Path(self.root).expanduser().resolve()
        if not root.is_dir():
            return {}
        result: dict[str, float] = {}
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(root))
            if self._should_ignore(rel):
                continue
            try:
                result[rel] = p.stat().st_mtime
            except OSError:
                continue
            # 防止超大目录卡住
            if len(result) > 50000:
                break
        return result

    def poll(self) -> list[FileChange]:
        """轮询一次，返回自上次 poll 以来的变更列表。"""
        current = self._scan()
        changes: list[FileChange] = []

        # 新增 / 修改
        for rel, mtime in current.items():
            old_mtime = self._snapshot.get(rel)
            if old_mtime is None:
                kind: Literal["created", "modified"] = "created"
            elif mtime > old_mtime:
                kind = "modified"
            else:
                continue
            try:
                size = (Path(self.root) / rel).stat().st_size
            except OSError:
                size = 0
            changes.append(FileChange(path=rel, kind=kind, size=size, mtime=mtime))

        # 删除
        for rel in set(self._snapshot) - set(current):
            changes.append(FileChange(path=rel, kind="deleted", size=0, mtime=0))

        self._snapshot = current
        self._changes.extend(changes)
        return changes

    def get_recent(self, minutes: int = 5) -> list[FileChange]:
        """获取最近 N 分钟内的变更（从内部缓存）。"""
        cutoff = time.time() - minutes * 60
        return [c for c in self._changes if c.mtime >= cutoff or c.kind == "deleted"]
