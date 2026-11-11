"""B 方案（2026-11-11 萧哥拍板）：每位专家自带一本**独门手艺** SKILL.md。

目标唯一：「让专家更专业」＝判断更可核验。不做更像个人、不加自传/情绪/关系/回访。

形态刻意走 OPE-58 bundle（``<id>/manifest.md`` + ``<id>/skills/<id>-method/``），
而不是在扁平 ``<id>.md`` 上加 ``skills:``：扁平 persona 的
``SessionManager.persona_skill_scope`` 会解析出 ``builtin/skills`` 这个**共享目录**，
11 本手艺会漏进小桔 / 其他 persona 的技能菜单 —— 直接违反 11-09「专家隔离不回退」。

本组锁四件事：
1. 11 位专家各自声明并实际携带一本自己的手艺；
2. 手艺只在本人 1v1 会话可见，别人（含小桔）看不到；
3. 挂载点真的可用（``persona_skill_scope`` → ``load_skill``）；
4. 每本手艺都带「判断可核验」的四条纪律，且守 11-01 的交付形态红线
   （只取内核方法论，不端工程文档外壳）。

先红后绿：本文件在 bundle 与 SKILL.md 落地之前应全部失败。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coworker.personas.registry import PersonaRegistry
from coworker.providers import ModelCapabilities, ProviderClient
from coworker.server.manager import SessionManager
from coworker.sessions import SessionRecord

# 与 deskmate/bots/author.py::EXPERT_PERSONA_IDS 同源（11 位写作专家）。
EXPERT_IDS = (
    "brainstormer", "lit-editor", "adaptation", "historian",
    "proofreader", "reader", "publisher", "genre", "editor",
    "outline", "character",
)


def _skill_name(pid: str) -> str:
    return f"{pid}-method"


def _reg(tmp_path: Path) -> PersonaRegistry:
    return PersonaRegistry(state_path=tmp_path / "personas.json")


class ScriptedProvider(ProviderClient):
    def complete(self, *, model, messages, tools=None, **settings):
        raise AssertionError("no turns expected")

    def capabilities(self, model):
        return ModelCapabilities()


# ----------------------------------------------------------- 一、每人一本

@pytest.mark.parametrize("pid", EXPERT_IDS)
def test_every_expert_ships_its_own_craft_skill(pid: str, tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    entry = reg.get(pid)
    assert entry is not None and entry.manifest is not None
    m = entry.manifest
    assert _skill_name(pid) in m.skills, f"{pid} manifest 没声明自己的手艺"

    skill = Path(m.source).parent / "skills" / _skill_name(pid) / "SKILL.md"
    assert skill.is_file(), f"{pid} 声明了手艺但盘上没有 SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert f"name: {_skill_name(pid)}" in text
    assert "description:" in text
    assert len(text) > 800, "独门手艺不能是几句口号"


def test_no_expert_declares_another_experts_skill(tmp_path: Path) -> None:
    """每本手艺只有一个主人；共享/复用的手艺不叫「独门」。"""
    reg = _reg(tmp_path)
    all_names = {_skill_name(p) for p in EXPERT_IDS}
    for pid in EXPERT_IDS:
        declared = set(reg.get(pid).manifest.skills)
        assert declared == {_skill_name(pid)}, f"{pid} 的手艺声明不独立：{declared}"
        assert not (declared & (all_names - {_skill_name(pid)}))


# ------------------------------------------------- 二、隔离（11-09 红线不回退）

def test_craft_skills_stay_private_to_their_owner(tmp_path: Path, monkeypatch) -> None:
    """每人只看见自己的手艺；别人的一本都不许漏进来。"""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    mgr = SessionManager(workspace=tmp_path, provider=ScriptedProvider())
    for i, pid in enumerate(EXPERT_IDS):
        sid = f"expert-{i}"
        mgr.session_store.save(
            SessionRecord(
                session_id=sid, workspace="", model="m",
                mode="interactive", agent=pid,
            )
        )
        names = mgr.effective_skill_names(sid)
        assert _skill_name(pid) in names, f"{pid} 在 1v1 看不到自己的手艺"
        others = {_skill_name(p) for p in EXPERT_IDS if p != pid}
        assert not (others & names), f"{pid} 看见了别人的手艺：{others & names}"


def test_xiaoju_sees_no_expert_craft_skills(tmp_path: Path, monkeypatch) -> None:
    """小桔本体走「越来越懂你」，绝不借附体专家的资产（双方向契约）。"""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    mgr = SessionManager(workspace=tmp_path, provider=ScriptedProvider())
    mgr.session_store.save(
        SessionRecord(
            session_id="xiaoju", workspace="", model="m",
            mode="interactive", agent="deskmate",
        )
    )
    names = mgr.effective_skill_names("xiaoju")
    leaked = {_skill_name(p) for p in EXPERT_IDS} & names
    assert not leaked, f"专家手艺漏进小桔技能池：{leaked}"


# ------------------------------------------------------------- 三、挂载可用

@pytest.mark.parametrize("pid", EXPERT_IDS)
def test_persona_skill_scope_resolves_own_bundle(pid: str, tmp_path: Path, monkeypatch) -> None:
    """OPE-58 挂载点真的可用：目录 + allowlist 都指向本人 bundle。"""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    mgr = SessionManager(workspace=tmp_path, provider=ScriptedProvider())
    d, allow = mgr.persona_skill_scope(pid)
    assert d is not None and d.is_dir(), f"{pid} 的 skills/ 没被解析到"
    assert d.name == "skills" and d.parent.name == pid, f"{pid} 挂到了共享目录：{d}"
    assert allow == {_skill_name(pid)}, f"{pid} 的 allowlist 不对：{allow}"
    assert (d / _skill_name(pid) / "SKILL.md").is_file()


@pytest.mark.parametrize("pid", EXPERT_IDS)
def test_load_skill_actually_returns_own_craft(pid: str, tmp_path: Path, monkeypatch) -> None:
    """菜单里看得见还不够，load_skill 必须真能取到（engine 侧 extra_skill_dirs）。"""
    from coworker.skills.base import SkillLoader, skill_tools

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    mgr = SessionManager(workspace=tmp_path, provider=ScriptedProvider())
    d, allow = mgr.persona_skill_scope(pid)
    loader = SkillLoader([d]) if d is not None else SkillLoader([])
    tool = skill_tools(loader, allowed=allow)[0]
    out = tool.fn(_skill_name(pid)) if hasattr(tool, "fn") else None
    # ai.tool 包装形态随版本变化；拿不到 fn 时退而验证 loader 本身。
    if out is None:
        assert loader.get(_skill_name(pid)) is not None
    else:
        assert "error" not in out
        assert "逐字" in out["instructions"] or "正文" in out["instructions"]


# --------------------------------------------- 四、判断可核验 + 11-01 形态红线

@pytest.mark.parametrize("pid", EXPERT_IDS)
def test_craft_skill_carries_verifiability_discipline(pid: str, tmp_path: Path) -> None:
    """每本手艺都必须把「判断更可核验」落成纪律，而不是加人设/加情绪。"""
    reg = _reg(tmp_path)
    m = reg.get(pid).manifest
    text = (Path(m.source).parent / "skills" / _skill_name(pid) / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "逐字" in text or "原文" in text, f"{pid} 手艺没要求逐字引文"
    assert "覆盖率" in text or "未加载" in text, f"{pid} 手艺没接上地基的覆盖率诚实"
    assert "二手" in text or "参考层" in text, f"{pid} 手艺没挡二手文档"
    assert "作者裁定" in text, f"{pid} 手艺没写清效力顺序"
    assert "否决线" in text or "不过" in text, f"{pid} 手艺没有否决线"


@pytest.mark.parametrize("pid", EXPERT_IDS)
def test_craft_skill_keeps_2026_11_01_delivery_redline(pid: str, tmp_path: Path) -> None:
    """11-01 草案 §七/§九：只取内核方法论，不用工程外壳，像作家在聊构思。"""
    reg = _reg(tmp_path)
    m = reg.get(pid).manifest
    text = (Path(m.source).parent / "skills" / _skill_name(pid) / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "像作家" in text or "不交" in text, f"{pid} 手艺没锁交付形态"
    assert "判据" in text, f"{pid} 手艺没有内核判据"
