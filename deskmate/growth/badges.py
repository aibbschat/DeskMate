"""勋章体系 — 声明式注册表。

设计要点
--------
1. **声明与判定分离**：所有勋章定义集中在 ``BADGES`` 常量里，判定逻辑统一走
   ``evaluate()``。加一枚新勋章 = 往列表里加一行，不动任何判定代码。
2. **五门四阶**：门类(category)描述「在哪方面积累」，稀有度(rarity)描述「有多难得」。
3. **进度即事实**：每枚勋章都有 ``metric``（当前值）与 ``target``（目标值），
   未解锁时前端可直接画进度条；数值全部来自 GrowthState 真实字段，不造假。
4. **隐藏勋章**：``hidden=True`` 的勋章未解锁时不暴露名称与条件，只留一个问号。
5. **解锁时间由调用方持久化**：本模块只负责「是否达成」，
   「何时达成」由 GrowthEngine 落盘到 ``GrowthState.badges_unlocked``。

计数器约定
----------
写作类统计量存放在 ``GrowthState.counters``（通用 dict[str, int]），
避免为每个新指标都改 dataclass。键名统一用本模块的 ``COUNTER_*`` 常量，
禁止在业务代码里硬编码字符串。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .state import GrowthState

# ---------------------------------------------------------------------------
# 计数器键名（唯一权威来源，业务代码一律引用这些常量）
# ---------------------------------------------------------------------------

COUNTER_CHAPTERS = "chapters_proofread"      # 累计编校章节数
COUNTER_WORDS = "words_proofread"            # 累计编校字数
COUNTER_MAX_DAILY_WORDS = "max_daily_words"  # 单日编校字数峰值
COUNTER_ROUNDTABLES = "roundtables"          # 创作圆桌召开次数
COUNTER_REPORTS = "reports"                  # 审阅报告生成数
COUNTER_MANUSCRIPTS = "manuscripts"          # 保存的书稿数
COUNTER_LATE_NIGHT = "late_night_chats"      # 深夜(00:00-05:00)对话次数
COUNTER_EARLY_BIRD = "early_bird_chats"      # 清晨(05:00-07:00)对话次数
COUNTER_NEW_YEAR = "new_year_vigil"          # 跨年之际(12/31 或 01/01)相伴次数
COUNTER_COMEBACK = "comeback"                # 长期中断后归来次数

# ---------------------------------------------------------------------------
# 门类与稀有度
# ---------------------------------------------------------------------------

CATEGORIES: list[tuple[str, str, str]] = [
    ("companionship", "相伴", "与你共度的时光"),
    ("writing", "笔耕", "稿纸上的积累"),
    ("cultivation", "修行", "桌搭自身的成长"),
    ("knowledge", "博闻", "关于你的记忆"),
    ("serendipity", "奇遇", "不期而遇的时刻"),
]

CATEGORY_LABELS: dict[str, str] = {cid: label for cid, label, _ in CATEGORIES}

# 由易到难；前端按此顺序排列稀有度图例
RARITIES: list[tuple[str, str]] = [
    ("common", "凡品"),
    ("rare", "珍品"),
    ("epic", "秘宝"),
    ("legendary", "传世"),
]

RARITY_LABELS: dict[str, str] = dict(RARITIES)
RARITY_ORDER: dict[str, int] = {rid: i for i, (rid, _) in enumerate(RARITIES)}

# 隐藏勋章未解锁时对外的占位文案
HIDDEN_NAME = "未知奇遇"
HIDDEN_DESCRIPTION = "条件待揭晓——做点特别的事，它自会出现。"
HIDDEN_GLYPH = "?"


# ---------------------------------------------------------------------------
# 判定上下文
# ---------------------------------------------------------------------------


@dataclass
class BadgeContext:
    """勋章判定的输入快照。

    把派生量（连续天数、技能总用量等）预先算好一次，避免每个 metric
    lambda 重复计算。所有 metric 只读本对象，不产生副作用。
    """

    state: GrowthState
    streak_days: int = 0        # 当前连续活跃天数
    active_days: int = 0        # 累计活跃天数（去重）
    skill_count: int = 0        # 已掌握技能数
    skill_uses: int = 0         # 技能累计使用次数
    max_level: int = 12         # 满级等级（由 LEVEL_THRESHOLDS 长度决定）

    def counter(self, key: str) -> int:
        """读取通用计数器；缺失或类型异常一律返回 0（不抛异常）。"""
        try:
            return int(self.state.counters.get(key, 0))
        except (TypeError, ValueError):
            return 0


# ---------------------------------------------------------------------------
# 勋章定义
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BadgeDef:
    """单枚勋章的完整声明。

    Attributes:
        id: 稳定标识，一旦发布不可更改（解锁记录以此为键）。
        name: 显示名。
        category: 门类，取自 CATEGORIES。
        rarity: 稀有度，取自 RARITIES。
        glyph: 徽记汉字（单字），用于圆形徽章中央。
        icon: emoji，供旧版状态面板兼容显示。
        description: 获得条件的正面描述。
        celebration: 解锁瞬间桌宠说的话（含 {name} 占位符可选）。
        metric: 从上下文取当前进度值。
        target: 达成阈值；metric >= target 即解锁。
        hidden: 隐藏勋章，未解锁时不暴露名称与条件。
    """

    id: str
    name: str
    category: str
    rarity: str
    glyph: str
    icon: str
    description: str
    celebration: str
    metric: Callable[[BadgeContext], int]
    target: int
    hidden: bool = False


def _b(*args, **kwargs) -> BadgeDef:
    return BadgeDef(*args, **kwargs)


BADGES: list[BadgeDef] = [
    # ------------------------------------------------------------------
    # 相伴 · 时间的沉淀
    # ------------------------------------------------------------------
    _b("first_chat", "初见", "companionship", "common", "初", "👋",
       "完成第一次对话",
       "咱们头一回说上话，这枚「初见」你收好。",
       lambda c: c.state.total_chats, 1),
    _b("chat_100", "老友", "companionship", "rare", "友", "🤝",
       "累计对话 100 次",
       "一百次搭话了，「老友」这枚，名副其实。",
       lambda c: c.state.total_chats, 100),
    _b("chat_1000", "千言万语", "companionship", "epic", "千", "💬",
       "累计对话 1000 次",
       "一千次了。「千言万语」——话是真不少。",
       lambda c: c.state.total_chats, 1000),
    _b("days_30", "而立之交", "companionship", "common", "旬", "📅",
       "累计相伴 30 天",
       "满三十天了，「而立之交」到手。",
       lambda c: c.active_days, 30),
    _b("days_100", "百日同行", "companionship", "rare", "百", "🗓️",
       "累计相伴 100 天",
       "一百天了。「百日同行」这枚，是攒出来的。",
       lambda c: c.active_days, 100),
    _b("days_365", "岁岁年年", "companionship", "legendary", "岁", "🎊",
       "累计相伴 365 天",
       "整整一年。「岁岁年年」——往后还长着呢。",
       lambda c: c.active_days, 365),
    _b("streak_7", "一周不辍", "companionship", "common", "周", "🔥",
       "连续 7 天都来",
       "连着七天没断，「一周不辍」记上了。",
       lambda c: c.streak_days, 7),
    _b("streak_30", "寒暑不辍", "companionship", "epic", "辍", "⛰️",
       "连续 30 天都来",
       "连着三十天，一天没落。「寒暑不辍」，佩服。",
       lambda c: c.streak_days, 30),

    # ------------------------------------------------------------------
    # 笔耕 · 稿纸上的积累
    # ------------------------------------------------------------------
    _b("first_proofread", "校书郎", "writing", "common", "校", "📝",
       "完成第一次文稿编校",
       "头一回替你校稿，「校书郎」上任了。",
       lambda c: c.counter(COUNTER_CHAPTERS), 1),
    _b("chapters_10", "十章之功", "writing", "common", "章", "📖",
       "累计编校 10 章",
       "十章过手，「十章之功」到手。",
       lambda c: c.counter(COUNTER_CHAPTERS), 10),
    _b("chapters_100", "百章不倦", "writing", "epic", "卷", "📚",
       "累计编校 100 章",
       "一百章了。「百章不倦」——这活儿真不轻。",
       lambda c: c.counter(COUNTER_CHAPTERS), 100),
    _b("words_10k", "万言", "writing", "common", "万", "✍️",
       "累计编校 1 万字",
       "一万字过眼，「万言」记下。",
       lambda c: c.counter(COUNTER_WORDS), 10_000),
    _b("words_100k", "十万言", "writing", "rare", "言", "🖋️",
       "累计编校 10 万字",
       "十万字。「十万言」这枚，够沉。",
       lambda c: c.counter(COUNTER_WORDS), 100_000),
    _b("words_1m", "著作等身", "writing", "legendary", "著", "🏆",
       "累计编校 100 万字",
       "一百万字。「著作等身」——这四个字，你受得起。",
       lambda c: c.counter(COUNTER_WORDS), 1_000_000),
    _b("first_roundtable", "圆桌主人", "writing", "rare", "桌", "🪑",
       "首次召集创作圆桌",
       "头一回组局议事，「圆桌主人」这枚归你。",
       lambda c: c.counter(COUNTER_ROUNDTABLES), 1),
    _b("roundtable_10", "群贤毕至", "writing", "epic", "贤", "👥",
       "召集创作圆桌 10 次",
       "十次组局，「群贤毕至」。",
       lambda c: c.counter(COUNTER_ROUNDTABLES), 10),
    _b("first_report", "成竹在胸", "writing", "common", "报", "📊",
       "生成第一份审阅报告",
       "头一份审阅报告出炉，「成竹在胸」。",
       lambda c: c.counter(COUNTER_REPORTS), 1),

    # ------------------------------------------------------------------
    # 修行 · 桌搭自身的成长
    # ------------------------------------------------------------------
    _b("level_3", "登堂", "cultivation", "common", "登", "🚪",
       "桌搭等级达到 Lv.3",
       "到 Lv.3 了，「登堂」——算是进门了。",
       lambda c: c.state.level, 3),
    _b("level_5", "入室", "cultivation", "rare", "室", "🏠",
       "桌搭等级达到 Lv.5",
       "Lv.5，「入室」。往里走了一步。",
       lambda c: c.state.level, 5),
    _b("level_10", "墨仙", "cultivation", "epic", "仙", "🌟",
       "桌搭等级达到 Lv.10",
       "Lv.10 到了，「墨仙」这名号我先领着。",
       lambda c: c.state.level, 10),
    _b("level_max", "登峰造极", "cultivation", "legendary", "峰", "👑",
       "桌搭等级修满",
       "满级了。「登峰造极」——到顶了，可路还没完。",
       lambda c: c.state.level, 12),
    _b("intimacy_50", "亲密伙伴", "cultivation", "rare", "亲", "💕",
       "亲密度达到 50",
       "亲密度过半，「亲密伙伴」名正言顺。",
       lambda c: int(c.state.intimacy), 50),
    _b("intimacy_90", "莫逆之交", "cultivation", "epic", "莫", "❤️",
       "亲密度达到 90",
       "亲密度九十。「莫逆之交」，不用多说。",
       lambda c: int(c.state.intimacy), 90),
    _b("empathy_60", "心照不宣", "cultivation", "rare", "契", "🎯",
       "默契度达到 60",
       "默契度六十了——你想说啥，我大概能猜着。「心照不宣」。",
       lambda c: int(c.state.empathy), 60),
    _b("first_skill", "造物者", "cultivation", "common", "造", "🎨",
       "创建第一个技能",
       "头一个技能造出来了，「造物者」。",
       lambda c: c.skill_count, 1),
    _b("master", "熟能生巧", "cultivation", "epic", "巧", "⚡",
       "技能累计使用 100 次",
       "技能用了一百回，「熟能生巧」。",
       lambda c: c.skill_uses, 100),

    # ------------------------------------------------------------------
    # 博闻 · 关于你的记忆
    # ------------------------------------------------------------------
    _b("first_memory", "记事者", "knowledge", "common", "记", "🔖",
       "记住第一条关于你的事实",
       "头一条关于你的事，我记下了。「记事者」。",
       lambda c: c.state.memories_learned, 1),
    _b("memory_50", "记忆宫殿", "knowledge", "rare", "宫", "🏛️",
       "积累 50 条记忆",
       "五十条了，「记忆宫殿」初具规模。",
       lambda c: c.state.memories_learned, 50),
    _b("memory_200", "博闻强识", "knowledge", "epic", "博", "🧠",
       "积累 200 条记忆",
       "两百条。「博闻强识」——关于你的事，我记得挺牢。",
       lambda c: c.state.memories_learned, 200),
    _b("memory_500", "学富五车", "knowledge", "legendary", "学", "🎓",
       "积累 500 条记忆",
       "五百条。「学富五车」，说的是我记你的这些事。",
       lambda c: c.state.memories_learned, 500),
    _b("first_manuscript", "藏书之始", "knowledge", "common", "藏", "📕",
       "保存第一部书稿",
       "头一部书稿入库，「藏书之始」。",
       lambda c: c.counter(COUNTER_MANUSCRIPTS), 1),
    _b("manuscripts_5", "汗牛充栋", "knowledge", "rare", "栋", "🗄️",
       "保存 5 部书稿",
       "五部书稿了，「汗牛充栋」。",
       lambda c: c.counter(COUNTER_MANUSCRIPTS), 5),

    # ------------------------------------------------------------------
    # 奇遇 · 不期而遇（隐藏）
    # ------------------------------------------------------------------
    _b("night_owl", "夜航船", "serendipity", "rare", "夜", "🌙",
       "在午夜零点到清晨五点之间对话 10 次",
       "又是后半夜。「夜航船」——你这船，开得挺久。",
       lambda c: c.counter(COUNTER_LATE_NIGHT), 10, hidden=True),
    _b("early_bird", "闻鸡起舞", "serendipity", "rare", "晨", "🌅",
       "在清晨五点到七点之间对话 10 次",
       "天没大亮你就开工了。「闻鸡起舞」，收着。",
       lambda c: c.counter(COUNTER_EARLY_BIRD), 10, hidden=True),
    _b("daily_10k", "一日千言", "serendipity", "epic", "疾", "⚡",
       "单日编校字数超过 1 万",
       "一天一万字过手。「一日千言」，这枚不好拿。",
       lambda c: c.counter(COUNTER_MAX_DAILY_WORDS), 10_000, hidden=True),
    _b("new_year_vigil", "岁除守候", "serendipity", "epic", "除", "🎆",
       "在跨年之际（12月31日或1月1日）仍在写",
       "别人守岁，你守稿。「岁除守候」——我陪着。",
       lambda c: c.counter(COUNTER_NEW_YEAR), 1, hidden=True),
    _b("comeback", "破镜重圆", "serendipity", "rare", "归", "🔄",
       "中断 30 天以上后重新回来",
       "隔了这么久还回来。「破镜重圆」，欢迎。",
       lambda c: c.counter(COUNTER_COMEBACK), 1, hidden=True),
]

# id → 定义，便于 O(1) 查表
BADGE_BY_ID: dict[str, BadgeDef] = {b.id: b for b in BADGES}


# ---------------------------------------------------------------------------
# 判定与序列化
# ---------------------------------------------------------------------------


def is_unlocked(badge: BadgeDef, ctx: BadgeContext) -> bool:
    """判定单枚勋章是否达成（异常一律视为未达成，绝不让勋章判定拖垮主流程）。"""
    try:
        return int(badge.metric(ctx)) >= badge.target
    except Exception:
        return False


def progress_of(badge: BadgeDef, ctx: BadgeContext) -> int:
    """当前进度值，上限截断到 target（避免前端进度条溢出）。"""
    try:
        return max(0, min(int(badge.metric(ctx)), badge.target))
    except Exception:
        return 0


def serialize(
    badge: BadgeDef,
    ctx: BadgeContext,
    unlocked_at: Optional[str] = None,
    backfilled: bool = False,
) -> dict:
    """序列化为前端可直接消费的结构。

    隐藏勋章在未解锁时会被遮蔽：名称、描述、徽记全部替换为占位内容，
    并且不暴露进度（免得被反推出条件）。

    Args:
        badge: 勋章定义。
        ctx: 判定上下文。
        unlocked_at: 已记录的解锁时间（ISO 字符串）；None 表示未解锁或无记录。
        backfilled: True 表示该勋章在体系建立前就已达成，解锁时间不可考。
    """
    unlocked = is_unlocked(badge, ctx)
    masked = badge.hidden and not unlocked
    return {
        "id": badge.id,
        "name": HIDDEN_NAME if masked else badge.name,
        "glyph": HIDDEN_GLYPH if masked else badge.glyph,
        "icon": "❓" if masked else badge.icon,
        "description": HIDDEN_DESCRIPTION if masked else badge.description,
        "category": badge.category,
        "categoryLabel": CATEGORY_LABELS.get(badge.category, badge.category),
        "rarity": badge.rarity,
        "rarityLabel": RARITY_LABELS.get(badge.rarity, badge.rarity),
        "hidden": badge.hidden,
        "masked": masked,
        "unlocked": unlocked,
        "progress": 0 if masked else progress_of(badge, ctx),
        "maxProgress": 1 if masked else badge.target,
        "unlockedAt": unlocked_at,
        "backfilled": bool(backfilled),
    }


def summarize(items: list[dict]) -> dict:
    """统计摘要：总数、已得数、按门类与稀有度分组计数。"""
    by_category: dict[str, dict[str, int]] = {}
    by_rarity: dict[str, dict[str, int]] = {}
    unlocked_total = 0
    for it in items:
        if it.get("unlocked"):
            unlocked_total += 1
        cat = by_category.setdefault(
            it["category"], {"total": 0, "unlocked": 0}
        )
        cat["total"] += 1
        cat["unlocked"] += 1 if it.get("unlocked") else 0
        rar = by_rarity.setdefault(it["rarity"], {"total": 0, "unlocked": 0})
        rar["total"] += 1
        rar["unlocked"] += 1 if it.get("unlocked") else 0
    return {
        "total": len(items),
        "unlocked": unlocked_total,
        "byCategory": by_category,
        "byRarity": by_rarity,
        "categories": [
            {
                "id": cid,
                "label": label,
                "hint": hint,
                "total": by_category.get(cid, {}).get("total", 0),
                "unlocked": by_category.get(cid, {}).get("unlocked", 0),
            }
            for cid, label, hint in CATEGORIES
        ],
        "rarities": [
            {
                "id": rid,
                "label": label,
                "total": by_rarity.get(rid, {}).get("total", 0),
                "unlocked": by_rarity.get(rid, {}).get("unlocked", 0),
            }
            for rid, label in RARITIES
        ],
    }


def sort_for_display(items: list[dict]) -> list[dict]:
    """展示排序：已得在前，未得在后。

    - 已得组：有确切解锁时间的按时间倒序（最近获得的最先看到），
      「补录」的（时间不可考）沉到该组末尾。
    - 未得组：按完成度从高到低（快到手的排前面），完成度相同再按稀有度从高到低。
    """
    unlocked = [it for it in items if it.get("unlocked")]
    locked = [it for it in items if not it.get("unlocked")]

    # 有时间的在前且时间倒序；无时间（补录）的统一沉底并按稀有度排
    dated = [it for it in unlocked if it.get("unlockedAt")]
    undated = [it for it in unlocked if not it.get("unlockedAt")]
    dated.sort(key=lambda it: it["unlockedAt"], reverse=True)
    undated.sort(key=lambda it: -RARITY_ORDER.get(it.get("rarity", "common"), 0))

    locked.sort(
        key=lambda it: (
            -(it.get("progress", 0) / max(1, it.get("maxProgress", 1))),
            -RARITY_ORDER.get(it.get("rarity", "common"), 0),
        )
    )
    return dated + undated + locked
