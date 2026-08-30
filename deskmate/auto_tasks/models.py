"""统一数据模型：所有自动任务的沉淀都遵循这套结构。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Insight:
    """一条结构化洞察。可被推入 vault / style_profile。"""
    text: str                                    # 洞察内容
    tags: list[str] = field(default_factory=list)  # 标签（风格/人物/叙事/伏笔…）
    target: Optional[str] = None                 # 作用对象（哪本书/哪个项目）
    push_to_vault: bool = False                  # 是否推入 vault 长期记忆
    push_to_style: bool = False                  # 是否更新 style_profile


@dataclass
class TaskRecord:
    """一次任务执行的沉淀记录。写入 timeline.jsonl。"""
    task_type: str                               # 任务类型标识（daily_reading / progress_tracking / ...）
    task_name: str                               # 展示名（每日读书 / 进度跟踪 / ...）
    target: str                                  # 作用对象（哪本书/哪个项目/哪篇文章）
    action: str                                  # 动作：start / continue / finish / analyze / remind
    date: str                                    # 日期 YYYY-MM-DD
    ts: float = field(default_factory=time.time) # 时间戳
    summary: str = ""                            # 一句话摘要
    insights: list[Insight] = field(default_factory=list)  # 结构化洞察
    tags: list[str] = field(default_factory=list)           # 标签
    detail_file: Optional[str] = None            # 详情文件路径（相对 auto_tasks/details/）
    extra: dict = field(default_factory=dict)    # 任务特定的扩展字段

    def to_jsonl(self) -> str:
        d = asdict(self)
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_jsonl(cls, line: str) -> "TaskRecord":
        d = json.loads(line)
        insights = [Insight(**i) for i in d.pop("insights", [])]
        return cls(insights=insights, **d)


@dataclass
class TaskRegistryEntry:
    """任务注册表条目。登记后自动落地+分类。"""
    task_type: str                               # 唯一标识
    task_name: str                               # 展示名
    category: str                                # 分类：writing / self / works / other
    description: str = ""                        # 任务描述
    insight_template: str = ""                   # 洞察展示模板（可选，默认用通用模板）
    enabled: bool = True                         # 是否启用
    created_at: str = ""                         # 登记日期
    last_run_at: str = ""                        # 最后执行日期
    run_count: int = 0                           # 累计执行次数
