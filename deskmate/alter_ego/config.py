"""P2-6 个人 AI 分身模式——开关（环境变量优先，文件持久化为辅）。

分身模式是高级功能，默认关闭，需用户显式开启：
- 环境变量 ``DESKMATE_ALTER_EGO=1`` 开 / ``=0`` 关（运行期最高优先级）
- 未设环境变量时读 ``<DESKMATE_DIR>/alter_ego_config.json`` 的 ``enabled`` 字段
- 文件写入失败不阻断（运行期环境变量已生效）

开启后：
- 学习闭环：每轮对话后从用户消息抽取决策偏好，增量落盘 ``decision_profile.json``
- 注入闭环：每轮对话把「另一个我」画像（写作风格 + 决策偏好）追加进记忆上下文
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ENV_KEY = "DESKMATE_ALTER_EGO"
_CONFIG_FILE = "alter_ego_config.json"


def _base_dir() -> Path:
    return Path(os.environ.get("DESKMATE_DIR", str(Path.home() / ".deskmate")))


def _config_path() -> Path:
    return _base_dir() / _CONFIG_FILE


def is_enabled() -> bool:
    """分身模式是否开启（环境变量优先，其次配置文件）。"""
    env = os.environ.get(ENV_KEY)
    if env == "1":
        return True
    if env == "0":
        return False
    p = _config_path()
    if p.is_file():
        try:
            return bool(json.loads(p.read_text(encoding="utf-8")).get("enabled", False))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
    return False


def set_enabled(enabled: bool) -> bool:
    """设置分身模式开关：同时写运行期环境变量与持久化文件。

    Returns:
        设置后是否开启（始终与入参一致；持久化失败不阻断）
    """
    os.environ[ENV_KEY] = "1" if enabled else "0"
    try:
        p = _config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"enabled": bool(enabled)}, ensure_ascii=False),
            encoding="utf-8",
        )
    except (OSError, ValueError, json.JSONDecodeError):
        pass  # 持久化失败不阻断（运行期环境变量已生效）
    return bool(enabled)
