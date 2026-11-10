"""P1-B 按书注入纪律：MEMORY.md / USER.md 读侧隔离 + 当前书接线。

对应 docs/memory-discipline-design.md §7（多书隔离完整落地 P1）：

- 不改写 ~/.deskmate/USER.md / MEMORY.md 文件本体（§10.4：人工标注是证据）；
  只在**读侧**按当前书过滤 / 标注。
- 认不出当前书时**不猜、不过滤**，只给「可能过时」标注；异常 fail-open 返回原文。
- 修 P0 缺口：显式 book（常是工作区 UUID）归一失败后必须回退到 query 推断，
  否则 query 明明写了《夜不收》，整轮书隔离却静默失效。
- 当前书按**每轮请求**解析，不写进程级 active_project（多会话共享 manager 会串味）。

全部用 tmp 目录，不碰真实 ~/.deskmate。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from deskmate.memory import book_scope
from deskmate.memory import user_fact_context as ufc
from deskmate.memory.learned_store import LearnedStore
from deskmate.memory.manager import DeskMateMemoryManager

MEMORY_MD = """前言：通用说明。

§ 夜不收 #stable
- 万历二十一年（1593）二月二十日，孔大贵率夜不收十五人东征。
- 大和盟总旗主是韩孝民，粮仓未烧成。

§ 国子监秘闻录 #stable
- 元祐年间诗案，牵连骨架不动、换呈现方式。

§ 偏好 #preference
- 他偏好短句、古白话，禁用文牍腔。
"""

USER_MD = """# 用户画像

## 基本信息
- 萧盛，1977年生，写作者。

## 当前创作（《国子监秘闻录》）
- 汪成儒诗须流露对新法的怅惘。
- 称谓：江鸿鹰「中丞」、高付「监察」。

## 工作模式与AI协作
- 用户提想法，AI负责实现。
"""

KNOWN = ["夜不收", "国子监秘闻录", "衙探"]

# 真实 ~/.deskmate/MEMORY.md 的形状：分节标题不是书名，多书条目按行混装
# （2026-11-10 实测只有 § 纠正 / § 工作 / § 创作 三节）。
MIXED_MEMORY_MD = """§ 纠正 #correction
- 2026-10-30: 《夜不收》前八章误将李昖置于开城。
- 2026-10-28: 萧盛澄清团队协作功能是系统内功能。
- 2026-11-09: 《国子监秘闻录》里韩效武是丁闻香心仪对象。
- 跨书对照：《夜不收》韩孝民与《国子监秘闻录》汪成儒不能混。

