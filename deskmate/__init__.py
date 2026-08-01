"""DeskMate — OpenWorker 之上的「懂你」灵魂层。

本包不修改 OpenWorker 核心代码，只通过两条扩展通道接入：
1. persona：`coworker/personas/builtin/deskmate.md`（系统提示 = 灵魂）
2. MCP 工具：`deskmate/novel_memory`（小说记忆：人物圣经抽取 + 跨章检索）

当前实现聚焦用户最痛的点——长篇小说人物关系错乱（敌人变朋友）。
后续可在此扩展：桌面感知、用户画像、主动推理、粒子 UI。
"""

__version__ = "0.1.0"
