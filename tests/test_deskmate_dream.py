"""P2-1 后台自动提炼（Dreaming）单测。

隔离在临时 base_dir，验证：
- 保守默认不误删不同主题条目
- 同主题近重复去重（留新）
- 同主题异值矛盾消解（留新、旧值标注保留 / 超龄迁 vault）
- 幂等（已干净再跑 0 改动）
- 间隔守卫（_should_run）
- 不碰书稿记忆（scope 边界）
"""

from __future__ import annotations

import datetime
import importlib

import pytest

from deskmate.memory import dream as _dream
from deskmate.memory.user_profile import load_user, save_user


def _write_memory(base, text):
    (base / "MEMORY.md").write_text(text, encoding="utf-8")


def _today() -> str:
    return datetime.date.today().isoformat()


def _days_ago(n: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# 1) 保守：不同主题条目不误删
# ---------------------------------------------------------------------------
def test_conservative_no_overdelete(tmp_path):
    save_user(tmp_path, "用户是历史小说作家\n\n用户偏好清晨写作")
    rep = _dream.run_dream(tmp_path)
    assert rep["merged"] == 0
    assert rep["contradictions_resolved"] == 0
    user = load_user(tmp_path)
    assert "历史小说作家" in user
    assert "偏好清晨写作" in user


# ---------------------------------------------------------------------------
# 2) 近重复去重（高重叠留新）
# ---------------------------------------------------------------------------
def test_near_duplicate_merged(tmp_path):
    # 近乎完全相同的两条（仅差一个句号）→ 应被去重合并
    save_user(tmp_path,
              f"{_today()}: 用户偏好在安静环境写作\n\n"
              f"{_today()}: 用户偏好在安静环境写作。")
    rep = _dream.run_dream(tmp_path)
    assert rep["merged"] == 1
    user = load_user(tmp_path)
    # 两条冗余被并成一条
    assert user.count("写作") <= 2


def test_value_flip_not_merged(tmp_path):
    # 「喜欢」vs「不喜欢」虽高度重叠，但属值翻转，绝不可合并删除（两条都保留）
    save_user(tmp_path,
              f"{_today()}: 用户喜欢用 Markdown 写大纲\n\n"
              f"{_today()}: 用户不喜欢用 Markdown 写大纲")
    rep = _dream.run_dream(tmp_path)
    assert rep["merged"] == 0  # 绝不能把「不喜欢」当重复删掉
    user = load_user(tmp_path)
    assert "喜欢" in user and "不喜欢" in user  # 两边都保留


# ---------------------------------------------------------------------------
# 3) 矛盾消解：同主题异值，未超龄 → 留新 + 旧值标注保留
# ---------------------------------------------------------------------------
def test_contradiction_annotated_not_deleted(tmp_path):
    _write_memory(tmp_path,
                  f"§ 笔记 #note\n{_days_ago(10)}: 喜欢用 Markdown 写大纲\n\n"
                  f"{_today()}: 喜欢用 Word 写大纲")
    rep = _dream.run_dream(tmp_path)
    assert rep["contradictions_resolved"] == 1
    assert rep["stale_superseded"] == 0
    mem = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    # 两条都在（旧值保留），且旧值被标注
    assert "Markdown" in mem
    assert "Word" in mem
    assert _dream._SUPERSEDED_MARK in mem


# ---------------------------------------------------------------------------
# 4) 超龄被覆盖 → 迁 vault（可恢复、不丢）
# ---------------------------------------------------------------------------
def test_stale_superseded_to_vault(tmp_path):
    _write_memory(tmp_path,
                  f"§ 笔记 #note\n{_days_ago(200)}: 喜欢用 Markdown 写大纲\n\n"
                  f"{_today()}: 喜欢用 Word 写大纲")
    rep = _dream.run_dream(tmp_path)
    assert rep["stale_superseded"] == 1
    mem = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    # 旧值已从活动记忆移除
    assert "Markdown" not in mem
    assert "Word" in mem
    # 旧值进了 vault（可恢复）
    vault_dir = tmp_path / "vault"
    assert vault_dir.is_dir()
    found = False
    for f in vault_dir.rglob("*.md"):
        if "Markdown" in f.read_text(encoding="utf-8"):
            found = True
    assert found, "超龄旧值应进入 vault"


# ---------------------------------------------------------------------------
# 5) 幂等：已干净的记忆再跑 0 改动
# ---------------------------------------------------------------------------
def test_idempotent(tmp_path):
    save_user(tmp_path, f"{_today()}: 用户偏好在安静环境写作")
    _dream.run_dream(tmp_path)
    # 清掉间隔标记，允许立即再跑
    (_tmp_marker(tmp_path))
    rep2 = _dream.run_dream(tmp_path)
    assert rep2["merged"] == 0
    assert rep2["contradictions_resolved"] == 0
    assert rep2["stale_superseded"] == 0


def _tmp_marker(base):
    m = base / _dream._MARKER
    if m.exists():
        m.unlink()


# ---------------------------------------------------------------------------
# 6) 间隔守卫
# ---------------------------------------------------------------------------
def test_should_run_guard(tmp_path):
    assert _dream._should_run(tmp_path) is True
    _dream._mark_run(tmp_path)
    # 刚跑过，间隔内不再跑
    assert _dream._should_run(tmp_path) is False


# ---------------------------------------------------------------------------
# 7) scope：不碰书稿记忆（项目记忆）
# ---------------------------------------------------------------------------
def test_scope_book_memory_untouched(tmp_path):
    proj = tmp_path / "projects" / "国子监秘闻录"
    proj.mkdir(parents=True)
    book = proj / "工作.md"
    book.write_text(f"{_today()}: 《国子监秘闻录》第三章金牌设定已定稿\n", encoding="utf-8")
    _dream.run_dream(tmp_path)
    # 书稿记忆原样不动
    assert "金牌设定已定稿" in book.read_text(encoding="utf-8")
    # 报告里也不该出现 book 痕迹
    rep_path = tmp_path / _dream._REPORT_FILE
    if rep_path.is_file():
        assert "国子监秘闻录" not in rep_path.read_text(encoding="utf-8")
