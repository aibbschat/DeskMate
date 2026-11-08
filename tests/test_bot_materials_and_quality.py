"""整体方案 §16 回归 —— 保质硬前提下的省时省钱（11-06 20:43）。

萧哥拍板：省时省钱只能在**不降单次输出质量**的前提下做，不能兼得就选保质。
本文件逐条锁死质量不变量，并验「群级共享材料 + 第 1 轮并发」的语义边界。

先红后绿：旧代码应红在
- 材料第一份是授权协议（正文优先缺失）；
- ``build_prompt`` 不接受 materials；
- 第 1 轮串行（peak 并发 = 1）。
"""
from __future__ import annotations

import asyncio
import inspect
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from coworker.teams.chat import ChatStore
from deskmate.bots import materials, reading, rooms, runtime

_NOVEL_LINE = "万历二十一年二月二十日，大风从海上袭来，宁靖立在雪地里。"
_CONTRACT_LINE = "甲方（授权方）：萧盛。本合同约定 AI 短剧改编权。"


@pytest.fixture()
def shelf_dir(tmp_path: Path) -> Path:
    """还原真实书库形态：文件名排序把合同排第一、230KB 正文排很后。"""
    book = tmp_path / "16.夜不收"
    book.mkdir()
    (book / "《夜不收》小说 AI 短剧改编授权协议.txt").write_text(_CONTRACT_LINE, encoding="utf-8")
    (book / "夜不收 粗纲.md").write_text("粗纲：烧仓是主线。", encoding="utf-8")
    (book / "夜不收-人物画像.md").write_text("宁靖：夜不收。", encoding="utf-8")
    (book / "夜不收-后半部大纲.md").write_text("后半部大纲。", encoding="utf-8")
    (book / "夜不收：绝命救援.txt").write_text(_NOVEL_LINE * 3, encoding="utf-8")
    (book / "夜不行：绝命救援 简介.md").write_text("简介。", encoding="utf-8")
    (book / "目录.md").write_text("目录。", encoding="utf-8")
    return tmp_path


def _materials(shelf_dir: Path) -> materials.RoomMaterials:
    return materials.RoomMaterials(reading.BookShelf(shelf_dir, max_chars=8000))


# ---------------------------------------------------------------- 1. 材料层

def test_shared_material_starts_with_novel_text_not_contract(shelf_dir: Path) -> None:
    """质量事故红线：第一次「读《夜不收》」拿到的必须是小说正文，不是授权协议。"""
    pack = _materials(shelf_dir).prepare("r1", "先读《夜不收》，然后说说烧仓")
    assert pack is not None
    body = pack.prompt_block()
    assert body.index("夜不收：绝命救援.txt") < body.index("后半部大纲")
    assert _NOVEL_LINE in body, "共享的是原文分块，不是摘要/圣经"
    assert _CONTRACT_LINE not in body, "协议/合同不是创作材料，默认不喂"


def test_material_index_lists_every_file_and_labels_types(shelf_dir: Path) -> None:
    """索引必须列出全部文件（含没喂的），bot 才知道还能点名补读。"""
    pack = _materials(shelf_dir).prepare("r1", "先读《夜不收》")
    assert pack is not None
    for name in ("授权协议", "粗纲", "人物画像", "后半部大纲", "绝命救援", "简介", "目录"):
        assert name in pack.index
    assert "协议/简介/目录" in pack.index
    assert "chapter 不是路径" in pack.index


def test_material_cache_invalidates_when_manuscript_changes(shelf_dir: Path) -> None:
    """改稿后下一次发言自动换新，不需要用户记着说「重读」。"""
    m = _materials(shelf_dir)
    first = m.prepare("r1", "先读《夜不收》")
    p = shelf_dir / "16.夜不收" / "夜不收：绝命救援.txt"
    p.write_text(_NOVEL_LINE + "新增的一段：铜雀渡船家看见白烟。", encoding="utf-8")
    second = m.prepare("r1", "接着聊")
    assert first is not None and second is not None
    assert second.fingerprint != first.fingerprint
    assert "铜雀渡船家" in second.prompt_block()
    assert "铜雀渡船家" not in first.prompt_block()


def test_no_book_title_keeps_existing_room_material(shelf_dir: Path) -> None:
    """没提书名时沿用群里已放的那份；不猜、不回退到任意路径。"""
    m = _materials(shelf_dir)
    pack = m.prepare("r1", "先读《夜不收》")
    assert m.prepare("r1", "大家觉得呢") is pack
    assert m.prepare("r2", "随便聊聊") is None


