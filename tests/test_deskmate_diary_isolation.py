"""日记与全局记忆的「隔离契约」回归测试。

背景（2026-09-21 实测）
----------------------
``manager._load_global_memory()`` 会把 MEMORY.md 拼接上 ``diary_memory.md``
（DiaryStore 导出的检索视图）供 prefetch 检索，而 ``_save_global_memory()``
只写 MEMORY.md。于是任何 ``load → 解析 → 渲染 → 保存`` 的路径（dream 每天跑、
append_to_section、衰减、压缩）都把视图内容**固化**进 MEMORY.md，每次落笔 +1 份
——实测长到 3 份、占文件 52%；且 dream 把日记当普通事实做近重复合并（19→18 条）
与矛盾消解。

存量清理脚本：``scripts/cleanup_memory_diary_blocks.py``

本文件锁死三条不变量：
1. 写回路径不得把「§ 日记沉淀」写进 MEMORY.md（断自我复制）；
2. dream 不得改写日记分节（日记是私人记述，不是可交易的事实断言）；
3. 隔离 ≠ 失联——日记仍须经拼接视图被 prefetch 检索到。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deskmate.memory import dream as D
from deskmate.memory.manager import DeskMateMemoryManager, _strip_diary_sections

DIARY_HEAD = "§ 日记沉淀 #moment #diary"
DIARY_A = "2026-09-14：🩷 萧盛的日记：出版寒冬与影视寒冬，文化业进入严寒期。"
DIARY_B = "2026-09-15：🩷 萧盛的日记：今天写了一千字，去看了《揭秘日》。"
DIARY_C = "2026-09-16：🩷 萧盛的日记：孩子开学了，平日能安心写作。"


def _diary_block(entries=(DIARY_A,)) -> str:
    return DIARY_HEAD + "\n" + "\n\n".join(entries) + "\n"


def _write(tmp_path: Path, mem: str) -> None:
    (tmp_path / "MEMORY.md").write_text(mem, encoding="utf-8")


def _diary_lines(mgr: DeskMateMemoryManager, text: str) -> list[str]:
    """取文本里日记分节的条目行（用于比对是否被改写）。"""
    _, secs = mgr._parse_sections(text)
    for s in secs:
        if "日记沉淀" in (s.get("header_rest") or ""):
            return [l.strip() for l in s["body"].splitlines() if l.strip()]
    return []


@pytest.fixture
def mgr(tmp_path: Path) -> DeskMateMemoryManager:
    (tmp_path / "memory").mkdir(exist_ok=True)
    (tmp_path / "projects").mkdir(exist_ok=True)
    return DeskMateMemoryManager(tmp_path)


# ---------------------------------------------------------------------------
# 1) 剥离函数的契约
# ---------------------------------------------------------------------------
class TestStripDiarySections:
    def test_no_diary_text_is_byte_identical(self):
        """无日记块时必须逐字节原样返回（快路径，避免正常保存路径格式漂移）。"""
        t = "§ 偏好 #preference\n- 2026-09-01: 只查正史\n"
        assert _strip_diary_sections(t) == t

    def test_strips_single_block_keeps_others(self):
        t = "§ 偏好 #preference\n- 2026-09-01: 只查正史\n\n" + _diary_block()
        out = _strip_diary_sections(t)
        assert "日记沉淀" not in out
        assert "只查正史" in out

    def test_strips_every_block(self):
        t = _diary_block() + "\n" + _diary_block((DIARY_B,)) + "\n" + _diary_block((DIARY_C,)) + "\n"
        assert _strip_diary_sections(t).count("日记沉淀") == 0

    def test_other_sections_survive_untouched(self):
        t = (
            "§ 工作 #work\n- 2026-09-20: 选题\n\n"
            "§ 长期事实 #stable\n- 2026-09-01: 串书禁令是最高优先级\n\n"
            + _diary_block()
            + "\n§ 偏好 #preference\n- 2026-09-06: 只查正史\n\n"
            + _diary_block((DIARY_B, DIARY_C))
        )
        out = _strip_diary_sections(t)
        assert out.count("日记沉淀") == 0
        for key in ("选题", "串书禁令", "只查正史"):
            assert key in out, f"误删了非日记内容: {key}"


# ---------------------------------------------------------------------------
# 2) 保存路径绝不落日记
# ---------------------------------------------------------------------------
class TestSaveNeverPersistsDiary:
    def test_save_strips_diary_block(self, mgr, tmp_path):
        _write(tmp_path, "§ 偏好 #preference\n- 2026-09-01: 只查正史\n\n" + _diary_block())
        content = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        mgr._save_global_memory(content)

        saved = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        assert "日记沉淀" not in saved, "保存路径把日记块固化进了 MEMORY.md"
        assert "只查正史" in saved

    def test_multiple_blocks_all_dropped_on_save(self, mgr, tmp_path):
        """对应真实故障：3 份日记块经一次 load→save 会被继续放大，现在必须收敛为 0。"""
        _write(tmp_path, _diary_block() + "\n" + _diary_block() + "\n" + _diary_block() + "\n")
        mgr._save_global_memory(mgr._load_global_memory())
        assert "日记沉淀" not in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")

    def test_save_keeps_non_diary_content(self, mgr, tmp_path):
        _write(tmp_path, "§ 长期事实 #stable\n- 2026-09-11: 串书禁令\n\n" + _diary_block())
        mgr._save_global_memory(mgr._load_global_memory())
        assert "串书禁令" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 3) 隔离 ≠ 失联：日记仍可检索
# ---------------------------------------------------------------------------
class TestDiaryStillRetrievable:
    def test_load_concatenates_diary_view(self, mgr, tmp_path):
        _write(tmp_path, "§ 偏好 #preference\n- 2026-09-01: 只查正史\n")
        (tmp_path / "diary_memory.md").write_text(_diary_block(), encoding="utf-8")

        combined = mgr._load_global_memory()
        assert "出版寒冬" in combined, "日记视图未被拼接，检索会失联"
        assert "只查正史" in combined

    def test_prefetch_reaches_diary(self, mgr, tmp_path):
        _write(tmp_path, "§ 偏好 #preference\n- 2026-09-01: 只查正史\n")
        (tmp_path / "diary_memory.md").write_text(_diary_block(), encoding="utf-8")

        ctx = mgr.prefetch("出版寒冬 影视寒冬")
        assert "出版寒冬" in ctx, "prefetch 未取到日记内容"


# ---------------------------------------------------------------------------
# 4) dream 不得改写日记
# ---------------------------------------------------------------------------
class TestDreamProtectsDiary:
    def test_protected_tags_include_diary(self):
        assert {"moment", "diary"} <= D._PROTECTED_SECTIONS

    def test_dream_keeps_diary_section_intact(self, mgr, tmp_path):
        """三条日记（含近重复）过一遍 dream，条数与内容都不许变。"""
        _write(
            tmp_path,
            "§ 工作 #work\n- 2026-09-20: 选题\n\n"
            + _diary_block((DIARY_A, DIARY_A, DIARY_B, DIARY_C)),
        )
        before_text = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        before = _diary_lines(mgr, before_text)
        assert len(before) == 4

        D.run_dream(tmp_path)

        after_text = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        after = _diary_lines(mgr, after_text)
        assert after == before, f"dream 改写了日记：{before} → {after}"

    def test_dream_still_processes_normal_sections(self, mgr, tmp_path):
        """保护不能变成「整个 dream 停摆」——普通分节该提炼仍要提炼。"""
        dup = "- 2026-09-10: 重复事实条目"
        _write(
            tmp_path,
            f"§ 工作 #work\n{dup}\n\n{dup}\n\n- 2026-09-11: 另一条\n\n" + _diary_block(),
        )
        report = D.run_dream(tmp_path)
        assert report.get("merged", 0) >= 1, "普通分节的近重复未被合并，保护范围过宽"


# ---------------------------------------------------------------------------
# 5) 迁移幂等键：有帖子也要清 MEMORY.md
# ---------------------------------------------------------------------------
class TestMigrateCleansMemoryEvenWhenPostsExist:
    def test_memory_cleaned_when_user_posts_exist(self, tmp_path):
        """旧幂等键只看 user_posts → 有帖子就整体跳过 → 清理代码从未执行。"""
        from deskmate.growth.diary_store import DiaryStore

        store = DiaryStore(base_dir=tmp_path)
        store.add_user_post("已入库的日记本体")
        _write(tmp_path, "§ 工作 #work\n- 2026-09-20: 选题\n\n" + _diary_block())

        res = store.migrate_from_legacy(backup=True)

        assert res.get("skipped") is not True, "有沉淀块时不该整体跳过"
        assert res["mem_cleaned"] is True
        assert res["migrated_posts"] == 0, "已有帖子时不应重复计数"
        saved = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        assert "日记沉淀" not in saved
        assert "选题" in saved

    def test_skipped_only_when_both_sources_clean(self, tmp_path):
        from deskmate.growth.diary_store import DiaryStore

        store = DiaryStore(base_dir=tmp_path)
        store.add_user_post("x")
        _write(tmp_path, "§ 工作 #work\n- 2026-09-20: 选题\n")

        res = store.migrate_from_legacy(backup=True)
        assert res.get("skipped") is True
        assert res["mem_cleaned"] is False


# ---------------------------------------------------------------------------
# 6) 分层净化的保留名单
# ---------------------------------------------------------------------------
class TestKeepGlobalSections:
    def test_diary_titles_are_kept(self):
        keep = DeskMateMemoryManager._KEEP_GLOBAL_SECTIONS
        for name in ("日记主题", "日记沉淀"):
            assert name in keep, f"分层净化会把 {name} 迁进项目记忆（不可达）"
