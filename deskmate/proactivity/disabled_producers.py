"""健康生产者的停用开关（2026-10-09 建，2026-10-10 判据修好后**已恢复 automation**）。

## 现状：automation 已恢复（2026-10-10，萧哥拍板）

本模块最初是**止血带**：``automation`` 生产者当时**在误报且必然误报**——三重缺陷叠加
（详见 ``docs/本地云端双脑分工设计_2026-10-09.md`` §3.5 与附录 A）：

1. **``last_run`` 只在任务结束时更新**，生产者在**运行期间**读它。
   实测 10-09 读书任务 10:00:57 开始、10:37:16 结束，而误报发生在 **10:34:13**
   —— 跑了 36 分钟，它在第 34 分钟报「该跑却没跑成」。
2. **硬锚点判据永不触发**：判据写
   ``task_type in ("daily_reading", "progress_tracking")``，
   但实测 5 个任务的 ``task_type`` **全是 None** → 对所有任务不触发。
3. **关键词判据误伤**：失败词表含「抱歉/失败/无法/火山/额度」，
   而**成功**的汇报原文就含「不擅自删除」这类措辞；且读书任务
   ``artifacts: []``（正文写进 ``.deskmate/tmp_reading/``，不走 artifact 通道），
   判据假设「产出必进 artifacts」对读书任务不成立。

三处已在**方案 B**（提交 ``44b8490``）根治，并经真实数据验证：``automation.db`` 里
5 个启用任务在新判据下**全部判 ok、零误报**。故 ``MISREPORTING`` 现已清空。

**本模块保留而非删除**：它是「哪个生产者当前不可信」的集中登记处。将来若再出现
误报生产者，只需往 ``MISREPORTING`` 加一个名字，无需改 ``ledger.py``（见下节）。

## 为什么做成独立模块（而不是改 ``run_producers_all`` 的默认 exclude）

``ledger.py`` 正被 R0（指纹锚定）并行修改，写作用域冲突。本模块把
「停用清单」放在自己这里，调用方（``tick.py`` / ``xiaoju_agency_tick.py``）
只传一个 exclude 参数——**与 ``run_producers_all(exclude=...)`` 既有签名兼容**，
不需要改 ledger.py 一行。

## 处置清单

| producer | 处置 | 理由 |
|---|---|---|
| ``automation`` | 保留（**已恢复**） | 误报根因已除，恢复体检职责 |
| ``sync_health`` | 保留 | 实测 1 条：``proactive_ledger`` sync_uuid 漂移，**真实**且有价值 |
| ``writing`` | 保留 | 实测 1 条：《长编》6 天没动笔，**合理**且有价值 |

停用/恢复都是**可一键回滚**的：设 ``DESKMATE_PRODUCERS_DISABLED="sync_health,writing"``
即临时停掉任意名单，设为 ``""``（空串）即一个都不停。无需改代码。
"""

from __future__ import annotations

import logging
import os
from typing import Tuple

logger = logging.getLogger(__name__)

#: 因误报而临时停用的生产者。
#: 2026-10-09 曾为 ``("automation",)``（误报止血）；2026-10-10 方案 B 根治三处缺陷后
#: **清空并恢复 automation**（萧哥拍板）。留空元组而非删除本模块，是为了让
#: 「当前不可信的生产者」有集中登记处，将来再出误报只需往这里加名字。
MISREPORTING: Tuple[str, ...] = ()

#: 覆盖环境变量。留空 → 用 MISREPORTING；显式设 "sync_health" → 停同步健康；
#: 设为 ""（空串）→ 一个都不停（用于对拍验证）。
_ENV_KEY = "DESKMATE_PRODUCERS_DISABLED"


def disabled_producers() -> Tuple[str, ...]:
    """当前应被排除的生产者名单（去空白、去重、稳定顺序）。"""
    raw = os.environ.get(_ENV_KEY)
    if raw is None:
        return MISREPORTING
    return tuple(dict.fromkeys(p.strip() for p in raw.split(",") if p.strip()))


def exclude_for_producers_all() -> Tuple[str, ...]:
    """给 ``run_producers_all(exclude=...)`` 用的完整排除名单。

    必须带上既有的默认 ``("cloud_audit",)`` —— 该默认**定义在 ledger.py 的函数签名里**，
    显式传 exclude 会**整体替换**它（不是追加）。漏掉就会让 ``cloud_audit``
    在每个周期窗口被重复跑一遍（它本由 tick 的 SUGGEST 分支负责）。
    """
    return ("cloud_audit", *disabled_producers())
