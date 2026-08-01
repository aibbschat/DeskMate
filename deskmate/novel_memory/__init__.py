"""小说记忆 MCP 服务包。

暴露给 OpenWorker agent 的工具：
- analyze_novel(folder)   通读全书，抽取权威人物关系圣经，写入 workspace 记忆
- novel_recall(query, folder)  跨章检索正文片段 + 圣经
- novel_relationships(char, folder)  查某角色的全部关系
"""

from .server import main

__all__ = ["main"]