def test_read_book_docstring_points_to_material_and_rejects_paths(shelf_dir: Path) -> None:
    """工具说明书的不变量（2026-11-08 能力常备化后重锁）。

    - §16 时代的「别留空碰运气」（留空撞合同）已随 §18 classify 修复 +
      本次全书预算而**过时**：留空现在就是读全部正文。锁的新不变量是
      「读多少由题决定」——既不逼全书也不禁全书，教模型自己判断；
    - 永不变的两条：引导先看群共享材料（省往返）；chapter 是文件名
      关键词、不是路径（防绝对路径注入）。
    """
    tools = reading.make_reading_tools(reading.BookShelf(shelf_dir, max_chars=8000))
    doc = next(t for t in tools if t.__name__ == "read_book").__doc__ or ""
    assert "读多少由题决定" in doc, "教模型按题判断，而不是代码开关或一刀切"
    assert "先看那份" in doc, "仍要引导先看群共享材料，省工具往返"
    assert "文件名关键词" in doc and "不是路径" in doc, "chapter 语义永不变"


# ---------------------------------------------------------------- 2. 质量不变量

def test_quality_no_reasoning_effort_anywhere_in_bot_runtime() -> None:
    """硬红线：bot 群绝不降思考强度（同题实测会丢结构性洞察）。"""
    src = Path(runtime.__file__).read_text(encoding="utf-8")
    assert "reasoning_effort" not in src
    body = inspect.getsource(rooms.RoomStore.say)
    assert "reasoning_effort" not in body


def test_quality_read_book_still_available_and_no_output_truncation(shelf_dir: Path) -> None:
    """真 Agent 自主补读权不削；发言正文不设字数上限。"""
    names = {t.__name__ for t in reading.make_reading_tools(reading.BookShelf(shelf_dir, max_chars=8000))}
    assert names == {"read_book", "list_books"}
    src = Path(rooms.__file__).read_text(encoding="utf-8")
    assert "reply[:600]" not in src and "text[:600]" not in src


def test_quality_shared_material_is_raw_text_not_summary(shelf_dir: Path) -> None:
    """共享的是原文分块，绝不做 LLM 抽取 / 圣经 / 纪要。"""
    src = Path(materials.__file__).read_text(encoding="utf-8")
    # 只看代码依赖，不看 docstring 里「绝不做摘要/纪要」的说明文字
    for banned in ("build_bible", "chat_memory", "llm_channels", "OpenAI(", "route(", "def summarize"):
        assert banned not in src
    pack = _materials(shelf_dir).prepare("r1", "先读《夜不收》")
    assert pack is not None and _NOVEL_LINE in pack.body


def test_build_prompt_accepts_materials_as_stable_prefix() -> None:
    """材料作为固定前缀注入；同一份材料给不同 bot 字节一致（缓存友好）。"""
    pack = materials.MaterialPack(book="夜不收", fingerprint="x", body="原文", index="索引")
    params = set(inspect.signature(runtime.build_prompt).parameters)
    assert "materials" in params
    a = runtime.build_prompt("给三个方向", None, materials=pack)
    b = runtime.build_prompt("给三个方向", None, materials=pack)
    assert a == b and a.startswith("# 群里已经共享的书稿材料")
    assert "给三个方向" in a


# ---------------------------------------------------------------- 3. 调度语义

