"""P0-5 记忆去重 / USER.md 清洗 测试。

核心保证（离线、无 LLM 也成立，因为这是「止血」的关键）：
1. 近重复条目不再被反复追加（防 3+ 重复堆积）。
2. 明显不同的事实（如「爱喝茶」vs「爱喝咖啡」）不被误判为重复 → 绝不误删真实信息。
3. clean_user_memory 能合并/删除已有重复，重复率趋近 0。
4. 改动记忆逻辑后，8.1 方向盘召回率不降（≥0.85）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deskmate.memory.manager import DeskMateMemoryManager
from deskmate.memory.user_profile import load_user, save_user


@pytest.fixture
def mgr(tmp_path: Path, monkeypatch):
    """强制离线（llm_available=False），使测试只验证离线去重逻辑（确定性、零成本）。"""
    monkeypatch.setattr("deskmate.memory.manager.llm_available", lambda: False)
    m = DeskMateMemoryManager(base_dir=tmp_path)
    return m


def _entries(mgr) -> list[str]:
    return mgr._user_entries()


# ----------------------------------------------------------------------
# 1. 近重复拦截（防 3+ 重复堆积）
# ----------------------------------------------------------------------
def test_offline_near_duplicate_not_appended(mgr):
    save_user(mgr.base_dir, "用户爱喝咖啡")
    before = len(_entries(mgr))
    msg = mgr.append_to_section("user", "用户爱喝咖啡。")  # 标点差异，sim=1.0
    assert "跳过" in msg or "合并" in msg
    assert len(_entries(mgr)) == before  # 未新增


def test_offline_repeat_append_idempotent(mgr):
    save_user(mgr.base_dir, "用户爱喝咖啡")
    mgr.append_to_section("user", "用户爱喝咖啡啊")  # 尾字差异，sim=1.0
    mgr.append_to_section("user", "用户爱喝咖啡呀")
    # 全部并入同一条，条目数不变
    assert len(_entries(mgr)) == 1


# ----------------------------------------------------------------------
# 2. 绝不误删：明显不同的事实要追加成功
# ----------------------------------------------------------------------
def test_offline_distinct_facts_both_kept(mgr):
    save_user(mgr.base_dir, "用户爱喝茶")  # 与咖啡 sim≈0.71，低于 0.85
    mgr.append_to_section("user", "用户爱喝咖啡")
    entries = _entries(mgr)
    assert len(entries) == 2
    assert any("茶" in e for e in entries)
    assert any("咖啡" in e for e in entries)


def test_offline_tool_names_not_false_positive(mgr):
    save_user(mgr.base_dir, "用户喜欢用VS Code")  # 与 PyCharm 仅拉丁部分不同
    mgr.append_to_section("user", "用户喜欢用PyCharm")
    entries = _entries(mgr)
    assert len(entries) == 2  # 两个不同工具都保留


def test_offline_new_fact_appended(mgr):
    save_user(mgr.base_dir, "用户是宁波人")
    msg = mgr.append_to_section("user", "用户每天写 2000 字")
    assert "已追加" in msg
    assert len(_entries(mgr)) == 2


# ----------------------------------------------------------------------
# 3. clean_user_memory：合并/删除已有重复
# ----------------------------------------------------------------------
def test_cleaner_removes_exact_duplicates(mgr):
    save_user(mgr.base_dir, "用户爱喝咖啡\n\n用户爱喝咖啡\n\n用户爱喝咖啡")
    assert len(_entries(mgr)) == 3
    res = mgr.clean_user_memory()
    assert res["entries_after"] == 1
    assert res["removed"] == 2
    assert len(_entries(mgr)) == 1


def test_cleaner_removes_near_duplicates_offline(mgr):
    save_user(mgr.base_dir, "用户爱喝咖啡\n\n用户爱喝咖啡。\n\n用户爱喝咖啡啊")
    before = len(_entries(mgr))
    res = mgr.clean_user_memory()
    assert res["entries_after"] == 1
    assert res["removed"] == before - 1


def test_cleaner_idempotent(mgr):
    save_user(mgr.base_dir, "用户爱喝咖啡\n\n用户爱喝咖啡。")
    mgr.clean_user_memory()
    after_first = len(_entries(mgr))
    # 再跑一次不应再删
    res = mgr.clean_user_memory()
    assert res["removed"] == 0
    assert len(_entries(mgr)) == after_first


def test_cleaner_backup_created(mgr):
    save_user(mgr.base_dir, "用户爱喝咖啡\n\n用户爱喝咖啡。")
    mgr.clean_user_memory()
    bak = mgr.base_dir / "USER.md.bak"
    assert bak.exists()  # 清洗前已备份，可还原


# ----------------------------------------------------------------------
# 4. user_dup_rate 指标
# ----------------------------------------------------------------------
def test_user_dup_rate_high_before_clean(mgr):
    save_user(mgr.base_dir, "用户爱喝咖啡\n\n用户爱喝咖啡。")
    assert mgr.user_dup_rate() >= 0.5


def test_user_dup_rate_zero_after_clean(mgr):
    save_user(mgr.base_dir, "用户爱喝咖啡\n\n用户爱喝咖啡。\n\n用户爱喝咖啡啊")
    mgr.clean_user_memory()
    assert mgr.user_dup_rate() == 0.0


# ----------------------------------------------------------------------
# 5. 8.1 方向盘：召回率不降（记忆逻辑改动后回归基准）
# ----------------------------------------------------------------------
def test_8_1_recall_no_regression():
    import sys
    import tempfile

    sys.path.insert(0, str(Path(__file__).resolve().parent / "memory_bench"))
    from fixture_xiaosheng import build_xiaosheng_fixture
    from runner import evaluate

    d = Path(tempfile.mkdtemp(prefix="membench_p05_"))
    build_xiaosheng_fixture(d)
    rep = evaluate(d)
    assert rep["recall_rate"] >= 0.85, f"召回率回退: {rep['recall_rate']:.1%}"
    # 方向盘附加指标：USER.md 重复率应为 0（fixture 无重复）
    assert rep["user_dup_rate"] == 0.0, f"fixture USER.md 重复率非0: {rep['user_dup_rate']}"
