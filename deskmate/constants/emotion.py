"""情绪态单一真相源（single source of truth）。

历史坑：日记自述（``emotion_sense``）、行为推断
（``emotion_timeline._infer_emotion``）、花园定色
（``emotion_profile.mood_tint_for_event``）三方曾各自硬编码同一组中文
情绪态字符串，改一处漏两处导致判定漂移。本模块为唯一来源，其它模块
一律从这里 import，不得再写魔法字符串。

五态模型（与三方对齐，必须完全一致）：

    顺利 / 平静 / 忙碌 / 压力大 / 低落

另有花园专用态「成长」——只出现在 ``mood_tint_for_event`` 的兜底映射里，
不属于五态集合（五态给「当天情绪判定」用，成长是「事件成就色」）。
"""

# 五态具名常量（值即中文展示态）
STATE_SMOOTH = "顺利"
STATE_CALM = "平静"
STATE_BUSY = "忙碌"
STATE_STRESS = "压力大"
STATE_LOW = "低落"

# 花园专用态（不属五态集合）
STATE_GROWTH = "成长"

# 五态有序元组：日记自述校验、行为推断返回集合、花园五态必须与之完全一致。
EMOTION_STATES = (STATE_SMOOTH, STATE_CALM, STATE_BUSY, STATE_STRESS, STATE_LOW)

# 各态心情基准值。mood 已改为情绪派生值（E1），没有第二个来源；
# LLM 给了具体数值就用它的，没给就落到这个基准上。
STATE_MOOD: dict[str, float] = {
    STATE_SMOOTH: 82.0,
    STATE_CALM: 78.0,
    STATE_BUSY: 72.0,
    STATE_STRESS: 58.0,
    STATE_LOW: 48.0,
}
