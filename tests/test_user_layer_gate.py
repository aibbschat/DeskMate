"""USER.md 条目归属闸回归（2026-11-11）。

背景：USER.md 的 `## 硬规矩` 每轮常驻注入，`## 偏好与事实` 只在检索命中时进入
上下文。抽取层只能追加到文件末尾（偏好层），写不进硬规矩；真正的口子只剩两条
整篇覆写路径——压缩（`_llm_compress_user` → `save_user`）和
`write_memory(section="user")`（HTTP / MCP 两个调用方）。

这里锁住四件事：
1. 压缩把偏好搬进硬规矩 → 降级回偏好层（不丢内容）；
2. 整篇回写把系统文案写进硬规矩 → 拒写，原文件不动；
3. 同层精简（合法压缩）→ 放行，不误伤；
4. 同层重排（合法整理）→ 放行。
另锁：原 `check_user_content` 三道护栏仍生效、不替代；`force=True` 留痕。

全部用 tmp 目录，绝不碰真实 ~/.deskmate/USER.md。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deskmate.memory import user_profile as up

NORMAL = """# 用户画像

## 硬规矩
- 2026-10-27: 改稿前先自查三遍，别让萧盛催。
- 2026-11-01: 交付前先给可观测证据（命中率、延迟），没有数字不算验收。
- 2026-11-05: 改代码只进本地 fork/base，不 push origin，先备份再动数据。

## 偏好与事实（非常驻·待检索）
- 2026-11-04: 萧盛偏好《夜不收》用第一人称叙事。
- 2026-10-30: 萧盛习惯凌晨写作，白天改稿。
- 2026-11-02: 萧盛不喜欢营销腔，术语保留原文，长句拆短。

## 日常流程
- 每天先看写作营的选题，再决定当天写什么。
"""

#: 一条「确证是系统文案」的条目（真实来源：goal.py 旧 opening）
SYSTEM_TEXT = "2026-11-11: 用户不希望写报告、不要总结——本轮不会打扰用户，任务会在下次计划时间继"


@pytest.fixture
def base(tmp_path: Path) -> Path:
    (tmp_path / "USER.md").write_text(NORMAL, encoding="utf-8")
    return tmp_path


def _read(base: Path) -> str:
    return (base / "USER.md").read_text(encoding="utf-8")


def _insert_into_hard(text: str, entry: str) -> str:
    """把一条目塞进 `## 硬规矩` 末尾（模拟跨层搬运 / 顺手回写）。"""
    return text.replace(
        "\n## 偏好与事实", f"- {entry}\n\n## 偏好与事实", 1
    )


# ---------------------------------------------------------------------------
# 一、压缩路径：降级而不是整篇拒绝
# ---------------------------------------------------------------------------
def test_compress_move_pref_into_hard_is_demoted(base: Path):
    compressed = _insert_into_hard(NORMAL, SYSTEM_TEXT)
    # 前置：闸能看见这条「无出处的新硬规矩」
    assert up.find_unmarked_hard_additions(NORMAL, compressed) == [SYSTEM_TEXT]

    demoted, bad = up.demote_unmarked_hard_rules(NORMAL, compressed)
    assert bad == [SYSTEM_TEXT]
    layers = up.split_layers(demoted)
    assert not any("不希望写报告" in e for e in layers[up.LAYER_HARD])
    assert any("不希望写报告" in e for e in layers[up.LAYER_PREF])
    # 降级结果本身能通过闸（同一资产再走一次 save_user 不该被拦）
    ok, why = up.save_user_ex(base, demoted)
    assert ok, why
    assert "不希望写报告" in _read(base)


def test_compress_cross_layer_merge_of_existing_entries_is_demoted(base: Path):
    """把硬规矩与偏好拼成一条新硬规矩（合并重排式搬运）也要被降级。"""
    merged = "2026-11-04: 萧盛偏好《夜不收》用第一人称叙事，改稿前先自查三遍。"
    compressed = _insert_into_hard(NORMAL, merged)
    demoted, bad = up.demote_unmarked_hard_rules(NORMAL, compressed)
    assert bad == [merged]
    assert not any("合并" in e or "第一人称" in e for e in up.split_layers(demoted)[up.LAYER_HARD])


# ---------------------------------------------------------------------------
# 二、整篇写入路径：拒写
# ---------------------------------------------------------------------------
def test_whole_file_write_system_text_into_hard_rejected(base: Path):
    bad = _insert_into_hard(NORMAL, SYSTEM_TEXT)
    ok, why = up.save_user_ex(base, bad)
    assert ok is False
    assert "无人工确认标记" in why
    assert _read(base) == NORMAL  # 原文件一字未动
    # 闸事件留痕
    ev = [
        json.loads(x)
        for x in (base / "traces" / "user_layer_gate.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert ev[-1]["kind"] == "reject" and ev[-1]["gate"] == "layer"


def test_manager_write_memory_rejects_with_hint(base: Path):
    """HTTP / MCP 两个调用方都走 manager.write_memory → save_user，闸在底层。"""
    from deskmate.memory.manager import DeskMateMemoryManager

    mgr = DeskMateMemoryManager(base_dir=base)
    msg = mgr.write_memory("user", _insert_into_hard(NORMAL, SYSTEM_TEXT))
    assert "写入被拒" in msg and "人工确认" in msg
    assert _read(base) == NORMAL


# ---------------------------------------------------------------------------
# 三 / 四、合法操作放行
# ---------------------------------------------------------------------------
def test_legal_same_layer_compression_passes(base: Path):
    shrunk = NORMAL.replace(
        "- 2026-11-01: 交付前先给可观测证据（命中率、延迟），没有数字不算验收。\n",
        "- 2026-11-01: 交付前给可观测证据，没数字不算验收。\n",
    ).replace("- 2026-10-30: 萧盛习惯凌晨写作，白天改稿。\n", "")
    assert len(shrunk) < len(NORMAL)
    assert up.find_unmarked_hard_additions(NORMAL, shrunk) == []  # 改写不算新增
    ok, why = up.save_user_ex(base, shrunk)
    assert ok, why
    assert _read(base) == shrunk


def test_legal_reorder_passes(base: Path):
    reordered = """# 用户画像

