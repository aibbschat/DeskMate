"""§17 回归 —— 群共享材料接入小桔既有沉淀（11-06 21:25 萧哥定调）。

萧哥判断：书桶补集到 155 样本 / 435 万字，这些是**她每天读过、已经落盘的现成
经验**，用它补群聊「不知道作者今天写到哪」的缺陷，与圆桌「现场跑 LLM 抽取的
有损摘要」性质不同 ⇒ **不触发 §16.2(5) 红线，红线原文一字不改**。

本文件锁死：经验层是「原样搬运既有文件」，不是生成物；只增不减；零 LLM；
只读；改稿自动失效；备份/快照/db 不得混入。

先红后绿：旧代码应红在
- ``prompt_block()`` 里完全没有简报 / 圣典 / 最新正文 / 文风（0 命中）；
- ``_fingerprint`` 不覆盖 ``.deskmate`` ⇒ 新简报落盘后缓存不换新；
- ``read_book(chapter="CHARACTERS")`` 补读不到圣典。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from deskmate.bots import materials, reading

_NOVEL_LINE = "万历二十一年二月二十日，大风从海上袭来，宁靖立在雪地里。"
_CONTRACT_LINE = "甲方（授权方）：萧盛。本合同约定 AI 短剧改编权。"

# 小桔沉淀里的特征串（模拟真实文件的可辨识内容）
_BRIEF_TODAY = "1486 段 / 97472 字符，第十一章《烧粮仓》，日线 4000-5000 未达"
_BRIEF_OLD = "旧简报：1198 段，请勿采用"
_FEED_HEAD = "第十一章 烧粮仓（开头）孔大贵推门进来。"
_FEED_TAIL = "今晚老子就拿你们的命给孔百户献祭！——停笔于此，未完"
_FEED_OLD_TAIL = "旧稿尾部：铜雀渡的白烟散了（昨天停笔处）"
_CANON_MARK = "孔大贵：夜不收军校，第十章死于江心（正文引文：略）"
_BACKUP_MARK = "旧版错误事实：孔大贵其实没有死"
_STYLE_SUMMARY = "历史悬疑小说，半文半白的说书人腔调，幽默生动，节奏明快。"
_STYLE_FEATURE = "多用四字格和短句，整散结合，对仗偶现"
_RULING_TEXT = "作者裁定：三人一个也不能死，带李昖回营。"


@pytest.fixture()
def shelf_dir(tmp_path: Path) -> Path:
    """还原真实书库形态：顶层正文 + .deskmate 逐日沉淀 + 备份/快照/db 干扰项。"""
    book = tmp_path / "16.夜不收"
    book.mkdir()
    (book / "《夜不收》小说 AI 短剧改编授权协议.txt").write_text(_CONTRACT_LINE, encoding="utf-8")
    (book / "夜不收 粗纲.md").write_text("粗纲：烧仓是主线。", encoding="utf-8")
    (book / "夜不收：绝命救援.txt").write_text(_NOVEL_LINE * 3, encoding="utf-8")

    d = book / ".deskmate"
    d.mkdir()
    (d / "CHARACTERS.md").write_text(_CANON_MARK, encoding="utf-8")
    (d / "NOTES.md").write_text("伏笔：信号弹交到宁靖手里；白烟没有粮车的焦味。", encoding="utf-8")
    (d / "OUTLINE.md").write_text("主线：碧蹄馆→龙山仓→烧粮仓。", encoding="utf-8")
    (d / "TIMELINE.md").write_text("万历二十一年二月二十日。", encoding="utf-8")
    (d / "AUTHOR_RULING.md").write_text(_RULING_TEXT, encoding="utf-8")
    (d / "进度简报-2026-11-05.md").write_text(_BRIEF_OLD, encoding="utf-8")
    (d / "进度简报-2026-11-06.md").write_text(_BRIEF_TODAY, encoding="utf-8")
    (d / "简报-2026-11-02-夜不收进度.md").write_text("最早的简报，勿采用。", encoding="utf-8")

    feeds = d / "style_feeds"
    feeds.mkdir()
    (feeds / "新增正文_2026-11-05.txt").write_text(_FEED_OLD_TAIL, encoding="utf-8")
    (feeds / "新增正文_2026-11-06.txt").write_text(
        _FEED_HEAD + "中间正文。" * 40 + _FEED_TAIL, encoding="utf-8"
    )

    backup = d / "_backup_20261105_1700"
    backup.mkdir()
    (backup / "CHARACTERS.md").write_text(_BACKUP_MARK, encoding="utf-8")
    (backup / "进度简报-2026-11-06.md").write_text(_BACKUP_MARK, encoding="utf-8")
    (d / "_snapshots").mkdir()
    (d / "entities.db").write_bytes(b"\x00\x01BINARY-NOT-TEXT\xff")
    return tmp_path


def _fake_style_profile() -> dict:
    """与真实 style_profile 同构，测试不碰用户真实 deskmate.db。"""
    return {
        "summary": _STYLE_SUMMARY,
        "features": {
            "sentence_style": _STYLE_FEATURE,
            "register": "半文半白的明清拟古白话，掺入现代语词。",
            "rhythm": "节奏明快，以动作和对话推进。",
            "word_preference": "爱用史白/官职/市井语汇。",
            "taboos": "忌现代网络语。",
        },
        "buckets": {
            "manuscript": {
                "updates": 155,
                "chars": 4_354_384,
                "metrics": {
                    "n": 155,
                    "sentences": {"short_pct": 15.25, "avg_len": 35.74},
                    "four_char": {"pct_of_chunks": 13.56},
                    "dialogue_pct": 21.03,
                    "classical_per_1k": 32.26,
                },
            }
        },
    }


#: 经验层各节全开（旧行为）；默认只开 "ruling"，见
#: test_default_materials_drop_briefing_canon_style_keep_ruling。
_ALL_SECTIONS = ("ruling", "style", "brief", "feed", "canon")


def _materials(shelf_dir: Path, **kwargs) -> materials.RoomMaterials:
    kwargs.setdefault("experience_sections", _ALL_SECTIONS)
    return materials.RoomMaterials(
        reading.BookShelf(shelf_dir, max_chars=8000),
        style_loader=_fake_style_profile,
        **kwargs,
    )


# ------------------------------------------------------- 1. 经验层四段齐备

def test_experience_block_contains_today_briefing_and_latest_stop_point(shelf_dir) -> None:
    """群里问「接下来怎么写」，bot 必须知道今天写到哪一章、停在哪句。"""
    pack = _materials(shelf_dir).prepare("r1", "先读《夜不收》，给三个方向")
    assert pack is not None
    block = pack.prompt_block()
    assert _BRIEF_TODAY in block, "最新进度简报必须进群共享材料"
    assert _FEED_TAIL in block, "最新逐字留档的尾部＝停笔点，必须原样在"
    assert _FEED_HEAD in block, "逐字留档是原文，不是摘要"


def test_experience_block_contains_canonical_facts_and_style(shelf_dir) -> None:
    """圣典（带引文事实）与 155 样本文风指纹，都是她已核准的现成经验。"""
    pack = _materials(shelf_dir).prepare("r1", "先读《夜不收》")
    assert pack is not None
    block = pack.prompt_block()
    assert _CANON_MARK in block, "四份圣典必须进材料（事实基准）"
    assert _STYLE_SUMMARY in block
    assert _STYLE_FEATURE in block
    assert "155" in block and "4354384" in block.replace(",", ""), "文风统计口径要标清来源"
    assert "跨书" in block or "非《夜不收》专属" in block or "整体文风" in block, \
        "文风是跨书全局统计，不许 bot 当成某本书的设定"


def test_only_latest_briefing_and_latest_feed_are_used(shelf_dir) -> None:
    """逐日沉淀只取最新一份，不把 5 份简报 / 8 份留档全堆进上下文。"""
    pack = _materials(shelf_dir).prepare("r1", "先读《夜不收》")
    assert pack is not None
    block = pack.prompt_block()
    assert _BRIEF_OLD not in block
    assert _FEED_OLD_TAIL not in block
    assert "最早的简报" not in block


def test_backup_snapshots_and_db_are_never_mixed_in(shelf_dir) -> None:
    """_backup_/_snapshots 里有同名旧圣典；db 被守卫白名单拒绝，一律不得混入。"""
    pack = _materials(shelf_dir).prepare("r1", "先读《夜不收》")
    assert pack is not None
    block = pack.prompt_block()
    assert _BACKUP_MARK not in block, "昨天的旧事实不能裁判今天的稿子"
    assert "BINARY-NOT-TEXT" not in block
    assert "entities.db" not in block


def test_original_raw_text_layer_is_not_removed(shelf_dir) -> None:
    """只增不减：§16 已验收的正文优先原文层与索引一个字不删。"""
    pack = _materials(shelf_dir).prepare("r1", "先读《夜不收》")
    assert pack is not None
    block = pack.prompt_block()
    assert _NOVEL_LINE in block
    assert _CONTRACT_LINE not in block
    assert "夜不收：绝命救援.txt" in block
    assert "chapter 不是路径" in pack.index


# ------------------------------------------------------- 2. 缓存与新鲜度

def test_experience_fingerprint_invalidates_when_new_briefing_lands(shelf_dir) -> None:
    """小桔 16:00 落新简报后，下一句哪怕只是「接着聊」也自动换新。"""
    m = _materials(shelf_dir)
    first = m.prepare("r1", "先读《夜不收》")
    assert first is not None
    p = shelf_dir / "16.夜不收" / ".deskmate" / "进度简报-2026-11-06.md"
    p.write_text(_BRIEF_TODAY + "\n新增：孔百户的腰刀在渡口出现。", encoding="utf-8")
    second = m.prepare("r1", "接着聊")
    assert second is not None
    assert second.fingerprint != first.fingerprint, "指纹必须覆盖 .deskmate 下的文件"
    assert "孔百户的腰刀" in second.prompt_block()


def test_experience_fingerprint_invalidates_when_style_profile_updates(shelf_dir, monkeypatch) -> None:
    """书桶补集后文风指纹变了，材料也要跟着换（不能拿旧文风发方向）。"""
    state = {"n": 155}

    def loader():
        d = _fake_style_profile()
        d["buckets"]["manuscript"]["updates"] = state["n"]
        return d

    m = materials.RoomMaterials(
        reading.BookShelf(shelf_dir, max_chars=8000), style_loader=loader
    )
    first = m.prepare("r1", "先读《夜不收》")
    state["n"] = 156
    second = m.prepare("r1", "接着聊")
    assert first is not None and second is not None
    assert second.fingerprint != first.fingerprint


def test_no_deskmate_dir_does_not_touch_real_style_store(shelf_dir, monkeypatch) -> None:
    """没有 .deskmate 沉淀的书：不读用户真实库，测试/运行都不产生隐式依赖。"""
    def boom():
        raise AssertionError("无沉淀时不应读 style_profile")

    book = shelf_dir / "17.没有沉淀的书"
    book.mkdir()
    (book / "正文.txt").write_text("一本没在跟的书。", encoding="utf-8")
    m = materials.RoomMaterials(
        reading.BookShelf(shelf_dir, max_chars=8000), style_loader=boom
    )
    pack = m.prepare("r1", "先读《没有沉淀的书》")
    assert pack is not None
    assert "一本没在跟的书" in pack.prompt_block()


# ------------------------------------------------------- 3. 补读能力

def test_read_book_can_fetch_canonical_file_by_name(shelf_dir) -> None:
    """经验层被预算截断后，bot 要能用 chapter 按名补读圣典（不靠硬编）。"""
    shelf = reading.BookShelf(shelf_dir, max_chars=8000)
    out = shelf.read("《夜不收》", chapter="CHARACTERS")
    assert _CANON_MARK in out, "chapter 点名必须能读到 .deskmate 下的圣典"
    assert _BACKUP_MARK not in out, "备份目录不能进补读候选"


def test_read_book_with_empty_chapter_keeps_original_behaviour(shelf_dir) -> None:
    """留空行为一字不变：仍只读顶层原文，不把经验文件挤进正文优先级。"""
    shelf = reading.BookShelf(shelf_dir, max_chars=8000)
    out = shelf.read("《夜不收》")
    assert _NOVEL_LINE in out
    assert _CANON_MARK not in out
    assert _BRIEF_TODAY not in out


def test_index_tells_bots_how_to_fetch_experience_files(shelf_dir) -> None:
    """索引要列出可补读的沉淀文件名，否则 bot 不知道还能点名要什么。"""
    pack = _materials(shelf_dir).prepare("r1", "先读《夜不收》")
    assert pack is not None
    assert "CHARACTERS" in pack.index
    assert "进度简报" in pack.index


# ------------------------------------------------------- 4. 红线与边界

def test_experience_layer_does_no_llm_and_generates_no_summary(shelf_dir) -> None:
    """硬红线：现成经验是原样搬运，不是现场 LLM 抽取/摘要/纪要。"""
    from deskmate.bots import experience

    src = Path(experience.__file__).read_text(encoding="utf-8")
    # 只看代码依赖 / 函数定义，不看说明文字
    code = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith(("#", '"', "'"))
    )
    for banned in ("build_bible", "chat_memory", "llm_channels", "OpenAI(", "route(", "def summarize", "def digest"):
        assert banned not in code
    m_src = Path(materials.__file__).read_text(encoding="utf-8")
    for banned in ("build_bible", "chat_memory", "llm_channels", "OpenAI(", "route("):
        assert banned not in m_src


def test_red_line_section_16_2_5_is_unchanged() -> None:
    """萧哥判定「与红线无关」⇒ §16.2(5) 原文一字不改，只新增 §17 澄清适用范围。"""
    doc = Path("docs/bot-mode-redesign.md").read_text(encoding="utf-8")
    assert "5. **不压缩材料**：共享的是**原文分块 + 文件索引**，绝不做 LLM 抽取／摘要／圣经／纪要" in doc
    assert "## 17. 群共享材料扩成两层" in doc
    assert "红线原文一字不改" in doc


def test_experience_budget_is_bounded_and_configurable(shelf_dir) -> None:
    """435 万字绝不能整桶塞进 prompt；预算独立、可调、确定性截断。"""
    # 造一份超长圣典，让 800 与 20000 两个预算真正分出差别（fixture 原件太短）。
    canon = shelf_dir / "16.夜不收" / ".deskmate" / "CHARACTERS.md"
    canon.write_text(_CANON_MARK + "\n" + "考据条目。" * 900, encoding="utf-8")
    small = _materials(shelf_dir, experience_chars=800).prepare("r1", "先读《夜不收》")
    large = _materials(shelf_dir, experience_chars=20_000).prepare("r2", "先读《夜不收》")
    assert small is not None and large is not None
    assert len(small.prompt_block()) < len(large.prompt_block())
    assert len(small.prompt_block()) < 8_000 + 800 + 3_000, "经验层不得越过独立预算"
    assert _FEED_TAIL in small.prompt_block(), "预算再小也要保住停笔点（优先级最高）"
    assert _BRIEF_TODAY in small.prompt_block()
    assert "考据条目。" * 100 not in small.prompt_block(), "超预算的圣典必须被确定性截断"


def test_shared_material_is_byte_identical_across_bots(shelf_dir) -> None:
    """群里放一份：同一 room 的每位 bot 拿到完全相同的字节。"""
    pack = _materials(shelf_dir).prepare("r1", "先读《夜不收》")
    assert pack is not None
    assert pack.prompt_block() == pack.prompt_block()
    assert "# 群里已经共享的书稿材料" in pack.prompt_block()


def test_default_materials_drop_briefing_canon_style_keep_ruling(shelf_dir) -> None:
    """2026-11-08 读稿层：默认只留作者裁定，简报／圣典／文风只进索引。

    萧哥原话「以正文为准」。简报／圣典可能过时且与正文打架（实测
    「进仓路线只有码头正门一条」 vs 正文两道门），默认喂进去会把脑洞锚死；
    作者裁定是他本人最新口径，留着防旧死序／旧结局回流。
    """
    pack = materials.RoomMaterials(
        reading.BookShelf(shelf_dir, max_chars=8000),
        style_loader=_fake_style_profile,
    ).prepare("r1", "先读《夜不收》，给三个方向")
    assert pack is not None
    block = pack.prompt_block()
    assert _RULING_TEXT in block, "作者裁定默认保留（最高效力口径）"
    assert _CANON_MARK not in block, "圣典默认不进上下文"
    assert _BRIEF_TODAY not in block, "简报默认不进上下文"
    assert _STYLE_SUMMARY not in block, "文风默认不进上下文"
    assert "进度简报-2026-11-06.md" in pack.index, "但仍在索引里，可点名补读"
    assert "CHARACTERS.md" in pack.index