§ 创作 #stable
- 《夜不收》火药桶数：陈冲两桶，方也行三桶。
- 通用：写作三阶段。
"""


class BookScopePureTest(unittest.TestCase):
    def test_resolve_turn_book_from_query(self):
        self.assertEqual(
            book_scope.resolve_turn_book(None, "接着写《夜不收》第十一章", known=KNOWN),
            "夜不收",
        )

    def test_resolve_turn_book_ambiguous_does_not_guess(self):
        self.assertEqual(
            book_scope.resolve_turn_book(None, "对比《夜不收》和《衙探》", known=KNOWN),
            "",
        )

    def test_resolve_turn_book_uuid_active_project_is_ignored(self):
        self.assertEqual(
            book_scope.resolve_turn_book(
                None, "随便聊聊", active_project="a68a13a3-f184-44be", known=KNOWN
            ),
            "",
        )

    def test_filter_memory_sections_other_book(self):
        out = book_scope.filter_memory_sections_by_book(
            MEMORY_MD, "国子监秘闻录", KNOWN
        )
        self.assertNotIn("大和盟总旗主是韩孝民", out)
        self.assertNotIn("孔大贵率夜不收十五人", out)
        self.assertIn("元祐年间诗案", out)
        self.assertIn("他偏好短句", out)

    def test_filter_memory_sections_keeps_own_book_and_generic(self):
        out = book_scope.filter_memory_sections_by_book(MEMORY_MD, "夜不收", KNOWN)
        self.assertIn("大和盟总旗主是韩孝民", out)
        self.assertIn("他偏好短句", out)
        self.assertNotIn("元祐年间诗案", out)

    def test_filter_memory_sections_unknown_book_does_not_filter(self):
        out = book_scope.filter_memory_sections_by_book(MEMORY_MD, "", KNOWN)
        self.assertEqual(out, MEMORY_MD)

    def test_mixed_sections_are_filtered_line_by_line(self):
        """真实 MEMORY.md 分节标题不是书名，必须按条目过滤，不能整节放行。"""
        out = book_scope.filter_memory_sections_by_book(
            MIXED_MEMORY_MD, "国子监秘闻录", KNOWN
        )
        self.assertNotIn("李昖", out)
        self.assertNotIn("火药桶数", out)
        self.assertIn("萧盛澄清", out)
        self.assertIn("韩效武", out)
        self.assertIn("跨书对照", out)  # 多书同现不误杀
        self.assertIn("写作三阶段", out)
        self.assertIn("按当前书", out)

    def test_mixed_sections_other_direction(self):
        out = book_scope.filter_memory_sections_by_book(
            MIXED_MEMORY_MD, "夜不收", KNOWN
        )
        self.assertIn("火药桶数", out)
        self.assertIn("李昖", out)
        self.assertNotIn("韩效武", out)

    def test_mixed_sections_unknown_book_keeps_everything(self):
        out = book_scope.filter_memory_sections_by_book(MIXED_MEMORY_MD, "", KNOWN)
        self.assertEqual(out, MIXED_MEMORY_MD)

    def test_filter_user_profile_other_book_drops_stale_section(self):
        out = book_scope.filter_user_profile_by_book(USER_MD, "夜不收", KNOWN)
        self.assertNotIn("汪成儒诗须流露", out)
        self.assertNotIn("江鸿鹰", out)
        self.assertIn("可能过时", out)
        self.assertIn("萧盛，1977年生", out)

    def test_filter_user_profile_same_book_keeps_original(self):
        out = book_scope.filter_user_profile_by_book(USER_MD, "国子监秘闻录", KNOWN)
        self.assertIn("汪成儒诗须流露", out)

    def test_filter_user_profile_unknown_book_annotates_only(self):
        out = book_scope.filter_user_profile_by_book(USER_MD, "", KNOWN)
        self.assertIn("汪成儒诗须流露", out)
        self.assertIn("可能过时", out)

    def test_kill_switch_returns_original(self):
        # 用环境变量直接验证（unittest.TestCase 不注入 pytest fixture）
        import os

        old = os.environ.get("DESKMATE_BOOK_SCOPE")
        os.environ["DESKMATE_BOOK_SCOPE"] = "0"
        try:
            self.assertEqual(
                book_scope.filter_memory_sections_by_book(MEMORY_MD, "国子监秘闻录", KNOWN),
                MEMORY_MD,
            )
            self.assertEqual(
                book_scope.filter_user_profile_by_book(USER_MD, "夜不收", KNOWN),
                USER_MD,
            )
        finally:
            if old is None:
                os.environ.pop("DESKMATE_BOOK_SCOPE", None)
            else:
                os.environ["DESKMATE_BOOK_SCOPE"] = old


class P0FallbackTest(unittest.TestCase):
    """显式 book=UUID 时，必须回退到 query 推断，不能让书隔离整轮失效。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        store = LearnedStore(self.base)
        store.append(["《夜不收》设定：大和盟总旗主是韩孝民"], source="setting")
        store.append(["《国子监秘闻录》设定：汪成儒诗案"], source="setting")

    def tearDown(self):
        self._tmp.cleanup()

    def test_uuid_book_falls_back_to_query(self):
        ctx = ufc.build_user_fact_context(
            self.base,
            query="接着写《夜不收》第十一章",
            book="a68a13a3-f184-44be",
        )
        self.assertIn("韩孝民", ctx)
        self.assertNotIn("汪成儒", ctx)


@pytest.fixture
def mem_base(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text(MEMORY_MD, encoding="utf-8")
    (tmp_path / "USER.md").write_text(USER_MD, encoding="utf-8")
    projects = tmp_path / "projects" / "夜不收"
    projects.mkdir(parents=True)
    (projects / "longterm.md").write_text(
        "§ 夜不收项目记忆 #stable\n- 龙山仓：仓门炸开逃生（粮未烧成）。\n",
        encoding="utf-8",
    )
    return tmp_path


def test_turn_context_wires_current_book(monkeypatch, mem_base: Path):
    """manager 必须把「按轮解析出的书名」传给 user_fact_context，而不是 UUID。"""
    captured: dict = {}
    original = ufc.build_user_fact_context

    def spy(base_dir, query=None, limit=10, book=None, now=None):
        captured["book"] = book
        return original(base_dir, query, limit, book, now)

    monkeypatch.setattr(ufc, "build_user_fact_context", spy)
    mgr = DeskMateMemoryManager(
        base_dir=mem_base, active_project="a68a13a3-f184-44be"
    )
    mgr.build_full_context_for_turn("接着写《夜不收》第十一章")
    assert captured.get("book") == "夜不收"


def test_turn_context_drops_other_book_profile(monkeypatch, mem_base: Path):
    """当前书写《夜不收》时，USER.md 的国子监「当前创作」整节不该进上下文。"""
    monkeypatch.setenv("DESKMATE_EMOTION_ENGINE", "0")
    mgr = DeskMateMemoryManager(base_dir=mem_base)
    ctx = mgr.build_full_context_for_turn("接着写《夜不收》第十一章")
    assert "汪成儒" not in ctx
    assert "萧盛" in ctx


def test_turn_context_unknown_book_keeps_profile(mem_base: Path):
    """认不出当前书 → 不猜、不过滤，只加提示。"""
    mgr = DeskMateMemoryManager(base_dir=mem_base)
    ctx = mgr.build_full_context_for_turn("今天随便聊两句")
    assert "汪成儒" in ctx


if __name__ == "__main__":
    unittest.main()