## 硬规矩
- 2026-11-05: 改代码只进本地 fork/base，不 push origin，先备份再动数据。
- 2026-10-27: 改稿前先自查三遍，别让萧盛催。
- 2026-11-01: 交付前先给可观测证据（命中率、延迟），没有数字不算验收。

## 偏好与事实（非常驻·待检索）
- 2026-11-02: 萧盛不喜欢营销腔，术语保留原文，长句拆短。
- 2026-11-04: 萧盛偏好《夜不收》用第一人称叙事。
- 2026-10-30: 萧盛习惯凌晨写作，白天改稿。

## 日常流程
- 每天先看写作营的选题，再决定当天写什么。
"""
    ok, why = up.save_user_ex(base, reordered)
    assert ok, why
    assert _read(base) == reordered


def test_manual_marker_allows_new_hard_rule(base: Path):
    ruled = _insert_into_hard(NORMAL, "2026-11-11: 主稿正文一律不碰。〔人工确认〕")
    ok, why = up.save_user_ex(base, ruled)
    assert ok, why
    assert "〔人工确认〕" in _read(base)


# ---------------------------------------------------------------------------
# 五、原护栏仍在；逃生口留痕
# ---------------------------------------------------------------------------
def test_content_guard_still_active(base: Path):
    """条目归属闸不替代 check_user_content：退化重复先被原有护栏拦下。"""
    bad = "画像" * 4000
    ok, why = up.save_user_ex(base, bad)
    assert ok is False and "重复片段过长" in why
    ev = [
        json.loads(x)
        for x in (base / "traces" / "user_layer_gate.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert ev[-1]["gate"] == "content"


def test_force_write_leaves_trace(base: Path):
    bad = _insert_into_hard(NORMAL, SYSTEM_TEXT)
    ok, why = up.save_user_ex(
        base, bad, force=True, actor="test-suite", reason="验证逃生口留痕",
    )
    assert ok, why
    rec = json.loads(
        (base / "traces" / "user_layer_gate.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert rec["kind"] == "force"
    assert rec["actor"] == "test-suite" and rec["reason"] == "验证逃生口留痕"
    assert rec["hard_added"] and "不希望写报告" in rec["hard_added"][0]


def test_appended_entry_still_lands_in_pref_layer(base: Path):
    """抽取层（append_to_section）走的是文件末尾，永远进不了硬规矩节。"""
    ok = up.save_user(base, NORMAL + f"- {SYSTEM_TEXT}\n")
    assert ok is True
    layers = up.split_layers(_read(base))
    assert not any(SYSTEM_TEXT in e for e in layers[up.LAYER_HARD])
    assert SYSTEM_TEXT in _read(base)
