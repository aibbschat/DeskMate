"""Bot Mode：bot 常驻、群随建随撤、自然语言驱动。

设计依据见 ``docs/bot-mode-redesign.md``。三条硬边界：

1. **bot 不是人设**：bot 是有独立 session / 记忆 / 模型的常驻实体；名册直接读官方
   PersonaRegistry（零新存储层，与 Hermes「bot = profile」同构）。
2. **群是临时协调面**：可建、可列、可撤；撤群只打墓碑，消息历史不删，room_id 永不复用。
3. **不填表单**：开群给个话题名即可，之后在群里说人话；不 @ = 全员，@ 谁谁答，
   讨论有硬上限（6 成员 / 3 轮 / 10 消息）。

本包不修改 ``coworker/teams/*``（上游干净代码），只经 ``ChatStore`` 复用其群聊原语。
"""
from __future__ import annotations

from . import discussion, rooms, roster

__all__ = ["discussion", "rooms", "roster"]
