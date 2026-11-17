"""web_search 的主备降级链（2026-11-17）。

事故背景：专家 900s 里一次书都没读，全烧在 ddgs「失败→换词重试」的循环里——
那时 web_search 只有一条车道，挂了就只会把异常原样丢给模型，模型只能换个词再撞。

现在的契约：主车道（配置的 provider，当前是豆包搜索，实测 1.5s）报错/超时/零结果时，
自动降到免密钥 ddgs 再查一次；返回体带 `degraded` + `failed` 留痕，让「谁挂了」
这件事可复盘，而不是静默降级。

全部用假 provider，不打网络。
"""

from __future__ import annotations

import pytest

from coworker.secrets import SecretStore
from coworker.web import SearchResult, make_web_search_tool
from coworker.web import tool as web_tool
from coworker.web.providers import WebSearchProvider


class Lane(WebSearchProvider):
    """一条假车道：要么给结果，要么抛错，要么返回空。"""

    def __init__(self, name: str, *, results=None, exc: Exception | None = None):
        self.name = name
        self.results = results
        self.exc = exc
        self.calls = 0

    def search(self, query, max_results=5, timelimit=None):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        if self.results is not None:
            return list(self.results)
        return [SearchResult(title="t", url="https://x/1", snippet="s")]


@pytest.fixture
def secrets(tmp_path):
    """主车道 = doubao（与实盘配置一致）。"""
    s = SecretStore(tmp_path / "secrets.json")
    s.put("web_search:default", {"provider": "doubao", "api_key": "ark-test"})
    return s


def _patch_lanes(monkeypatch, **lanes: Lane) -> None:
    """把 build_provider 换成查表：name → Lane。未登记的名字一律当 ddgs。"""

    def fake_build(name, api_key=None):
        if name in lanes:
            return lanes[name]
        return lanes["duckduckgo"]

    monkeypatch.setattr(web_tool, "build_provider", fake_build)


def test_primary_ok_never_touches_fallback(secrets, monkeypatch):
    primary = Lane("doubao")
    backup = Lane("duckduckgo")
    _patch_lanes(monkeypatch, doubao=primary, duckduckgo=backup)

    out = make_web_search_tool(secrets)(query="朝鲜 士祸")

    assert out["provider"] == "doubao"
    assert len(out["results"]) == 1
    assert backup.calls == 0
    # 没降级就不该出现降级标记——否则复盘时分不清"一直正常"和"一直在兜底"
    assert "degraded" not in out and "failed" not in out


def test_primary_error_falls_back_to_keyless(secrets, monkeypatch):
    primary = Lane("doubao", exc=RuntimeError("429 rate limited"))
    backup = Lane("duckduckgo")
    _patch_lanes(monkeypatch, doubao=primary, duckduckgo=backup)

    out = make_web_search_tool(secrets)(query="朝鲜 士祸")

    assert out["provider"] == "duckduckgo"  # 备胎接住了
    assert out["degraded"] is True
    assert out["failed"][0]["provider"] == "doubao"
    assert "429" in out["failed"][0]["error"]
    assert primary.calls == 1 and backup.calls == 1


def test_primary_empty_results_also_falls_back(secrets, monkeypatch):
    """零结果也算主车道没办成——查不到不等于不存在，得让备胎再试一次。"""
    primary = Lane("doubao", results=[])
    backup = Lane("duckduckgo")
    _patch_lanes(monkeypatch, doubao=primary, duckduckgo=backup)

    out = make_web_search_tool(secrets)(query="q")

    assert out["provider"] == "duckduckgo"
    assert out["failed"][0]["error"] == "no results"


def test_all_lanes_failing_yields_one_error_with_reasons(secrets, monkeypatch):
    primary = Lane("doubao", exc=RuntimeError("boom"))
    backup = Lane("duckduckgo", results=[])
    _patch_lanes(monkeypatch, doubao=primary, duckduckgo=backup)

    out = make_web_search_tool(secrets)(query="q")

    assert "所有车道都没结果" in out["error"]
    assert out["provider"] == "doubao"  # 报错时给的是"本来是主车道"
    assert [f["provider"] for f in out["tried"]] == ["doubao", "duckduckgo"]


def test_missing_key_skips_lane_instead_of_aborting(secrets, monkeypatch):
    """主车道缺 key（build_provider 抛 ValueError）不该中断整条链。"""

    def fake_build(name, api_key=None):
        if name == "doubao":
            raise ValueError("web search provider 'doubao' needs an API key")
        return Lane("duckduckgo")

    monkeypatch.setattr(web_tool, "build_provider", fake_build)

    out = make_web_search_tool(secrets)(query="q")

    assert out["provider"] == "duckduckgo"
    assert "needs an API key" in out["failed"][0]["error"]


def test_duckduckgo_primary_has_no_second_lane(tmp_path, monkeypatch):
    """主车道本身就是末位时不该自己兜自己。"""
    s = SecretStore(tmp_path / "s.json")  # 无 profile → 默认 duckduckgo
    solo = Lane("duckduckgo", exc=RuntimeError("down"))
    _patch_lanes(monkeypatch, duckduckgo=solo)

    out = make_web_search_tool(s)(query="q")

    assert solo.calls == 1  # 只试一次
    assert "所有车道都没结果" in out["error"]


def test_injected_provider_stays_single_lane(monkeypatch):
    """显式注入的 provider 不参与降级：注入者要的就是它。"""
    injected = Lane("boom", exc=RuntimeError("network down"))

    def fake_build(name, api_key=None):  # 若被调用就说明错误地降级了
        raise AssertionError("不应构建备胎")

    monkeypatch.setattr(web_tool, "build_provider", fake_build)

    out = make_web_search_tool(provider=injected)(query="q")

    assert "web search failed" in out["error"] and out["provider"] == "boom"


def test_empty_query_short_circuits(secrets, monkeypatch):
    primary = Lane("doubao")
    _patch_lanes(monkeypatch, doubao=primary)

    out = make_web_search_tool(secrets)(query="   ")

    assert out["error"] == "query 不能为空"
    assert primary.calls == 0
