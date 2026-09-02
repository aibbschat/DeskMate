"""花园 —— 个人中心的私密花园升级层。

把现有个人中心（ProfileCenter）升级成「花园」：
- ``season``：四季 × 昼夜（本地时间，确定性）
- ``flowers``：花开记录（事件 → 花，幂等，零 LLM；含花园图鉴 flora_snapshot 与回响 echo）
- ``clock``：花园时钟（确定性事件互动，幂等，克制不打扰；含节日事件）
- ``storybook``：羁绊故事簿（P1：bond 阶段叙事 + 「它眼中的我」画像聚合）

全部 fail-open：任何异常静默，不影响个人中心与主动推理主流程。
数据设备本地（BlobStore KEY_GARDEN），不随跨端同步。
"""

from deskmate.garden import clock, flowers, season, state, storybook

__all__ = ["clock", "flowers", "season", "state", "storybook"]
