"""意志层（成为我）。

把认知层（懂我）与情感层（像我）的产出，**回流成行为调制参数**——
语气、主动阈值、记忆预算、打扰频率。本期只做 L1（像你一样判断），
L2（替你行动）单独立项。

模块：
- ``settings``：用户可调阈值（独立 will_settings.json，不碰含 API key 的 settings.json）
- ``digest``：认知/情感摘要层（只读、TTL 缓存、fail-open）
- ``engine.modulate_*``：五个行为调制方法接入 will 增量
"""
