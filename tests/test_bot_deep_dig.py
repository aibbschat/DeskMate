"""深挖层 → 能力常备化回归（2026-11-08 萧哥两步拍板）。

第一步（上午）：显式触发词 + 双轨 engine —— 当天真机翻车：萧哥照 Hermes 的
习惯说「先读原稿」「我需要脑洞大开的安排和设计」，术语词表一个没碰上，
深挖层没启动。第二步拍板原话「**你不能靠触发词。如果功能是齐的，但启动要
靠触发词，那以后如果换了本书，换个要讨论的场景呢**」：拆掉开关层。

现在的架构（Hermes 同构：能力常备，模型自主）：
1. ``make_reading_tools`` 默认全书预算 —— read_book 一次能读全书，任何
   bot 任何时候，不需要谁开权限；
2. ``_DEFAULT_MAX_ITERATIONS`` 4 → 12 —— 上限是「允许」不是「消耗」，
   不调工具的普通发言边际成本为零；真正的成本上界 = 只读工具面 +
   单次 8 万字 + 128K 窗口物理封顶；
3. ``is_deep_dig`` 降级为**信号放大器**（不是权限闸）：命中 → 全书材料
   前缀 + 强作业指令（prefix cache 友好、省模型试探）；没命中 → 能力
   照样齐，模型自己按题判断（guidance / read_book docstring / 人设教的）。
   词表不全的代价从「功能没启动」降为「少一点强指令」。

本组锁住的不变量：
- 能力常备：默认工具一次全书、engine 单轨统一预算、迭代 ≥12；
- 信号放大（非权限）：触发词命中给全书材料 + 深挖 lead；不命中能力不变；
- 护栏不动：3 轮 / 10 条 / 普通材料 12000 字（省窗）照旧；
- 词表防误触发：泛用短词（「盘」「方案」）永不进表。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deskmate.bots import discussion, rooms, runtime
from deskmate.bots.materials import RoomMaterials
from deskmate.bots.reading import BookShelf, make_reading_tools


# ------------------------------------------------------------- is_deep_dig

@pytest.mark.parametrize(
    "text",
    [
        "深挖一下烧仓的完整方案",
        "你们盘一下龙山仓怎么烧",
        "盘个方案给我",
        "局势盘全，出个完整方案",
        "先读全书，然后系统方案给我",
        "帮我通读一遍再想",
        "给我一个作战方案",
        "这个局你盘清楚再说话",
        "把防守盘明白",
        "全文盘一遍",
        "给我一套系统性的方案",
    ],
)
def test_is_deep_dig_hits_explicit_markers(text: str) -> None:
    assert discussion.is_deep_dig(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "烧仓这段接下来怎么写",
        "给三个方向",
        "我觉得这段的地盘感很好",   # 「盘」单字不触发
        "盘问那段对话写得如何",     # 「盘问」不触发
        "方案呢",                   # 「方案」单用不触发
        "夜不收的走向和结局",
        "把这段读一遍给我看看",     # 「读一遍」指当前段落，不是全书
        # 2026-11-08 晚修正：这些是**开脑洞**的自然说法，不是要系统级方案。
        # 深挖命中会给「盘局势 → 支点 → 分层方案」的收敛作业指令，把开脑洞
        # 推成方案就是 16:57 专家那轮的翻车方式 ⇒ 必须留给默认开脑洞档。
        "你可以先读原稿，然后帮我想一下，在龙山仓铁板一块的情况下，怎么才能完成任务。我需要脑洞大开的安排和设计",
        "我需要脑洞大开的安排和设计",
        "先读原稿，从头到尾",
        "",
    ],
)
def test_is_deep_dig_no_false_positive(text: str) -> None:
    assert discussion.is_deep_dig(text) is False


def test_deep_dig_known_light_false_positive_is_documented() -> None:
    """已知轻微误触发（11-08 取舍，明示不隐瞒）：「系统性」已收编进词表。

    「这段写得有点系统性偏执」这类说法会触发深挖。取舍依据：写作群语境里
    「系统性」出现在用户消息上几乎全是「要系统级方案／系统梳理」；误触发
    的代价是**多花一次深挖的钱**，不是质量损失（漏触发的代价才是质量——
    11-08 真机就是漏触发）。若目测误触发变多，收紧办法是把「系统性」换成
    「系统性的方案」，一行改动 + 本例反转。
    """
    assert discussion.is_deep_dig("这段写得有点系统性偏执") is True  # 已知，接受


def test_deep_dig_wordlist_has_no_ambiguous_short_markers() -> None:
    """词表必须是**带限定的长标记**：防泛用词误触发（成本事故）。

    「盘」「方案」这类裸泛用词会把大量普通发言误判成深挖。允许 2 字无歧义词
    （「深挖」「通读」），但裸的「盘」「方案」「读」绝不许出现。
    """
    banned = {"盘", "方案", "读", "书", "深", "挖", "想", "写"}
    for marker in discussion._DEEP_DIG_MARKERS:
        assert len(marker) >= 2, f"深挖标记太短，易误触发：{marker!r}"
        assert marker not in banned, f"泛用词不许进词表：{marker!r}"


# ------------------------------------------------------------- build_prompt

def test_build_prompt_deep_lead_carries_methodology() -> None:
    p = runtime.build_prompt("深挖一下烧仓", None, deep=True)
    # 作业路径四件套：盘局势 → 隐性支点 → 系统自己裂开 → 回收伏笔
    assert "盘局势" in p or "盘清局势" in p
    assert "隐性支点" in p
    assert "自己裂开" in p
    assert "伏笔" in p
    # 深挖解除平常的长度约束
    assert "写透为止" in p
    assert "深挖任务" in p


def test_build_prompt_normal_lead_unchanged_by_deep_flag() -> None:
    """普通路径（deep=False）的 lead 与旧版一致 —— 默认态不动。"""
    history = [{"author": "historian", "author_role": "bot", "text": "前文观点"}]
    p1 = runtime.build_prompt("用户原话", history, round_index=1)
    assert "请直接以你自己的身份发言。" in p1
    p2 = runtime.build_prompt("用户原话", history, round_index=2)
    assert "这是第 2 轮接话" in p2
    # 深挖标志缺席时，普通 lead 一个字不提「深挖」
    assert "深挖" not in p1 and "深挖" not in p2


# ------------------------------------------------------------- 材料：全书默认

def _make_shelf(tmp_path: Path) -> BookShelf:
    book = tmp_path / "books" / "16.测试书"
    book.mkdir(parents=True, exist_ok=True)
    # 一份 3 万字的正文（远超旧 12000 预算，尾部是检验点）
    (book / "正文.txt").write_text(
        "开头。" + "中段填充。" * 5000 + "全书末尾的钩子句。", encoding="utf-8"
    )
    return BookShelf(tmp_path / "books", max_chars=12_000)


def test_prepare_default_pack_is_whole_book(tmp_path: Path) -> None:
    """能力常备（2026-11-08 拍板「无论讨论什么都启动」）：材料默认全书。

    bot 从第一句发言起就站在全书认知上 —— 不再有「普通版只给开头、深挖版
    才给全书」的双轨；显式传 ``max_chars`` 仍可收紧（测试／特殊场景）。
    """
    shelf = _make_shelf(tmp_path)
    mats = RoomMaterials(shelf)  # 不传 max_chars → 默认全书
    pack = mats.prepare("r1", "读《测试书》")
    assert pack is not None
    assert "钩子句" in pack.prompt_block(), "默认材料必须装下全书尾部"


def test_prepare_explicit_budget_still_caps_but_keeps_stop_point(tmp_path: Path) -> None:
    """显式收紧仍然生效（护栏可降不可破），但收紧的是**总字数**不是结尾。

    2026-11-08 读稿层新语义：预算不够时按「开头 + 停笔点」装，结尾必须保留
    （作者写到哪儿才是脑洞的落点）；中间缺口在索引里如实列出。
    """
    shelf = _make_shelf(tmp_path)
    mats = RoomMaterials(shelf, max_chars=12_000)
    pack = mats.prepare("r1", "读《测试书》")
    assert pack is not None
    block = pack.prompt_block()
    assert "读取上限 12000" in block
    assert "钩子句" in block, "停笔点（结尾）必须保留"
    assert "未加载" in pack.index, "中间缺口要如实标出来"


def test_prepare_reuses_pack_without_title(tmp_path: Path) -> None:
    """没提书名时沿用该群已有材料（含新鲜度校验），拿不到不猜路径。"""
    shelf = _make_shelf(tmp_path)
    mats = RoomMaterials(shelf)
    first = mats.prepare("r2", "读《测试书》")
    assert first is not None
    assert mats.prepare("r2", "接着聊") is first
    assert mats.prepare("r3", "随便聊聊") is None


def test_drop_clears_pack(tmp_path: Path) -> None:
    shelf = _make_shelf(tmp_path)
    mats = RoomMaterials(shelf)
    mats.prepare("r3", "读《测试书》")
    mats.drop("r3")
    assert mats.get("r3") is None


# ------------------------------------------------------------- 读稿工具预算

def test_reading_tools_default_is_whole_book(tmp_path: Path) -> None:
    """能力常备（2026-11-08 拍板「不能靠触发词」）：默认工具一次全书。

    显式收紧（``max_chars=``）仅供测试/特殊场景 —— 深挖不再是工具的专属通道。
    """
    shelf = _make_shelf(tmp_path)
    default_tools = make_reading_tools(shelf)
    tight_tools = make_reading_tools(shelf, max_chars=12_000)
    read_default = next(t for t in default_tools if t.__name__ == "read_book")
    read_tight = next(t for t in tight_tools if t.__name__ == "read_book")
    out_default = read_default("测试书")
    out_tight = read_tight("测试书")
    assert "钩子句" in out_default, "默认工具必须一次读到全书尾部（能力常备）"
    assert "钩子句" in out_tight, "显式收紧只收总字数：停笔点优先保留"
    assert "读取上限 12000" in out_tight
    assert "本次未加载" in out_tight
    assert len(out_tight) < len(out_default)


# ------------------------------------------------------------- engine 预算

class _SilentProvider:
    """离线 provider：不产发言，只为起 engine 验证预算与缓存隔离。"""

    def capabilities(self, model: str) -> Any:
        from coworker.providers.base import ModelCapabilities

        return ModelCapabilities()

    def complete(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import AssistantTurn

        return AssistantTurn(text="ok")

    def stream(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import AssistantTurn, StreamChunk

        yield StreamChunk(turn=AssistantTurn(text="ok"))


def _runtime(tmp_path: Path) -> runtime.BotRuntime:
    return runtime.BotRuntime(
        books_root=tmp_path / "books",
        workspace_root=tmp_path / "ws",
        provider=_SilentProvider(),
        model="test-model",
    )


def test_engine_single_track_capability_always_on(tmp_path: Path) -> None:
    """单一 engine、预算常备（2026-11-08 拍板后不再有深挖双轨）。

    - 同一 (room, bot) 只有一份 engine，speak 的 deep 参数不再分轨；
    - 工具循环统一 ``_DEFAULT_MAX_ITERATIONS``（12）—— 上限是「允许」不是
      「消耗」，普通发言不调工具就是 0 轮，能力常备不等于成本常备；
    - read_book 由 engine 挂的工具集决定预算，默认全书。
    """
    rt = _runtime(tmp_path)
    try:
        eng = rt.engine("brainstormer", room_id="r4")
        assert eng is rt.engine("brainstormer", room_id="r4"), "engine 缓存唯一"
        iters = int(getattr(eng, "max_iterations", 0))
        assert iters >= 12, f"统一迭代上限应 ≥12（能力常备），实际 {iters}"
        # 工具面：read_book 默认全书
        tools = eng.registry._tools if hasattr(eng, "registry") else {}
        read_spec = tools.get("read_book") if isinstance(tools, dict) else None
        assert read_spec is not None, "read_book 必须在工具面"
    finally:
        rt.close()


# ------------------------------------------------------------- rooms 贯通

class _FakeRuntime:
    """记录 speak 参数的假 runtime，验证 deep 只落在第 1 轮。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def speak(self, bot_id, text, **kw) -> str:  # noqa: ANN001, ANN003
        self.calls.append({"bot_id": bot_id, **kw})
        return f"[{bot_id}] 说"

    async def speak_stream(self, bot_id, text, **kw):  # noqa: ANN001, ANN003
        self.calls.append({"bot_id": bot_id, **kw})
        yield {"type": "final", "text": f"[{bot_id}] 说"}