class _ConcurrencyRuntime:
    """记录第 1 轮并发度与每位 bot 看到的 history 快照。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak = 0
        self.calls: list[dict[str, Any]] = []

    def speak(self, bot_id, text, *, history=None, **kwargs) -> str:
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            self.calls.append({
                "bot_id": bot_id,
                "round": kwargs.get("round_index"),
                "history": [m.get("text") for m in (history or [])],
                "materials": kwargs.get("materials"),
            })
        time.sleep(0.12)
        with self.lock:
            self.inflight -= 1
        # 第 1 轮真发言，第 2 轮起全 pass，让多轮收口
        return f"[{bot_id}] 第1轮发言" if kwargs.get("round_index") == 1 else "pass"

    async def speak_stream(self, bot_id, text, *, history=None, **kwargs):
        with self.lock:
            self.calls.append({"bot_id": bot_id, "round": kwargs.get("round_index")})
        out = f"[{bot_id}] 第1轮发言" if kwargs.get("round_index") == 1 else "pass"
        yield {"type": "final", "text": out}


def _pair(tmp_path: Path):
    cs = ChatStore(tmp_path / "chat.db")
    base = tmp_path / "store"
    base.mkdir(exist_ok=True)
    return cs, rooms.RoomStore(base, chat_store=cs)


def test_first_round_is_parallel_but_second_round_sees_all_replies(tmp_path, monkeypatch) -> None:
    """第 1 轮并发省墙钟；第 2 轮接话仍看得到完整前文（原语义不破）。"""
    monkeypatch.delenv("DESKMATE_BOT_PARALLEL_FIRST_ROUND", raising=False)
    cs, store = _pair(tmp_path)
    room = store.create_room("话题", ["historian", "lit-editor", "reader"])
    fake = _ConcurrencyRuntime()
    out = store.say(room.room_id, "大家给三个方向", runtime=fake)
    assert fake.peak >= 2, "第 1 轮应并发，否则 N 位仍是一位等一位"
    assert len(out["replies"]) == 3
    first_round = [c for c in fake.calls if c["round"] == 1]
    assert len(first_round) == 3
    assert all(c["history"] == first_round[0]["history"] for c in first_round), \
        "第 1 轮各 bot 看同一份群历史快照，独立出方向"
    later = [c for c in fake.calls if c["round"] == 2]
    if later:
        for m in ("[historian] 第1轮发言", "[lit-editor] 第1轮发言", "[reader] 第1轮发言"):
            assert m in later[0]["history"], "第 2 轮接话必须看得到全部第 1 轮发言"
    store.close(); cs.close()


def test_parallel_can_be_disabled_for_exact_serial_semantics(tmp_path, monkeypatch) -> None:
    """语义存疑时一键回退全串行，不必改代码。"""
    monkeypatch.setenv("DESKMATE_BOT_PARALLEL_FIRST_ROUND", "0")
    cs, store = _pair(tmp_path)
    room = store.create_room("话题", ["historian", "lit-editor", "reader"])
    fake = _ConcurrencyRuntime()
    store.say(room.room_id, "大家给三个方向", runtime=fake)
    assert fake.peak == 1
    store.close(); cs.close()


def test_shared_material_is_passed_to_every_bot(tmp_path, shelf_dir, monkeypatch) -> None:
    """群级备料一次，N 位 bot 都拿到同一份材料（不是各读各的）。"""
    monkeypatch.setenv("DESKMATE_BOT_BOOKS_ROOT", str(shelf_dir))
    cs, store = _pair(tmp_path)
    store.materials = _materials(shelf_dir)
    room = store.create_room("话题", ["historian", "lit-editor", "reader"])
    fake = _ConcurrencyRuntime()
    store.say(room.room_id, "先读《夜不收》，给三个方向", runtime=fake)
    packs = [c["materials"] for c in fake.calls if c["round"] == 1]
    assert len(packs) == 3 and all(p is not None for p in packs)
    assert all(p is packs[0] for p in packs), "全群共用同一份材料包"
    assert _NOVEL_LINE in packs[0].prompt_block()
    store.close(); cs.close()


def test_stream_first_round_emits_all_bots(tmp_path) -> None:
    """流式路径也走并发合并，每位 bot 的 final 都落库、done 正常收口。"""
    cs, store = _pair(tmp_path)
    room = store.create_room("话题", ["historian", "lit-editor"])

    async def collect():
        return [ev async for ev in store.say_stream(
            room.room_id, "大家说一句", runtime=_ConcurrencyRuntime()
        )]

    events = asyncio.run(collect())
    # 第 2 轮起 mock 回 pass，也会推 final 事件；只数第 1 轮的真发言
    finals = [ev for ev in events if ev.get("type") == "final" and ev.get("round") == 1]
    assert len(finals) == 2
    assert any(ev.get("type") == "done" for ev in events)
    assert len([m for m in cs.messages(room.room_id) if m.get("author_role") == "bot"]) == 2
    store.close(); cs.close()


def test_engine_error_detail_is_not_swallowed() -> None:
    """429 / 配额超限必须带真实原因冒出来，不能被吞成「bot 发言出错」。

    11-06 真机验收时 engine 返回 RateLimitError(AccountQuotaExceeded)，
    旧 ``_drive`` 把事件全 pass、``speak_stream`` 只找 ``message`` 字段，
    于是页面上只显示 bot 没说话 —— 差点被误判成模型弃权或质量退化。
    """
    from types import SimpleNamespace

    class _FakeEngine:
        async def run(self, prompt):
            yield SimpleNamespace(
                type=SimpleNamespace(value="error"),
                data={
                    "error": "Error code: 429 AccountQuotaExceeded",
                    "error_type": "RateLimitError",
                },
            )

    with pytest.raises(RuntimeError, match="AccountQuotaExceeded"):
        asyncio.run(runtime.BotRuntime._drive(_FakeEngine(), "x"))
    src = Path(runtime.__file__).read_text(encoding="utf-8")
    # 一次性发言与流式两条路径都要透出真实原因
    assert src.count('data.get("error")') >= 2
