"""1v1 专家读稿地基（2026-11-11）：**工具级覆盖率诚实** + 声明/实现对齐。

萧哥终裁：「不做更像个人，只做判断更可核验」。地基是四刀之下的那一层——
手艺再全，架在没读全的正文上就是「连续地错」。

真实事故（11-10/11-11 三场 1v1 会话，只读抽样）：
- `e596713d-c43`（brainstormer）：「上一轮没读第六章就把他当成空白悬念，这是我的锅」
  「我标的『未加载』是我偷懒只读了停笔点和头两章」；
- `37e829fb-a30`（character）：末尾「所有『粗纲说要这样』『画像说十六章』的引证，
  全部不算数」＝二手文档混入；
- 覆盖率全场仅出现 1 次，还是模型自己口述的，**工具结果里 0 次**。

根因不是「专家读不到正文」（他走全局 MCP `perception.read_chapter` 读了 12/12/15 次），
是 `file_reader.read_chapter` 每次只回「【第X章】（N 字，本章完整）」——
从不说全书几章、这次读了第几章、哪些章没加载。于是「没读到」被当成「书里没写」。

本组只锁两件事（都是工具层，可自动化、可核验）：
1. `read_chapter` 的**返回**必须自带覆盖率与「未加载 ≠ 正文缺失」的防御性措辞；
2. 专家人设**声明的工具名**必须与 1v1 运行时真实存在的工具对齐。

先红后绿：本文件在改生产代码之前应先全部失败。
"""
from __future__ import annotations

from pathlib import Path

import pytest

# catalog 与 coworker.agents 有已知循环：必须先让 agents 包完整初始化，
# 否则 `from ..catalog import expand` 撞上 partially-initialized 的 catalog。
# 顺序照抄 tests/test_catalog.py:12-15。
from coworker.agents.base import AgentContext  # noqa: F401
from coworker.agents.code import CODE_CAPABILITIES  # noqa: F401
from coworker.catalog import CATALOG
from deskmate.perception import file_reader

# 11 位写作专家（deskmate/bots/author.py::EXPERT_PERSONA_IDS 同源）。
EXPERT_IDS = (
    "brainstormer", "lit-editor", "adaptation", "historian",
    "proofreader", "reader", "publisher", "genre", "editor",
    "outline", "character",
)
_PERSONA_DIR = Path(__file__).resolve().parents[1] / "coworker" / "personas" / "builtin"


# --------------------------------------------------------------------- 造书

def _make_book(tmp_path: Path, *, chapters: int = 6) -> Path:
    """造一本 6 章的稿子；每章有可辨识的章末标记，便于断言只加载了指定一章。"""
    book = tmp_path / "测试书.txt"
    parts = [
        f"第{i}章：标题{i}\n第{i}章正文。" + "填" * 300 + f"章末标记{i}"
        for i in range(1, chapters + 1)
    ]
    book.write_text("\n\n".join(parts), encoding="utf-8")
    return book


# ------------------------------------------------- 一、read_chapter 覆盖率诚实

def test_read_chapter_reports_total_chapter_count(tmp_path: Path) -> None:
    """工具必须自己说「本稿共 N 章」，不能让模型靠猜。"""
    out = file_reader.read_chapter(str(_make_book(tmp_path)), "3")
    assert "本章完整" in out  # 原有行为不回退
    assert "共 6 章" in out, "工具返回里没有全书章数 → 模型无从判断自己读了多少"


def test_read_chapter_names_the_chapter_it_loaded(tmp_path: Path) -> None:
    """必须点名「本次仅加载第 3 章」，而不是只给一个孤立的章节块。"""
    out = file_reader.read_chapter(str(_make_book(tmp_path)), "3")
    assert "第 3 章" in out
    assert "章末标记3" in out
    assert "章末标记5" not in out, "别把没读的章混进正文"


def test_read_chapter_marks_unloaded_chapters(tmp_path: Path) -> None:
    """其余章节必须显式标为未加载（对应群聊 reading.py::Coverage.note 的诚实口径）。"""
    out = file_reader.read_chapter(str(_make_book(tmp_path)), "3")
    assert "未加载" in out
    assert "其余 5 章" in out


