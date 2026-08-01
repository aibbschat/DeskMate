"""USER.md 用户画像管理 — 从对话中自动提取用户偏好。"""

from __future__ import annotations

from pathlib import Path

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
    """保存 USER.md。"""
    p = get_user_path(base_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def reset_user(base_dir: Path) -> str:
    """重置为默认模板。"""
    save_user(base_dir, _DEFAULT_USER)
    return _DEFAULT_USER