def _store(tmp_path: Path, tag: str = "base") -> rooms.RoomStore:
    import os

    shelf = _make_shelf(tmp_path)
    os.environ.setdefault("DESKMATE_BOT_PARALLEL_FIRST_ROUND", "0")  # 串行好断言
    return rooms.RoomStore(
        base_dir=tmp_path / tag,
        limits=discussion.DiscussionLimits(max_rounds=3, max_messages=10),
        materials=RoomMaterials(shelf, max_chars=12_000),
    )


def test_say_deep_only_first_round(tmp_path: Path) -> None:
    store = _store(tmp_path)
    fake = _FakeRuntime()
    try:
        store.create_room("深挖烧仓", ["brainstormer", "historian"])
        room = store.list_rooms()[0]
        out = store.say(
            room.room_id, "深挖一下《测试书》烧仓的完整方案", runtime=fake
        )
        assert out["replies"], "fake 路径应有回复"
        rounds = {}
        for c in fake.calls:
            rounds.setdefault(c.get("round_index"), []).append(c.get("deep"))
        assert rounds.get(1) == [True, True], "第 1 轮被唤醒者全部深挖"
        assert all(d is False for r, ds in rounds.items() if r > 1 for d in ds), (
            "第 2 轮起必须恢复普通预算"
        )
        # 深挖发言也要落库（author_role=bot），轮次护栏照旧
        assert out["bounded"] in (None, "max_rounds")
    finally:
        store.close()


