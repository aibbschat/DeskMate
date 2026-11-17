"""读稿层回归（2026-11-08）：按章切分 / 停笔点优先 / 覆盖率诚实。

萧哥拍板原话：「以正文为准」「先修到能读到第十一章」。本组锁住：
- 书比预算长时，正文装「开头 + 结尾（停笔点）」，**不再从第一页硬砍到 80%**；
- 索引如实写「正文共 N 字 / M 章，本次加载哪几章、哪些没加载」，不许再谎称
  「正文已加载」；
- 能按章节 / 章节标题 / 区间点名补读，读到中间被略去的部分。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from deskmate.bots import materials, reading


# --------------------------------------------------------------------- 造书

def _make_book(tmp_path: Path, *, chapters: int = 12, per_chapter: int = 8_000) -> Path:
    """造一本有章节标题的长书：正文 9.6 万字，尾部有可辨识的停笔标记。"""
    book = tmp_path / "books" / "16.测试书"
    book.mkdir(parents=True)
    parts = []
    for i in range(1, chapters + 1):
        marker = f"章末标记{i}"
        filler = "填" * max(0, per_chapter - len(marker) - 20)
        parts.append(f"第{i}章：标题{i}\n第{i}章正文。{filler}{marker}")
    (book / "正文.txt").write_text("\n\n".join(parts), encoding="utf-8")
    return tmp_path / "books"


def _shelf(books_root: Path, *, max_chars: int = 12_000) -> reading.BookShelf:
    return reading.BookShelf(books_root, max_chars=max_chars)


# --------------------------------------------------------------------- 切章

def test_split_chapters_ignores_inline_mentions() -> None:
    text = (
        "第1章：起\n正文一，他想起第2章那句话，还不能算标题。\n"
        "第2章：承\n正文二\n"
        "第3章\n正文三"
    )
    chapters = reading.split_chapters(text)
    assert [c.label for c in chapters] == ["第1章", "第2章", "第3章"]
    assert chapters[0].title == "起"
    assert chapters[1].title == "承"


def test_split_chapters_falls_back_to_single_chapter() -> None:
    chapters = reading.split_chapters("没有章节标题的一段正文。")
    assert len(chapters) == 1
    assert chapters[0].label == "全文"


# --------------------------------------------------------------------- 停笔点

def test_default_read_keeps_stop_point_when_budget_is_small(tmp_path: Path) -> None:
    root = _make_book(tmp_path)
    out = _shelf(root, max_chars=12_000).read("测试书")
    assert "章末标记12" in out, "预算再小，结尾（停笔点）必须进来"
    assert "读取上限 12000" in out
    assert "未加载" in out, "中间被略去的部分要明说"
    assert "章末标记1" in out, "开头也要留一段（全局认知）"
    assert len(out) < 12_000 + 2_000, "总加载量受预算约束"


def test_read_book_chapter_selects_single_chapter(tmp_path: Path) -> None:
    root = _make_book(tmp_path)
    out = _shelf(root).read("测试书", chapter="第11章")
    assert "章末标记11" in out
    assert "章末标记12" not in out
    assert "第11章：标题11" in out


def test_read_book_matches_chapter_title(tmp_path: Path) -> None:
    root = _make_book(tmp_path)
    out = _shelf(root).read("测试书", chapter="标题9")
    assert "章末标记9" in out


def test_read_book_supports_chapter_range(tmp_path: Path) -> None:
    # 2026-11-16 起区间点名按章均分预算（见下方「多章点名预算总量封顶」组）：
    # 预算给足（3 章 × 8k < 30k）时仍能读到每章章尾；预算不足时诚实截断。
    root = _make_book(tmp_path)
    out = _shelf(root, max_chars=30_000).read("测试书", chapter="第5章到第7章")
    for i in (5, 6, 7):
        assert f"章末标记{i}" in out
    assert "章末标记8" not in out


# ------------------------------------------------------- 多章点名预算总量封顶
# 2026-11-16 靶子①修复（萧哥拍板）：read(chapter=区间) 过去每章各吃满 budget，
# 「第1到第3章」= 3×budget 无总量上限（默认 80k 预算下可达 24 万字）。
# 契约：多章点名按章数均分预算（保底 1000 字防碎片化），总量受单次预算封顶；
# 单章点名行为不变（仍吃满预算）。截断时 render_chapter 如实报
# 「本章共 X 字，本次读取上限 Y 字」，模型可换更窄的区间再点。


def test_multi_chapter_range_caps_total_budget(tmp_path: Path) -> None:
    root = _make_book(tmp_path, chapters=6, per_chapter=40_000)  # 每章 4 万字
    out = _shelf(root, max_chars=30_000).read("测试书", chapter="第1章到第3章")
    assert "第1章：标题1" in out
    assert "第2章：标题2" in out
    assert "第3章：标题3" in out
    assert len(out) < 30_000 + 3_000, "三章点名总加载量必须受单次预算封顶（旧行为会到 9 万）"


def test_single_chapter_keeps_full_budget(tmp_path: Path) -> None:
    root = _make_book(tmp_path, chapters=6, per_chapter=40_000)
    out = _shelf(root, max_chars=30_000).read("测试书", chapter="第2章")
    assert len(out) > 29_000, "单章点名仍吃满预算（行为不变）"


def test_many_chapters_floor_prevents_slivers(tmp_path: Path) -> None:
    root = _make_book(tmp_path, chapters=30, per_chapter=20_000)
    out = _shelf(root, max_chars=30_000).read("测试书", chapter="第1章到第30章")
    assert "第30章：标题30" in out
    assert "本次读取上限 1000" in out, "保底均分：每章 1000 字，不许再碎片化到几百字"
    assert len(out) < 45_000, "整本区间点名总量 ≈ n×1000，受控"


# --------------------------------------------------------------------- 材料索引

def test_materials_body_covers_stop_point_and_index_is_honest(tmp_path: Path) -> None:
    root = _make_book(tmp_path)
    pack = materials.RoomMaterials(_shelf(root), max_chars=20_000).prepare(
        "r1", "先读《测试书》"
    )
    assert pack is not None
    assert "章末标记12" in pack.body, "群材料也必须装到停笔点"
    index = pack.index
    assert "正文共" in index
    assert "第12章" in index, "章节清单必须在索引里"
    assert "本次未加载" in index, "没加载的章要标出来"
    assert "已写正文已加载" not in index, "不许再说这种让模型误以为读全的假话"
    assert "chapter 不是路径" in index


def test_materials_default_budget_reads_to_tail(tmp_path: Path) -> None:
    """默认 80k 预算下，9.6 万字的书能装到结尾（旧的 12k 头部截断已撤）。"""
    root = _make_book(tmp_path)
    pack = materials.RoomMaterials(_shelf(root)).prepare("r1", "读《测试书》")
    assert pack is not None
    assert "章末标记12" in pack.body


# --------------------------------------------------------------------- 真书

_REAL_BOOK = Path.home() / "Documents" / "已出版" / "16.夜不收"


@pytest.mark.skipif(not _REAL_BOOK.is_dir(), reason="本机没有《夜不收》书库")
def test_real_manuscript_reaches_writing_frontier() -> None:
    """真书验收：10.16 万字（十万级）也读得到第十一章、停笔处。

    2026-11-08 的真机事故：材料只喂前 8 万字，第十章、第十一章（停笔点）
    根本不在上下文，bot 却被告知「正文已加载」⇒ 拿开头硬推全局，点子全废。
    """
    shelf = reading.BookShelf(Path.home() / "Documents" / "已出版")
    pack = materials.RoomMaterials(shelf, style_loader=lambda: {}).prepare(
        "r1", "读《夜不收》"
    )
    assert pack is not None
    assert "迟正河" in pack.body, "第十一章人物必须在群材料正文里"
    assert "第十一章" in pack.index, "索引必须列出第十一章"
    assert "本次未加载" in pack.index, "10.16 万字 > 80k 预算，必须如实标出缺口"
    assert "已写正文已加载" not in pack.index


# ----------------------------------------------------------------- 区间写法
# 2026-11-17 实盘事故：人物专家 900 秒没交出一个字。
#
# 它按工具文档写了 `chapter="第七到第九章"`——**只有末位带「章」字**。旧正则
# `第[...]+[章回]` 于是只认出一个标签，find_chapters 返回 1 章：点三章给一章。
# 专家以为读了三章、实际只拿到第九章，只能反复重读整本来找中间内容，prompt 从
# 24k 涨到 199k，15 分钟全花在读上。
#
# 🔴 教训：测试必须用**模型的真实写法**。历史用例写的是「第5章到第7章」（每位都带
# 「章」），恰好绕过这个 bug，所以 11-16 那轮「多章均分预算」是纸面上绿、实盘全红。

@pytest.mark.parametrize(
    "needle",
    [
        "第七到第九章",      # 模型最常写：只有末位带「章」
        "第五到第七章",
        "第7-9章",          # 阿拉伯数字 + 连字符
        "第7到9章",
        "第七、第八、第九章",  # 顿号并列
        "第七、八、九章",     # 顿号并列的省略式
        "第七～第九章",       # 波浪线
        "第五至第九章",
    ],
)
def test_chapter_range_accepts_real_model_phrasings(tmp_path: Path, needle: str) -> None:
    root = _make_book(tmp_path, chapters=12, per_chapter=800)
    shelf = _shelf(root, max_chars=30_000)
    manuscript = shelf.manuscript("测试书")
    picked = [c.label for c in manuscript.find_chapters(needle)]
    assert picked, f"{needle!r} 一节都没命中"
    # 区间/并列必须真的给出中间那些章，不是只给末位
    assert len(picked) >= 3, f"{needle!r} 只命中 {picked}——又退回「点三章给一章」了"

    out = shelf.read("测试书", chapter=needle, max_chars=30_000)
    for label in picked:
        assert label in out, f"{label} 没进正文"


def test_chapter_range_does_not_hit_inline_prose(tmp_path: Path) -> None:
    """宽松序号解析不能把散文误当章节：「第一人称视角」不是第一章。"""
    root = _make_book(tmp_path, chapters=12, per_chapter=800)
    manuscript = _shelf(root, max_chars=30_000).manuscript("测试书")
    assert manuscript.find_chapters("第一人称视角") == []
    # 越界序号（书只有 12 章）也不许凭空造章
    assert manuscript.find_chapters("第十七到第十九章") == []
