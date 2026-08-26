"""deskmate/teams_client.py — 薄壳：复用官方 coworker/teams 基座。

设计约束（与 deskmate/roundtable/web.py._resolve_store 一致）：
- deskmate 模块不直接持有 server.manager 单例（无全局入口、耦合过深）；
- 靠「数据根目录 base」定位官方 teams 的磁盘状态（teams.db / teams.json /
  journal.db / chat.db），与主 app（coworker.server.manager.ServerManager）共享同一份
  文件，从而读写互通。
- base 解析优先级：显式 base_dir → 环境变量 COWORKER_DATA_DIR → ~/.config/coworker。

本模块只负责「解析 base + 实例化官方 store/registry」，**不含任何二开业务逻辑**。
真正的建团 / 建项 / 门禁逻辑见阶段 1 的 deskmate/creation/pipeline.py 与
阶段 2 的 deskmate/roundtable/teams_bridge.py。
"""
from __future__ import annotations

import os
from pathlib import Path

from coworker.teams.store import TeamStore
from coworker.teams.registry import TeamRegistry
from coworker.teams.chat import ChatStore
from coworker.teams.journal import JournalStore


def resolve_base(base_dir: str | Path | None = None) -> Path:
    """解析官方 teams 状态所在的「数据根目录」。

    必须与主 app（coworker.server.manager.ServerManager(base=...)）使用同一 base，
    否则 deskmate 写入的 board / team 对主 app 不可见。

    优先级：显式 base_dir > 环境变量 COWORKER_DATA_DIR > ~/.config/coworker。
    """
    base = (
        base_dir
        or os.environ.get("COWORKER_DATA_DIR")
        or Path.home() / ".config" / "coworker"
    )
    return Path(base).expanduser()


class TeamsClient:
    """官方 teams 基座的轻量访问壳。

    用法::

        client = TeamsClient()              # 自动解析 base（与主 app 共享）
        client.team_store.create_item(...)  # 官方 board 动词
        client.teams.create(...)            # 官方 roster 注册
        client.chat_store.post(...)         # 官方群聊

    ``journal`` 默认与 manager 一致指向同一 ``journal.db``（assignment 事件会写入 case
    grant）；若只想用 board 且暂不需要 journal，可传 ``journal=False``（TeamStore 原生
    支持 standalone —— 源码注释明言 "Optional so the board works standalone"）。
    """

    def __init__(
        self,
        base_dir: str | Path | None = None,
        *,
        journal: bool = True,
    ) -> None:
        self.base = resolve_base(base_dir)
        self.journal_store = (
            JournalStore(self.base / "journal.db") if journal else None
        )
        self.team_store = TeamStore(self.base / "teams.db", journal=self.journal_store)
        self.chat_store = ChatStore(self.base / "chat.db")
        self.teams = TeamRegistry(self.base / "teams.json")

    def __repr__(self) -> str:
        return f"TeamsClient(base={self.base!r})"


__all__ = ["resolve_base", "TeamsClient"]
