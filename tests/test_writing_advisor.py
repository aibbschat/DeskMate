"""P1-5 写作顾问 hermetic 回归：worldview 为空 / 无 LLM / 成功路径。

全程用临时库（tmp_path）隔离，不碰真实 ~/.deskmate。
后端是只读消费 worldview + 一次 LLM 推理，fail-open：每个异常分支都要优雅降级，不抛、不假结果。
"""

import importlib

import pytest

MOD = "deskmate.cognitive.writing_advisor"


def _mod():
    return importlib.import_module(MOD)


class _FakeProfile:
    """让 worldview 层判据认为「她有自己的看法」。"""

    def __init__(self):
        self.stances = ["我相信：看清谁在争，比站队更重要。"]
        self.value_rankings = []
        self.thinking_frameworks = []


def test_too_short_returns_reason(monkeypatch, tmp_path):
    """草稿过短 → ok=False, reason=too_short，不做任何外部调用。"""
    res = _mod().advise(str(tmp_path), "短", "plot")
    assert res["ok"] is False
    assert res["reason"] == "too_short"


def test_no_worldview_graceful(monkeypatch, tmp_path):
    """worldview 层为空（全新库）→ ok=True（设计内降级），advice 是诚实的「还在形成看法」。"""
    res = _mod().advise(str(tmp_path), "一段足够长的情节：主角在城破前夜犹豫是否投降。", "plot")
    assert res["ok"] is True
    assert res["reason"] == "no_worldview"
    assert "还在形成" in res["advice"]


def test_no_llm_graceful(monkeypatch, tmp_path):
    """worldview 有内容但 LLM 未就绪 → ok=False, reason=no_provider（不白烧调用）。"""
    m = _mod()
    monkeypatch.setattr(f"{MOD}.load_cognitive_profile", lambda *a, **k: _FakeProfile())
    monkeypatch.setattr(f"{MOD}.load_worldview_beliefs", lambda base_dir: "我相信：看清谁在争，比站队更重要。")
    monkeypatch.setattr(f"{MOD}.memory_llm_available", lambda: False)
    res = m.advise(str(tmp_path), "一段足够长的情节：主角在城破前夜犹豫是否投降。", "plot")
    assert res["ok"] is False
    assert res["reason"] == "no_provider"


def test_success_returns_advice(monkeypatch, tmp_path):
    """worldview 有内容 + LLM 就绪 → ok=True，返回她的看法，worldview_used=True。"""
    m = _mod()
    fake_worldview = "我相信：看清谁在争，比站队更重要。\n我看重：事实准确 > 立场鲜明。"
    fake_advice = (
        "这段情节里最让我别扭的是：主角的犹豫没有来由。按我对权力的理解，城破前夜的动摇，"
        "往往不是怕死，是怕背锅——你没把这一层写出来，人物就空了。这不是技巧问题，是你看人还没看透。"
    )
    monkeypatch.setattr(f"{MOD}.load_cognitive_profile", lambda *a, **k: _FakeProfile())
    monkeypatch.setattr(f"{MOD}.load_worldview_beliefs", lambda base_dir: fake_worldview)
    monkeypatch.setattr(f"{MOD}.memory_llm_available", lambda: True)
    monkeypatch.setattr(f"{MOD}.chat_memory", lambda *a, **k: fake_advice)
    res = m.advise(str(tmp_path), "一段足够长的情节：主角在城破前夜犹豫是否投降。", "plot")
    assert res["ok"] is True
    assert res["worldview_used"] is True
    assert res["advice"] == fake_advice
    assert res["reason"] == ""


def test_kind_passthrough(monkeypatch, tmp_path):
    """kind 透传：后端不改 kind，前端用它渲染「小桔的视角（书评）」。"""
    m = _mod()
    monkeypatch.setattr(f"{MOD}.load_cognitive_profile", lambda *a, **k: _FakeProfile())
    monkeypatch.setattr(f"{MOD}.load_worldview_beliefs", lambda base_dir: "我相信：写人要先写处境。")
    monkeypatch.setattr(f"{MOD}.memory_llm_available", lambda: True)
    monkeypatch.setattr(f"{MOD}.chat_memory", lambda *a, **k: "你的书评立场我大体认，只有一处我想反驳——你把明朝的灭亡全归到财政上，太顺了。按我的镜头，财政是表象，真正动摇的是大家对「天命」还信不信。这一层你没碰，文章就停在表面。")
    res = m.advise(str(tmp_path), "一篇足够长的书评：作者认为明朝的灭亡源于财政崩溃。", "review")
    assert res["ok"] is True
    assert res["kind"] == "review"
