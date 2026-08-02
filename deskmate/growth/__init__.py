"""deskmate-growth — 墨灵成长系统。

职责：把「使用行为」转化为「真实行为调制」，杜绝空数值。

- 技能熟练度：由技能使用/成功/进化派生，反哺 match_skills 排序与索引徽标
- 亲密度 / 信任感 / 默契度：由交互反馈派生，分别调制语气 / proactive 阈值 / 记忆预算
- 等级：由成长值（XP）派生，解锁记忆预算上限
- 心情 / 活力：由反馈派生，调制 proactive 意愿

核心原则：任何数值都必须影响真实行为，否则不展示。
"""

from .engine import GrowthEngine
from .state import GrowthState, load_state, save_state

__all__ = ["GrowthEngine", "GrowthState", "load_state", "save_state"]