def test_say_normal_untouched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    fake = _FakeRuntime()
    try:
        store.create_room("普通群", ["brainstormer", "historian"])
        room = store.list_rooms()[0]
        store.say(room.room_id, "烧仓这段接下来怎么写", runtime=fake)
        assert fake.calls, "应有发言"
        assert all(c.get("deep") is False for c in fake.calls), "普通发言零深挖"
    finally:
        store.close()


def test_say_stream_start_event_reports_deep(tmp_path: Path) -> None:
    import asyncio

    store = _store(tmp_path)
    fake = _FakeRuntime()
    try:
        store.create_room("流式深挖", ["brainstormer", "historian"])
        room = store.list_rooms()[0]

        async def _collect() -> list[dict[str, Any]]:
            events = []
            async for ev in store.say_stream(
                room.room_id,
                "盘一下《测试书》烧仓",
                runtime=fake,
                rounds=1,
            ):
                events.append(ev)
            return events

        events = asyncio.run(_collect())
        starts = [e for e in events if e.get("type") == "start"]
        assert starts and starts[0].get("deep") is True, "start 事件必须报 deep"
        assert all(c.get("deep") is True for c in fake.calls), "流式路径第 1 轮深挖贯通"
    finally:
        store.close()


# ------------------------------------------------------------- 一对一作业形态

