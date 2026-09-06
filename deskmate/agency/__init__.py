"""L2 治理层（自主发起 + 代理决策）。

把「小桔替你行动」从设计变成代码：自主发起总开关、任务白名单、风险闸
（低风险可自主 / 高风险走提议卡）、统一任务记录（任务面板可见）、代理决策路由。

设计同构意志层（deskmate/will）：独立配置文件 l2_settings.json + 环境变量 kill switch
（DESKMATE_L2_ENABLED=0 关闭）+ 原子写 + TTL 缓存 + fail-open。所有异常绝不阻断主流程。
"""

from __future__ import annotations

from .settings import AgencySettings, load_agency_settings, save_agency_settings
from .engine import AgencyEngine
from .task_log import TaskLog, TaskRecord, TaskStatus

__all__ = [
    "AgencySettings",
    "load_agency_settings",
    "save_agency_settings",
    "AgencyEngine",
    "TaskLog",
    "TaskRecord",
    "TaskStatus",
]
