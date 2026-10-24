"""deskmate-proactivity MCP 服务（stdio）— 主动推理工具。

通过 OpenWorker 的 MCP 机制暴露主动推理工具。运行方式：
    <venv>/bin/python -m deskmate.proactivity.server

工具列表：
- proactive_tick      执行一次主动推理 tick（感知→判断→决策）
- proactive_capture    把用户「记住这个」写入主动台账
- proactive_lookup     按 #id 短句柄/关键词回查台账（§11.16 层二：接自己递的卡片）
- proactive_next_steps 查一条台账项的预置下一步（阶段六任务直达）
- proactive_start_task 开始一个台账下一步任务（置进行中并返回任务指令）
- proactive_finish_task 结束一个台账下一步任务（done/dismiss/later）
- proactive_feedback   记录用户对建议的反馈
- proactive_silent     设置静默模式
- proactive_stats      获取防打扰统计
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .ledger import is_mobile_kind_allowed
from .tick import ProactiveEngine

mcp = FastMCP("deskmate-proactivity")

def _base_dir() -> Path:
    """**调用时**解析数据目录。

    禁模块级常量（2026-09-22 收口）：import 期求值有两个害处——
    (a) 把真实 ``~/.deskmate`` 绑死，而 conftest 的 autouse fixture 在
        import **之后**才 setenv，隔离全线失效；
    (b) 只 import 本模块就产生「构造引擎 + 读盘」的副作用。
    """
    from deskmate.paths import deskmate_base_dir

    return deskmate_base_dir()


# 全局引擎单例（惰性：首次调用才构造并加载防打扰状态）
_engine: ProactiveEngine | None = None


def _get_engine() -> ProactiveEngine:
    global _engine
    if _engine is None:
        _engine = ProactiveEngine(base_dir=_base_dir())
        # 加载防打扰持久化数据
        _engine.disturb.persist(_base_dir() / "memory" / "disturb_state.json")
    return _engine


@mcp.tool()
def proactive_tick(
    user_message: str = "",
    workspace: str = "",
    memory_context: str = "",
    project_context: str = "",
) -> str:
    """执行一次主动推理 tick。

    收集当前桌面状态，判断是否需要主动提供帮助。
    建议在对话轮次之间定期调用（如每 30-60 秒一次）。

    Args:
        user_message: 当前用户消息（如有）
        workspace: 当前工作目录
        memory_context: 相关记忆上下文（由 build_memory_context 获取）
        project_context: 项目上下文

    Returns:
        推理结果 JSON（action/confidence/suggestion_content 等）
    """
    try:
        import json

        engine = _get_engine()

        if workspace:
            engine.workspace = workspace
        if memory_context:
            engine.set_memory_context(memory_context)
        if project_context:
            engine.set_project_context(project_context)

        result = engine.tick(user_message=user_message)
        return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)

    except Exception as e:
        import json
        return json.dumps(
            {"action": "no_action", "reason": f"tick 失败: {e}"},
            ensure_ascii=False,
        )


@mcp.tool()
def proactive_capture(
    kind: str, content: str, evidence: str = "", confidence: float = 0.9
) -> str:
    """把用户说「记住这个」的原始内容写入主动产出台账（proactive_ledger）。

    手机端/桌面端用户明确要求记住一个灵感、话题或备忘时调用。写入后由理解管道统一
    加工（understanding 后处理），经 SYNC_TABLES 跨端同步，开聊时以对话式开场白递出
    （带 #id 句柄与证据）。与 memory_write 互补：记忆归记忆、台账归台账。

    Args:
        kind: 台账类型——灵感用 idea_capture，话题/备忘用 topic_note
        content: 用户原话/要记住的内容（尽量保留原文）
        evidence: 来源锚点（如「手机端 2026-10-06 15:30」）；缺省自动补当前时间
        confidence: 置信度（用户亲口要求=高置信，默认 0.9；写入门槛 0.75）

    Returns:
        成功返回台账 id（如 mobile_voice:idea_capture:xxxx）；被门槛拒绝/失败返回错误说明
    """
    try:
        import time as _t

        from .ledger import record_proactive

        if not (content or "").strip():
            return "失败：content 不能为空"
        # ⚠️ 2026-10-10 白名单：与 app.py 的 /v1/deskmate/proactive/capture 同一口径。
        # kind 来自调用方参数，原实现只 .strip() 就落库；实测可传矛盾类 kind
        # （plot_contradiction 等）绕过 R0 的「无锚点矛盾类拒收」把关。
        _k = (kind or "idea_capture").strip() or "idea_capture"
        if not is_mobile_kind_allowed(_k):
            return f"失败：kind 非法（{_k}），本入口仅接受 idea_capture / topic_note"
        if not (evidence or "").strip():
            evidence = f"用户亲口要求记住（{_t.strftime('%Y-%m-%d %H:%M', _t.localtime())}）"
        rid = record_proactive(
            source="mobile_voice",
            kind=(kind or "idea_capture").strip(),
            content=content.strip(),
            evidence=evidence,
            understanding=None,  # 留空 → 理解管道统一加工（§5.2）
            # source_node 不传：按本端 _sync_meta 自动标注（云端 server 调用=cloud，
            # Mac 调用=mac）。「来自手机」语义由 source=mobile_voice 承载；source_node
            # 按设计语义是「谁产出的」即哪个节点写的库，错标会污染 §六「哪端产出」展示。
            confidence=confidence,
        )
        if not rid:
            return "失败：未过写入门槛（需 confidence≥0.75 且 evidence 明确）"
        return f"已记入主动台账：{rid}（琢磨完开聊时我递给你）"
    except Exception as e:
        return f"记入台账失败：{e}"


@mcp.tool()
def proactive_lookup(handle: str = "", query: str = "") -> str:
    """回查主动产出台账（我之前递给你的卡片内容，§11.16 层二）。

    用户报「#a1b2c3」这种短号、或问起我之前提过的一件事时调用——
    我自己递出的卡片，接话前先用它把原文/依据/我的理解调出来，别凭印象瞎接。

    Args:
        handle: #id 短句柄（如 #a1b2c3）或完整 id 前缀（如 cloud_audit:drift:8aaf16）
        query: 关键词（按 content/evidence/understanding 子串语义检索）

    Returns:
        命中条目的紧凑清单（id/内容/依据/我的理解/状态）；无命中给明确答复
    """
    try:
        import json as _json

        from .ledger import lookup_items

        hits = lookup_items(_base_dir(), handle=handle, query=query, limit=5)
        if not hits:
            return (
                "台账里没有匹配的条目。可能是：短号记岔了（# 后 4 位以上再试）、"
                "或这件事我没递过/已被处理掉。可以直接问用户确认，别硬接。"
            )
        compact = [
            {
                "id": h.get("id"),
                "短号": "#" + (h.get("id") or "").rsplit(":", 1)[-1][:6],
                "内容": h.get("content"),
                "依据": h.get("evidence"),
                "我的理解": h.get("understanding"),
                "状态": h.get("status"),
            }
            for h in hits
        ]
        return _json.dumps(compact, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"台账回查失败：{e}"


@mcp.tool()
def proactive_feedback(suggestion_key: str, accepted: bool) -> str:
    """记录用户对主动建议的反馈。

    Args:
        suggestion_key: 建议的标识（建议内容前 100 字符）
        accepted: 用户是否接受了建议

    Returns:
        操作结果
    """
    try:
        _get_engine().record_feedback(suggestion_key, accepted)
        status = "接受" if accepted else "忽略"
        return f"已记录反馈：{status}"
    except Exception as e:
        return f"记录反馈失败：{e}"


@mcp.tool()
def proactive_silent(silent: bool) -> str:
    """设置主动推理的静默模式。

    Args:
        silent: True=静默（不主动打扰），False=正常模式

    Returns:
        操作结果
    """
    try:
        _get_engine().set_silent(silent)
        if silent:
            return "已进入静默模式，不会主动打扰"
        return "已退出静默模式"
    except Exception as e:
        return f"设置失败：{e}"


@mcp.tool()
def proactive_stats() -> str:
    """获取主动推理的防打扰统计信息。

    Returns:
        统计信息
    """
    try:
        import json

        stats = _get_engine().disturb.get_stats()
        return json.dumps(stats, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"获取统计失败：{e}"


@mcp.tool()
def proactive_next_steps(handle: str = "") -> str:
    """查一条台账项的预置下一步（阶段六任务直达）。

    Args:
        handle: 台账 #id 短句柄、完整 id 前缀，或书名/关键词。

    Returns:
        该事项的下一步列表（step_key/标题/任务指令），找不到时如实说明。
    """
    try:
        import json as _json

        from .next_steps import _find_item, next_steps_for_item

        item = _find_item(_base_dir(), handle)
        if item is None:
            return "台账里找不到该事项"
        return _json.dumps(next_steps_for_item(item), ensure_ascii=False, indent=2)
    except Exception as e:
        return f"查下一步失败：{e}"


@mcp.tool()
def proactive_start_task(handle: str, step: str = "") -> str:
    """开始一个台账下一步任务（阶段六任务直达）。

    把台账项置为 in_progress，并返回可直接执行的任务指令——主对话拿到指令
    就直接开工，不要再问用户是哪本书、从哪儿开始。找不到事项时如实说明，不要编任务。

    Args:
        handle: 台账 #id 短句柄、完整 id 前缀，或书名/关键词。
        step: 下一步的 step_key（如 check_characters / continue_outline / douyin_topic）；
            留空则默认第一条。
    """
    try:
        import json as _json

        from .next_steps import start_task

        res = start_task(_base_dir(), handle, step)
        if not res.get("ok"):
            return f"开始任务失败：{res.get('error') or '未知原因'}"
        return (
            f"已开始任务（台账项置为进行中）：{res['step']['title']}\n"
            f"任务指令：{res['instruction']}"
        )
    except Exception as e:
        return f"开始任务失败：{e}"


@mcp.tool()
def proactive_finish_task(handle: str, outcome: str = "done") -> str:
    """结束一个台账下一步任务（阶段六跟进状态）。

    Args:
        handle: 台账 #id 短句柄、完整 id 前缀，或书名/关键词。
        outcome: done=已完成 → actioned；dismiss=不要 → dismissed；later=稍后 → snoozed。
    """
    try:
        from .next_steps import finish_task

        res = finish_task(_base_dir(), handle, outcome)
        if not res.get("ok"):
            return f"结束任务失败：{res.get('error') or '未知原因'}"
        return f"任务已结束，新状态：{res.get('status')}"
    except Exception as e:
        return f"结束任务失败：{e}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