def test_build_prompt_solo_lead_is_assignment_not_chat(tmp_path: Path) -> None:
    """solo lead 是作业指令：单独对话、直接交结果，不带群聊形态约束。"""
    p = runtime.build_prompt("龙山仓怎么烧", None, solo=True)
    assert "作者对你说" in p, "solo 的主语是作者对它，不是『群里刚有人说』"
    assert "单独对话" in p
    assert "写透为止" in p
    assert "过程汇报" in p, "教它直接交结果、不写过程"
    assert "群里刚有人说" not in p


def test_solo_guidance_keeps_redlines_drops_chat_form() -> None:
    """solo guidance：红线保留，群聊形态约束卸掉。"""
    g = runtime._SOLO_GUIDANCE
    # 红线（一字不减）
    assert "只读" in g and "不写文件" in g
    assert "绝不编造" in g and "绝不问用户要路径" in g
    assert "不生成纪要" in g
    assert "署名" in g
    # 形态约束卸掉（群聊版的这些话不许出现在 solo 版）
    for banned in ("三五句", "像在群里说话", "站队", "接话"):
        assert banned not in g, f"solo 版不该有群聊形态约束：{banned}"
    # 长短由题定（不是由群规定）
    assert "该三千字就三千字" in g


def test_engine_solo_is_separate_instance_same_budget(tmp_path: Path) -> None:
    """solo engine 独立缓存（群聊/单聊两副样子互不污染），预算一致。"""
    rt = _runtime(tmp_path)
    try:
        group_eng = rt.engine("brainstormer", room_id="r9")
        solo_eng = rt.engine("brainstormer", room_id="r9", solo=True)
        assert group_eng is not solo_eng, "solo 必须是独立 engine"
        assert solo_eng is rt.engine("brainstormer", room_id="r9", solo=True)
        assert group_eng is rt.engine("brainstormer", room_id="r9")
        assert int(getattr(solo_eng, "max_iterations", 0)) == int(
            getattr(group_eng, "max_iterations", 0)
        ), "预算一致：solo 只是场合差异，不是能力差异"
    finally:
        rt.close()


