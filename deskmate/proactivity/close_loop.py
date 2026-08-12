"""主动闭环决策 — 对话/任务收尾后，LLM 反思是否有关联的未竟之事应闭环。

对应 Hermes 的「主动闭环」核心能力：用户刚做完一件事（如改了公众号内容），
agent 主动联想到关联的副本（网站）也要同步，并主动去完成。

本模块只做「决策」（识别关联未竟 + 评估可逆/风险），**不执行**——
执行由 ``executor.py`` 接管（高门槛时才复用 coworker 的 AUTO 任务通路）。
无 LLM Key 时静默返回 no-action，绝不抛异常。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass
class CloseLoopDecision:
    """一次主动闭环决策结果。"""

    should_close_loop: bool       # 是否应该主动闭环
    action_summary: str           # 要主动做的事（一句话，含目标）
    rationale: str                # 为什么认为需要闭环（关联点）
    confidence: float             # 0.0-1.0
    target_hint: str              # 目标线索（文件/URL/系统名），用于安全作用域判断
    reversible: bool              # 是否可逆（决定能否直接执行）
    risk_level: str               # low | medium | high


# 决策提示词：纪律非常严格——只在「高置信度 + 明确可逆 + 低风险」时才建议闭环。
_SYSTEM_PROMPT = """\
你是一个桌面 AI 助手的「主动闭环」判断模块。用户的上一轮动作刚完成，你要判断：
是否存在**关联的、未竟的**后续动作，应该被主动闭环（即用户大概率也希望顺手做完，但还没做）。

典型场景：
- 用户改了一处内容（如公众号文章），但存在关联的副本 / 镜像 / 同步源（如网站、另一个平台）还没同步。
- 用户完成一个产出（如生成文档），但关联的归档 / 通知 / 提交动作被遗漏。
- 用户改了设定 / 配置，关联的依赖项可能需要相应更新。

判断纪律（非常严格）：
- 只有在「高置信度」且「动作明确、可逆、低风险」时才 should_close_loop=true。
- 不要臆测不存在的关联；关联必须在本轮对话上下文或可验证的记忆 / 文件中明确存在。
- 动作必须具体可执行（一句话说清要做什么、改哪里），不要笼统。
- reversible=true 表示动作可撤销（如改写一个明确的文件、更新一条记录）；删除 / 发布 / 对外发送等不可逆或影响面广的为 false。
- risk_level：low=改单个明确文件或记录；medium=跨系统但可逆；high=删除 / 发布 / 对外发送等——high 绝不主动执行。

输出严格 JSON（不要包裹在 ``` 代码块里）：
{
  "should_close_loop": true,
  "action_summary": "要主动做的事（一句话，含目标）",
  "rationale": "为什么认为需要闭环（关联点）",
  "confidence": 0.92,
  "target_hint": "目标线索（文件 / URL / 系统名）",
  "reversible": true,
  "risk_level": "low"
}
"""


def _call_llm(prompt: str, max_tokens: int = 800) -> str:
    """调用 LLM（统一走 deskmate.memory.llm，与 judge / 记忆抽取共用凭证）。

    无 API Key 或异常时返回 no-action JSON，不抛异常。
    """
    try:
        from deskmate.memory.llm import chat, llm_available

        if not llm_available():
            return (
                '{"should_close_loop": false, "confidence": 0.0, '
                '"action_summary": "", "rationale": "未配置 LLM provider，跳过闭环决策", '
                '"target_hint": "", "reversible": false, "risk_level": "low"}'
            )
        return chat(
            system=_SYSTEM_PROMPT,
            user=prompt,
            temperature=0.2,
            max_tokens=max_tokens,
        )
    except Exception as e:  # 任何异常都降级为 no-action，不阻断主流程
        return (
            '{"should_close_loop": false, "confidence": 0.0, '
            f'"action_summary": "", "rationale": "LLM 调用失败: {e}", '
            '"target_hint": "", "reversible": false, "risk_level": "low"}'
        )


def _parse_decision(response: str) -> CloseLoopDecision:
    """解析 LLM 返回的 JSON 为 CloseLoopDecision（容错：提取平衡大括号包裹的对象）。"""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
    if m:
        json_str = m.group(1)
    else:
        depth = 0
        start_idx = -1
        json_str = None
        for i, ch in enumerate(response):
            if ch == "{":
                if depth == 0:
                    start_idx = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start_idx >= 0:
                    json_str = response[start_idx : i + 1]
                    break
        if json_str is None:
            return CloseLoopDecision(False, "", "无法解析 LLM 响应", 0.0, "", False, "low")

    try:
        data = json.loads(json_str)
        rl = str(data.get("risk_level", "low")).lower()
        if rl not in ("low", "medium", "high"):
            rl = "low"
        return CloseLoopDecision(
            should_close_loop=bool(data.get("should_close_loop", False)),
            action_summary=str(data.get("action_summary", "")).strip(),
            rationale=str(data.get("rationale", "")).strip(),
            confidence=float(data.get("confidence", 0.0)),
            target_hint=str(data.get("target_hint", "")).strip(),
            reversible=bool(data.get("reversible", False)),
            risk_level=rl,
        )
    except (json.JSONDecodeError, ValueError, TypeError):
        return CloseLoopDecision(False, "", "JSON 解析失败", 0.0, "", False, "low")


def decide_close_loop(
    user_msg: str,
    asst_msg: str,
    *,
    memory_context: str = "",
    workspace: str | None = None,
) -> CloseLoopDecision:
    """判断本轮对话 / 任务结束后，是否应该主动闭环一个关联的未竟之事。

    Args:
        user_msg: 本轮用户消息（做了什么请求）。
        asst_msg: 本轮助手回复（做了什么）。
        memory_context: 相关记忆上下文（可选，由调用方提供）。
        workspace: 当前工作目录（可选，用于上下文）。

    Returns:
        CloseLoopDecision
    """
    ctx_parts: list[str] = []
    ctx_parts.append("## 本轮发生了什么")
    ctx_parts.append(f"- 用户：{user_msg[:600]}")
    ctx_parts.append(f"- 助手：{asst_msg[:1200]}")

    if workspace:
        ctx_parts.append(f"\n## 工作目录\n{workspace}")
    if memory_context:
        ctx_parts.append(f"\n## 相关记忆\n{memory_context[:1500]}")

    ctx_parts.append(
        "\n请判断：是否存在关联的、未竟的后续动作应该主动闭环？"
        "只在高置信度、明确可逆、低风险时才建议。"
    )
    prompt = "\n".join(ctx_parts)

    return _parse_decision(_call_llm(prompt))
