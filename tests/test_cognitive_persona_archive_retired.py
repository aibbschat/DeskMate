"""vault 静态档案「小桔眼中的萧盛.md」退役出注入链（2026-11-16 萧哥拍板）。

背景（根因见 .workbuddy/memory/2026-11-16.md 17:45）：
- 该文件是**派生视图**（docs/认知层架构文档.md:167、cognitive/store.py:23 都这么写），
  但全仓只有读它的代码、没有任何写回/导出器 ⇒ 2026-08-30 v1.0 之后永久冻结。
- 数据其实一直在动，全进了 DB（profile_items 142 cognitive + 21 emotional + 17 worldview）。
- 它一边过期一边霸占最贵的位置：认知块 cap=4500 保头切尾，旧档案 2645 字 100% 保留，
  尾部的 worldview（「你自己是谁」）每次都整段被切。

萧哥判断：「再建导出器就是重复了」⇒ 不续命，直接摘掉注入。
**人格不受影响**：真正的人格底座走 `coworker/personas/builtin/deskmate.md` 的
`persona_base: deskmate.persona:XIAOJU_SYSTEM`（manifest 通道），与本文件无关。

本契约锁三件事：
1. 认知注入段**不再含** vault 档案内容（过期派生视图不许回潮）。
2. `has_cognitive_data()` 不再把「档案存在」当作有认知数据的证据。
3. `load_persona_base()` 本体保留：vault 里的档案仍在，她可按需 `vault_read` 读到
   （退役的是注入，不是档案本身）。
"""

from unittest import mock

import pytest

from deskmate.cognitive import memory_injector as mi


_MARKER = "【这份静态档案的内容不该出现在认知注入里】"


# --------------------------------------------------------------------------- #
# 1. 注入段不再含档案内容
# --------------------------------------------------------------------------- #
def test_cognitive_context_excludes_vault_persona_archive():
    """即便 vault 档案存在且可读，认知注入也不得把它塞进去。"""
    with mock.patch.object(mi, "load_persona_base", return_value=_MARKER), \
         mock.patch.object(mi, "load_cognitive_profile", return_value=None):
        out = mi.build_cognitive_context()
    assert _MARKER not in out, "vault 静态档案回到了认知注入链（应已退役）"


def test_cognitive_context_still_renders_his_profile_without_archive():
    """摘掉档案不等于摘掉「她眼中的他」——DB 里的画像必须照常注入。"""
    class _P:
        stances = [mock.Mock(content="他讨厌把历史写成爽文", status="active", contexts=[])]
        argument_patterns = []
        value_rankings = []
        thinking_frameworks = []
        cognitive_gaps = []

    with mock.patch.object(mi, "load_persona_base", return_value=_MARKER), \
         mock.patch.object(mi, "load_cognitive_profile", return_value=_P()):
        out = mi.build_cognitive_context()

    assert _MARKER not in out
    assert "关于萧盛" in out, "「她眼中的他」段不该因摘档案而消失"
    assert "他讨厌把历史写成爽文" in out, "DB 里的认知条目必须照常注入"


def test_cognitive_context_renders_worldview_without_archive():
    """「你自己是谁」段同理：摘档案不能让世界观段消失。"""
    class _W:
        stances = [mock.Mock(content="我认定好故事得让人自己发现", status="active", contexts=[])]
        argument_patterns = []
        value_rankings = []
        thinking_frameworks = []
        cognitive_gaps = []

    def _fake_load(layer=None, **kw):
        return _W() if layer == mi._HER_LAYER else None

    with mock.patch.object(mi, "load_persona_base", return_value=_MARKER), \
         mock.patch.object(mi, "load_cognitive_profile", side_effect=_fake_load):
        out = mi.build_cognitive_context()

    assert _MARKER not in out
    assert "我认定好故事得让人自己发现" in out


# --------------------------------------------------------------------------- #
# 2. has_cognitive_data 不再以档案存在为判据
# --------------------------------------------------------------------------- #
def test_has_cognitive_data_ignores_archive_only():
    """只有 vault 档案、DB 三层全空 → 必须判 False（档案不是认知数据）。"""
    with mock.patch.object(mi, "load_persona_base", return_value=_MARKER), \
         mock.patch.object(mi, "load_cognitive_profile", return_value=None):
        assert mi.has_cognitive_data() is False


def test_has_cognitive_data_true_on_db_layers():
    """DB 里有任一层数据 → 仍判 True（能力没被摘坏）。"""
    with mock.patch.object(mi, "load_persona_base", return_value=""), \
         mock.patch.object(mi, "load_cognitive_profile", return_value={"k": 1}):
        assert mi.has_cognitive_data() is True


# --------------------------------------------------------------------------- #
# 3. 档案本体与按需读取通道保留
# --------------------------------------------------------------------------- #
def test_archive_file_still_exists_and_loadable(tmp_path, monkeypatch):
    """退役的是注入，不是档案：文件在就仍能被 load_persona_base 读到（供按需）。"""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    (vault / mi._PERSONA_FILE).write_text("档案正文", encoding="utf-8")
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    assert mi.load_persona_base() == "档案正文"


def test_archive_missing_is_fail_open(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    assert mi.load_persona_base() == ""


# --------------------------------------------------------------------------- #
# 4. 人格通道不受影响（防误伤：真正的人格底座在 manifest）
# --------------------------------------------------------------------------- #
def test_persona_base_channel_unaffected():
    """人格仍由 deskmate.md 的 persona_base 从 persona.py 注入，与本档案无关。"""
    from pathlib import Path

    from coworker.personas.manifest import load_manifest_file
    from deskmate.persona import XIAOJU_SYSTEM

    sp = load_manifest_file(
        Path("coworker/personas/builtin/deskmate.md"), builtin=True
    ).system_prompt
    # 底座首句必在（证明 manifest 通道活着），且不是从 vault 档案来的
    assert XIAOJU_SYSTEM.splitlines()[0][:20] in sp
    assert "小桔眼中的萧盛" not in sp


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
