"""1v1 专家会话：笔记本工具 + 作者基础档案（2026-11-09）。

萧哥：「把 1v1 接上」「知道我是谁、是干什么的、写了哪些书」。这里锁住：
- 三位专家在 1v1（persona engine）里拿得到笔记本工具；
- 所有创作/分析专家的 system prompt 里有作者档案；cowork/小桔不重复注入。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from coworker.agent import build_engine
from coworker.personas.registry import get_registry


class _SilentProvider:
    def capabilities(self, model: str) -> Any:
        from coworker.providers.base import ModelCapabilities

        return ModelCapabilities()

    def complete(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import AssistantTurn

        return AssistantTurn(text="ok")

    def stream(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import AssistantTurn, StreamChunk

        yield StreamChunk(turn=AssistantTurn(text="ok"))


def _engine(persona: str, tmp_path: Path):
    # workspace=None：测试环境没有 sandbox-exec，不建 LocalExecutor；笔记本/作者档案
    # 不依赖 workspace，正好隔离验证这两件事。
    agent = get_registry().get(persona).agent()
    return build_engine(
        agent=agent,
        provider=_SilentProvider(),
        model="test-model",
        workspace=None,
    )


def test_1v1_brainstormer_gets_notebook_tools_and_author(tmp_path: Path) -> None:
    eng = _engine("brainstormer", tmp_path)
    names = set(eng.registry.names())
    assert {"notebook_read", "notebook_remember", "notebook_feedback", "notebook_rewrite"} <= names
    system = str(eng.messages[0].get("content") or "")
    assert "作者（你服务的人" in system
    assert "AI 不代笔" in system


def test_1v1_historian_gets_author_but_no_notebook(tmp_path: Path) -> None:
    eng = _engine("historian", tmp_path)
    names = set(eng.registry.names())
    assert not (names & {"notebook_read", "notebook_remember", "notebook_rewrite"})
    assert "作者（你服务的人" in str(eng.messages[0].get("content") or "")

def test_notebook_personas_have_conflict_rule() -> None:
    """冲突不带着旧判断说话：三位专家人设都要有「先按作者裁定/正文改」的规则。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "coworker/personas/builtin"
    for pid in ("brainstormer", "lit-editor", "adaptation"):
        text = (root / f"{pid}.md").read_text(encoding="utf-8")
        assert "冲突先改" in text, f"{pid} 缺少冲突修正规则"
        assert "作者裁定 > 正文" in text, f"{pid} 冲突优先级不明确"
