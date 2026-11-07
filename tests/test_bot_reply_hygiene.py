"""Bot 群发言卫生回归 —— 署名前缀 / 具体度（2026-11-06 真机实测翻车点）。

真机证据：补厚人设 + 新增创意脑洞专家后，群里说「先读《夜不收》，给三个方向」
产出了 1676 字、具体到「伏笔埋在第九章／读者翻到哪句会愣住」的高质量建议，
但开头仍带模型自加的 ``（来自 @brainstormer · 创意脑洞专家）``。

两个根因：
1. ``_GROUP_GUIDANCE`` 只是**在 prompt 里请求**模型别加署名，模型不遵守就直接
   漏到群里 → 必须在落库前做确定性剥离，不靠模型自觉。
2. 同一段里还压着「一次发言简短（100~300 字）」，与萧哥要的「具体到能落笔」
   **直接矛盾**：这次模型碰巧选了人设，换个轮次/通道就可能又被压回空泛。

本组用例锁住：剥离是确定性的（两条发言路径都生效）、正文字句不被误删、
群聊层不再设硬字数上限。注意「真实优先」红线：这里剥的是模型自加的元数据
噪声，不是约束表达方式；改字数约束是**撤掉**硬约束，不是新增话术模板。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from coworker.providers.base import AssistantTurn, ModelCapabilities, StreamChunk
from deskmate.bots import runtime as rt

_BODY = (
    "我先说清楚我读到哪儿：正文断在第十一章末——孔大贵死在炸开的仓门口。\n\n"
    "**方向一：那枚信号弹不是求救，是报数**\n\n"
    "伏笔在第九章，读者翻到宁靖放弹那一页会愣住。"
)
_PREFIXED = "（来自 @brainstormer · 创意脑洞专家）\n\n" + _BODY


def test_strip_self_attribution_removes_only_the_attribution_line() -> None:
    """确定性剥离：只删开头那一行署名，正文一个字不动。"""
    assert rt.strip_self_attribution(_PREFIXED) == _BODY
    assert rt.strip_self_attribution("（来自 @historian）\n" + _BODY) == _BODY
    assert rt.strip_self_attribution("(来自 @reader · 读者视角)\n\n" + _BODY) == _BODY
    assert rt.strip_self_attribution("@brainstormer：\n" + _BODY) == _BODY


def test_strip_self_attribution_keeps_legitimate_parentheses() -> None:
    """正文本身的括号说明不能被误删（只认「来自 @…」这种署名行）。"""
    legit = "（读者会愣住）这不是署名。\n" + _BODY
    assert rt.strip_self_attribution(legit) == legit
    assert rt.strip_self_attribution(_BODY) == _BODY
    assert rt.strip_self_attribution("") == ""


class _PrefixedProvider:
    """离线 provider：故意让模型自加署名，验证落库前一定被剥掉。"""

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    def complete(self, *, model, messages, tools=None, **settings):
        return AssistantTurn(text=_PREFIXED)

    def stream(self, *, model, messages, tools=None, **settings):
        step = max(1, len(_PREFIXED) // 4)
        for i in range(0, len(_PREFIXED), step):
            yield StreamChunk(text_delta=_PREFIXED[i : i + step])
        yield StreamChunk(turn=AssistantTurn(text=_PREFIXED))


def _runtime(tmp_path: Path) -> rt.BotRuntime:
    shelf = tmp_path / "books"
    (shelf / "16.夜不收").mkdir(parents=True)
    (shelf / "16.夜不收" / "第三章 烧仓.txt").write_text("三更仓场起火。", encoding="utf-8")
    return rt.BotRuntime(
        books_root=shelf,
        workspace_root=tmp_path / "ws",
        provider=_PrefixedProvider(),
        model="test-model",
    )


def test_speak_path_strips_prefix(tmp_path: Path) -> None:
    """旧 /say 契约：整段返回前必须剥掉署名。"""
    r = _runtime(tmp_path)
    try:
        out = r.speak("brainstormer", "聊聊烧仓")
        assert out == _BODY, f"署名漏到落库文本: {out[:80]!r}"
    finally:
        r.close()


def test_speak_stream_final_strips_prefix(tmp_path: Path) -> None:
    """/stream 契约：delta 已逐字推给前端，final 必须剥掉署名供气泡覆盖。"""

    async def _collect() -> list[dict[str, Any]]:
        return [ev async for ev in r.speak_stream("brainstormer", "聊聊烧仓")]

    r = _runtime(tmp_path)
    try:
        events = asyncio.run(_collect())
        finals = [e for e in events if e.get("type") == "final"]
        assert len(finals) == 1
        assert finals[0]["text"] == _BODY, f"final 仍带署名: {finals[0]['text'][:80]!r}"
        # delta 拼接是原始流（含署名），但前端以 final 覆盖气泡
        assert "（来自" in "".join(
            str(e.get("text") or "") for e in events if e.get("type") == "delta"
        )
    finally:
        r.close()


def test_group_guidance_no_hard_length_cap() -> None:
    """群聊层不得设数字型字数上限；具体度锚点永在（防 11-06 空泛复发）。

    设计变更史（两次都有真机证据，方向相反的两种病）：
    - 11-06：撤「100~300 字」硬上限——它与「具体到能落笔」直接矛盾，模型会
      砍细节凑字数。当时也不留任何长度暗示，防的是**换皮软上限**。
    - 11-07：引入**分层形态**指引（萧哥拍板换骨架）——真机 11-06 夜《夜不收》
      定义讨论里全员 1500~2500 字 markdown 文档体，接话轮也是整篇文档。
      新版指引「首轮可以展开说透；接话轮常常三五句就够」：治的是体例与
      轮次分工，不是内容长度——短的是接话，不是首轮展开，更不是数字上限。

    永不变的三条：①数字上限（100~300 / 300 字）永不回来；②「具体 / 伏笔 /
    愣住」的具体度锚点永在——空泛和文档体是两种病，都要防；③「常常…就够」
    必须是弹性措辞，不许绝对化成「必须三五句」的新软上限。
    """
    g = rt._GROUP_GUIDANCE
    assert "100~300" not in g
    assert "300 字" not in g
    assert "长篇" not in g
    assert "才简短" not in g
    # 具体度锚点（防空泛复发）
    assert "具体" in g and "伏笔" in g and "愣住" in g
    # 分层形态：首轮可展开、接话轮短，且「短」带「常常」的弹性
    assert "展开说透" in g
    assert "常常三五句就够" in g