def test_say_single_mention_is_solo_multi_is_group(tmp_path: Path) -> None:
    """@ 单独一位 = solo；不 @（多人群）= 群聊；单成员群不 @ 也 = solo。"""
    store = _store(tmp_path)
    fake = _FakeRuntime()
    try:
        store.create_room("solo 判定", ["brainstormer", "historian"])
        room = store.list_rooms()[0]
        # @ 一位 → solo（@ 英文 handle，resolve 语义最稳）
        fake.calls.clear()
        store.say(room.room_id, "@brainstormer 龙山仓怎么烧", runtime=fake)
        assert fake.calls and all(c.get("solo") is True for c in fake.calls), (
            "@ 单独一位必须进作业形态"
        )
        assert all(c.get("deep") is False for c in fake.calls), "无深挖词不加强指令"
        # 不 @ 双成员群 → 群聊形态
        fake.calls.clear()
        store.say(room.room_id, "龙山仓怎么烧", runtime=fake)
        assert fake.calls and all(c.get("solo") is False for c in fake.calls), (
            "多人群不 @ 是群聊形态"
        )
    finally:
        store.close()
    # 单成员群：不 @ 也只有一位 → solo（独立 base_dir，别复用上面的库）
    store2 = _store(tmp_path, tag="base2")
    fake2 = _FakeRuntime()
    try:
        store2.create_room("单人群", ["brainstormer"])
        room2 = store2.list_rooms()[0]
        store2.say(room2.room_id, "龙山仓怎么烧，给个完整方案", runtime=fake2)
        assert fake2.calls and all(
            c.get("solo") is True and c.get("deep") is True for c in fake2.calls
        ), "单成员群天然 solo；且 solo 与 deep 可叠加（单独深聊）"
    finally:
        store2.close()


def test_solo_is_single_round_no_forced_multi(tmp_path: Path) -> None:
    """solo 天然单轮：一位 bot 没有可接话的对象，multi_round=False。"""
    store = _store(tmp_path)
    fake = _FakeRuntime()
    try:
        store.create_room("单轮", ["brainstormer"])
        room = store.list_rooms()[0]
        out = store.say(room.room_id, "@brainstormer 盘一下怎么烧", runtime=fake)
        assert out["rounds"] == 1, "solo 一轮即收口，不硬凑多轮"
        assert out["bounded"] is None
    finally:
        store.close()
