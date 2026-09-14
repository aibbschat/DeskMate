"""原子落盘的共享小工具——**无内部依赖**，growth 下任何模块都能安全 import。

## 为什么单独成模块

``state`` ← ``engine`` ← ``diary``：``state.py`` 若直接 import ``diary.unique_tmp``
就成环。放进这个谁都不依赖的小模块，三处落盘点才能共用同一份实现。

## 它解决什么

项目里"临时文件 + ``os.replace``"的原子落盘一直写成固定名 ``path.name + ".tmp"``。
单进程单线程时没问题，但写日记/写 state 的触发源可能来自**不同线程与不同进程**
（后台 tick 线程、GUI 请求线程、独立的 proactivity MCP 进程）：

    线程 A: 写 xxx.tmp ──► os.replace(xxx.tmp, xxx)   ✓
    线程 B:      写 xxx.tmp（同一个文件）──► os.replace(…)  ✗ FileNotFoundError

B 的临时文件被 A 的 replace 提前拿走，B 整次写入**静默丢失**——日记少一则、
成长状态少一次保存（2026-09-14 实测两边都出现过）。加上 pid + 线程号即隔离。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path


def unique_tmp(path: Path) -> Path:
    """同目录内的**唯一**临时文件路径，供原子落盘使用。"""
    return path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """原子写文本：先写唯一临时文件，再 ``os.replace`` 覆盖目标。

    ``os.replace`` 在 POSIX / NT 上都保证读取方看不到半截文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = unique_tmp(path)
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)