def test_read_chapter_warns_unloaded_is_not_absent(tmp_path: Path) -> None:
    """防「未读当空白」的关键一句：没读到 ≠ 书里没写。

    11-11 事故原话「上一轮没读第六章就把他当成空白悬念」就是缺这句。
    """
    out = file_reader.read_chapter(str(_make_book(tmp_path)), "3")
    assert "不等于" in out or "不是" in out, "缺「未加载 ≠ 正文缺失」的防御性措辞"
    assert "书里没写" in out or "正文缺失" in out
    assert "不得" in out or "不许" in out, "必须给出禁止性判据，不是中性描述"


def test_read_chapter_tells_how_to_close_the_gap(tmp_path: Path) -> None:
    """要给可执行的补读路径：list_chapters 看目录 + 逐章 read_chapter。"""
    out = file_reader.read_chapter(str(_make_book(tmp_path)), "3")
    assert "list_chapters" in out
    assert "read_chapter" in out


def test_partial_chapter_still_reports_coverage(tmp_path: Path) -> None:
    """单章超上限被截断时，覆盖率标注同样不能丢（旧实现只说「本章前半」）。"""
    book = _make_book(tmp_path)
    out = file_reader.read_chapter(str(book), "3", max_chars=100)
    assert "共 6 章" in out
    assert "未加载" in out
    assert "书里没写" in out or "正文缺失" in out


def test_unknown_chapter_error_reports_total(tmp_path: Path) -> None:
    """章节找不到时，报错里也得给全书规模，别让模型以为书很短。"""
    out = file_reader.read_chapter(str(_make_book(tmp_path)), "第九十九章")
    assert "未找到" in out
    assert "共 6 章" in out


def test_list_chapters_reminds_directory_is_not_read(tmp_path: Path) -> None:
    """目录只是目录：只有实际读过某一章才算读过（防「看过目录＝读完全书」）。"""
    out = file_reader.list_chapters(str(_make_book(tmp_path)))
    assert "共 6 节" in out or "共 6 章" in out
    assert "不算" in out or "不等于" in out or "只有" in out


def test_internal_index_consumer_gets_clean_body(tmp_path: Path) -> None:
    """creation/novel_index.py 把返回当纯正文灌进圣经索引，必须能关掉标注。

    这是防回归闸：工具层默认开，内部消费者显式关，两侧都不能悄悄换。
    """
    out = file_reader.read_chapter(str(_make_book(tmp_path)), "3", coverage=False)
    assert "【覆盖率】" not in out
    assert "共 6 章" not in out
    assert "章末标记3" in out
    assert "本章完整" in out


def test_coverage_marker_precedes_body(tmp_path: Path) -> None:
    """提示必须放在正文之前——模型读到内容时已经知道这不是全文。

    与 file_reader.read_file_safe:90-91 同口径。
    """
    out = file_reader.read_chapter(str(_make_book(tmp_path)), "3")
    assert out.index("共 6 章") < out.index("章末标记3")


# --------------------------------------------- 二、声明与实现对齐（工具名错配）

def _persona_text(pid: str) -> str:
    p = _PERSONA_DIR / f"{pid}.md"
    if not p.is_file():
        p = _PERSONA_DIR / pid / "manifest.md"  # B 方案后转 bundle
    return p.read_text(encoding="utf-8")


@pytest.mark.parametrize("pid", EXPERT_IDS)
def test_expert_persona_names_the_real_1v1_reading_tools(pid: str) -> None:
    """5 份人设写「用 read_book 按需读」，但 1v1 注册表里根本没有 read_book。

    `coworker/catalog.py::_CAPS` 8 个 capability 无 read_book/list_books；
    1v1 实际可用的是全局 MCP `perception.list_chapters` / `read_chapter`
    （persona 未声明 mcp: 时全量暴露，见 manager.prepare_mcp_tools）。
    群聊侧 `read_book` 是真实工具，因此不要求删除该词，只要求人设同时点名
    1v1 的真实工具名——否则声明与实现脱节，模型只能「工具里有什么就用什么」。
    """
    text = _persona_text(pid)
    assert "list_chapters" in text, f"{pid} 人设没告诉模型 1v1 该先列目录"
    assert "read_chapter" in text, f"{pid} 人设没告诉模型 1v1 该按章读"


def test_catalog_has_no_phantom_read_book() -> None:
    """锁住事实：read_book/list_books 不是 1v1 的 catalog capability。

    这条本身是绿的（描述现状），用来防止将来有人「顺手」把幻觉工具名加进
    _CAPS 来绕过上面那条契约测试。
    """
    assert "read_book" not in CATALOG
    assert "list_books" not in CATALOG
