"""USER.md 用户画像管理 — 从对话中自动提取用户偏好。"""

from __future__ import annotations

from pathlib import Path

from ._fs_lock import atomic_write_locked

_DEFAULT_USER = """\
# 用户画像

## 基本信息
- （待填充）

## 工作习惯
- （待观察）

## 偏好
- （待发现）

## 关注领域
- （待了解）

## 需要 AI 避免的
- （待记录）
"""


def get_user_path(base_dir: Path) -> Path:
    return base_dir / "USER.md"


def load_user(base_dir: Path) -> str:
    """加载 USER.md，不存在则返回默认模板。"""
    p = get_user_path(base_dir)
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return _DEFAULT_USER


def save_user(base_dir: Path, content: str) -> None:
    """保存 USER.md。

    ⚠ 加锁原子写（2026-09-23 补）：USER.md 被对话轮管理器方法、后台守护线程
    ``reflect_and_persist``、定时 ``run_dream`` 三入口并发写，历史上无互斥保护，
    是「同一件事被重复写成两条」（并发重入）的根因之一。改用项目约定的
    fcntl 锁 + 原子写（见 ``_fs_lock``）。
    """
    p = get_user_path(base_dir)
    atomic_write_locked(p, content)


def reset_user(base_dir: Path) -> str:
    """重置为默认模板。"""
    save_user(base_dir, _DEFAULT_USER)
    return _DEFAULT_USER
