"""
auto_tasks — 统一的「自动任务沉淀框架」。

所有与"我/写作/作品"相关的自动任务（每日读书、进度跟踪、风格复盘…）
都通过本框架沉淀，统一数据模型、统一沉淀管道、统一展示入口。

新任务接入只需两步：
  1. register_task(...)  登记任务元数据
  2. record_task_result(...)  任务执行完调用一次，自动落地+分类+推记忆

数据目录：~/.deskmate/auto_tasks/
  registry.json    任务注册表
  timeline.jsonl   统一时间线（所有任务动作按时间排列）
  insights/<type>/ 结构化洞察（按任务类型分子目录）
  details/<type>/  详情文件（大段心得、简报等）
"""

from .models import TaskRecord, TaskRegistryEntry, Insight
from .registry import TaskRegistry, get_registry, register_task
from .recorder import TaskRecorder, get_recorder, record_task_result

__all__ = [
    "TaskRecord",
    "TaskRegistryEntry",
    "Insight",
    "TaskRegistry",
    "get_registry",
    "register_task",
    "TaskRecorder",
    "get_recorder",
    "record_task_result",
]
