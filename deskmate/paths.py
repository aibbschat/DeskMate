"""DeskMate 统一根目录解析（P0 测试隔离根因修复）。

所有持久化路径必须以本模块的 :func:`deskmate_base_dir` 为准，保证测试通过
``DESKMATE_DIR``（兼容别名 ``DESKMATE_MEMORY_DIR``）指向临时目录时，全部模块
自动隔离、不再触碰真实 ``~/.deskmate``。

优先级：``DESKMATE_DIR`` > ``DESKMATE_MEMORY_DIR``(别名兼容) > ``~/.deskmate``。

重要：必须在「调用时」取值，不要缓存到模块级常量——conftest 在 import 之后才
``monkeypatch.setenv("DESKMATE_DIR", ...)``，模块级缓存会让隔离失效。
"""

from __future__ import annotations

import os
from pathlib import Path


def deskmate_base_dir() -> Path:
    """返回统一根目录（调用时解析环境变量）。"""
    raw = os.environ.get("DESKMATE_DIR") or os.environ.get("DESKMATE_MEMORY_DIR")
    return Path(raw) if raw else Path(os.path.expanduser("~/.deskmate"))


def deskmate_db_path() -> Path:
    """统一库 ``deskmate.db`` 路径（认知层 / 记忆层 / 日记层同库）。"""
    return deskmate_base_dir() / "deskmate.db"


def deskmate_legacy_profile_path() -> Path:
    """legacy 认知画像 JSON（首次迁移源，仅作只读兼容）。"""
    return deskmate_base_dir() / "cognitive_profile.json"
