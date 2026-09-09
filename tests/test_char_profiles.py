"""人物小传质量锚（防人物漂移）回归测试。

覆盖：
- LLM 路径抽取出结构化小传；
- LLM 不可用降级抽取（从圣经抽「人物」段）非空、不崩；
- 空源返回占位说明（不抛异常）；
- draft_outline / draft_screenplay 把 character_profiles 透传给 executor；
- 落盘后重建 pipeline 仍能恢复 _char_profiles（跨重启/跨生成复用）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def _tmp_base():
    return tempfile.mkdtemp(prefix="ow_char_")


def _create_episodic(tmp, episodes=2):
    from deskmate.creation import service

    out = service.create_pipeline(
        "screenplay", "测试概念", executor_kind="stub",
        base_dir=tmp, episodes=episodes,
    )
    pid = out["pid"]
    service.draft_outline(pid)
    service.review_outline(pid)
    service.lead_outline(pid, True)
    return pid


_FAKE_BIBLE = "# 《测试》改编圣经\n\n## 人物表\n1. 李白：诗人，狂放不羁\n2. 杜甫：诗圣，沉郁顿挫\n"


def _fake_chat(system="", user="", **kw):
    if "角色策划" in (system or ""):
        return (
            "## 李白\n"
            "- 身份与社会关系：盛唐诗人，与杜甫为友\n"
            "- 核心目标：建功立业、施展抱负\n"
            "- 内心动机：报国之志与自由天性的张力\n"
            "- 性格弧光：狂放 → 失意沉郁\n"
            "- 关键关系与冲突：与权贵对立，冲突根源在仕途抱负\n"
            "- 人设红线：不可无端怯懦、不可背弃朋友\n"
        )
    if "文学编辑" in (system or ""):
        return _FAKE_BIBLE
    return "ok"


def test_extract_llm_path_produces_structured_profile():
    """LLM 可用时，extract_characters 产出含人物目标/动机的结构化小传。"""
    from deskmate.creation import service
    from unittest.mock import MagicMock

    tmp = _tmp_base()
    pid = _create_episodic(tmp, episodes=1)
    pl = service.get_pipeline(pid)
    pl._novel_context = lambda *a, **k: _FAKE_BIBLE  # 模拟已建好的圣经，跳过建索引

    with patch("deskmate.memory.llm.llm_available", return_value=True), \
         patch("deskmate.memory.llm.chat", side_effect=_fake_chat):
        doc = pl.extract_characters()
    assert "李白" in doc
    assert "核心目标" in doc and "内心动机" in doc
    # 幂等：已有小传不重复烧 token
    with patch("deskmate.memory.llm.chat", MagicMock(side_effect=_fake_chat)) as m:
        again = pl.extract_characters()
    assert again == doc
    assert m.call_count == 0


def test_extract_degrade_when_no_llm():
    """LLM 不可用时降级从圣经抽「人物」段，非空且不抛异常。"""
    from deskmate.creation import service

    tmp = _tmp_base()
    pid = _create_episodic(tmp, episodes=1)
    pl = service.get_pipeline(pid)
    pl._novel_context = lambda *a, **k: _FAKE_BIBLE

    with patch("deskmate.memory.llm.llm_available", return_value=False):
        doc = pl.extract_characters()
    assert "李白" in doc
    assert "人物表" in doc  # 降级抽到了圣经人物段
    assert "降级" in doc    # 带降级说明


def test_extract_empty_source_placeholder():
    """无原著无大纲时返回占位说明，不崩。"""
    from deskmate.creation import service

    tmp = _tmp_base()
    out = service.create_pipeline("outline", "概念", executor_kind="stub", base_dir=tmp)
    pid = out["pid"]
    pl = service.get_pipeline(pid)
    pl._novel_ctx = ""
    pl._drafts[pl.ids["outline_item"]] = ""

    with patch("deskmate.memory.llm.llm_available", return_value=False):
        doc = pl.extract_characters()
    assert "暂无" in doc


def test_char_profiles_injected_into_screenplay():
    """写剧本时 character_profiles 必须透传给 executor（防漂移锚点生效）。"""
    from deskmate.creation import service

    tmp = _tmp_base()
    pid = _create_episodic(tmp, episodes=2)
    pl = service.get_pipeline(pid)
    pl._char_profiles = "## 李白\n- 核心目标：建功立业"

    captured: dict = {}
    pl.executor.draft_screenplay = lambda *a, **k: captured.update(k) or "[剧本·stub] x"
    service.draft_screen(pid, episode=1)

    assert "character_profiles" in captured
    assert "李白" in captured["character_profiles"]


def test_char_profiles_injected_into_outline():
    """写大纲时 character_profiles 同样透传（大纲人物设定须以小传为准）。"""
    from deskmate.creation import service

    tmp = _tmp_base()
    out = service.create_pipeline("screenplay", "概念", executor_kind="stub", base_dir=tmp, episodes=2)
    pid = out["pid"]
    pl = service.get_pipeline(pid)
    pl._char_profiles = "## 王勃\n- 核心目标：文名传世"

    captured: dict = {}
    pl.executor.draft_outline = lambda *a, **k: captured.update(k) or "[大纲·stub] x"
    service.draft_outline(pid)

    assert "character_profiles" in captured
    assert "王勃" in captured["character_profiles"]


def test_char_profiles_persist_across_reload():
    """落盘后重建 pipeline（模拟重启），_char_profiles 必须恢复。"""
    from deskmate.creation import pipeline as P
    from deskmate.creation import service

    tmp = _tmp_base()
    out = service.create_pipeline("outline", "概念", executor_kind="stub", base_dir=tmp)
    pid = out["pid"]
    pl = service.get_pipeline(pid)
    pl._char_profiles = "## 杜甫\n- 核心目标：致君尧舜"
    pl._save_drafts()

    pl2 = P.CreativePipeline(space=f"creation:{pid}", base_dir=tmp)
    assert pl2._char_profiles == "## 杜甫\n- 核心目标：致君尧舜"


def test_service_extract_and_set():
    """service 层 extract_characters / set_char_profiles 接口可用且落盘。"""
    from deskmate.creation import service

    tmp = _tmp_base()
    pid = _create_episodic(tmp, episodes=1)
    pl = service.get_pipeline(pid)
    pl._novel_context = lambda *a, **k: _FAKE_BIBLE

    with patch("deskmate.memory.llm.llm_available", return_value=False):
        res = service.extract_characters(pid)
    assert res["ready"] is True
    assert "李白" in res["char_profiles"]

    # 用户手改后，snapshot 应可见且后续每集生效
    edited = "## 李白\n- 核心目标：归隐山林（用户改写）"
    service.set_char_profiles(pid, edited)
    snap = service.snapshot(pid)
    assert snap["char_profiles_ready"] is True
    assert "归隐山林" in snap["char_profiles"]
    assert service.get_pipeline(pid)._char_profiles == edited
