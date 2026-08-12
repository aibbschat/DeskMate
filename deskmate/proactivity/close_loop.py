"""主动闭环决策 - 对话/任务收尾后，LLM 调查真实环境后判断是否有关联未竟应闭环。

**v2 改进（对齐 Hermes 主动行为）**：

1. **调查→决策两阶段**：不再只看对话文本猜，先让 LLM 用只读工具（read_file /
   search_files / git_status / read_memory）调查真实环境，再基于调查结果做决策。
2. **接记忆**：memory_context 参与决策（用户偏好/纠正历史影响判断）。
3. **多步推理**：调查循环最多 _MAX_ROUNDS 轮，LLM 先调查再决定，不是 one-shot。
4. **工具驱动**：决策从"反思对话"变成"调查世界"--能在文件里发现对话没提到的问题。

安全模型不变：只做「决策」（识别关联未竟 + 评估可逆/风险），**不执行**--
执行由 ``executor.py`` 接管。无 LLM Key 时静默返回 no-action，绝不抛异常。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .investigator import (
    TOOL_DEFINITIONS,
    TOOL_IMPLEMENTATIONS,
    _MAX_INVESTIGATION_ROUNDS,
)


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
    investigation_notes: str      # 调查发现摘要（透明可审计）


# -----------------------------------------------------------------------
# 阶段 1：调查（LLM + 工具调用循环）
# -----------------------------------------------------------------------

_INVESTIGATE_SYSTEM_PROMPT = """\
你是一个桌面 AI 助手的「主动闭环调查员」。用户的上一轮动作刚完成，你的任务是：
**调查真实环境**，找出是否存在关联的、未竟的后续动作。

你可以使用以下只读工具进行调查：
- read_file(path): 读取文件内容，确认实际状态
- list_dir(path): 列出目录，了解项目结构
- search_files(pattern, path): 搜索文件内容，发现关联文件或不一致
- git_status(): 查看 git 工作区状态，发现未提交/未推送的改动
- read_memory(query): 读取用户偏好/历史纠正，判断用户是否在意这件事

调查纪律：
- 最多调用 {max_rounds} 轮工具，每轮可以调一个工具。
- 你调查的目标是「验证是否存在关联未竟」，不是泛泛浏览。
- 如果对话内容已经足够明确（比如用户说"改了 A 但 B 还没同步"），可以少调查或跳过。
- 如果需要确认文件内容、配置状态、代码一致性，就主动用工具去看。

每轮回复格式（严格 JSON）：
{{"tool": "工具名", "args": {{"参数名": "值"}}, "thought": "为什么要调查这个"}}

如果你已经有足够信息做出判断（或无需调查），回复：
{{"tool": "done", "args": {{}}, "thought": "调查完成/无需调查，可以进入决策"}}
""".format(max_rounds=_MAX_INVESTIGATION_ROUNDS)


def _call_llm_plain(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 800,
) -> str:
    """调用 LLM（纯文本，无工具），返回文本。异常时返回空串。"""
    try:
        from deskmate.memory.llm import chat

        return chat(
            system=system,
            user=user,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception:
        return ""


def _run_investigation(
    user_msg: str,
    asst_msg: str,
    workspace: str | None,
    memory_context: str,
    base_dir: str | None,
) -> tuple[str, list[str]]:
    """执行调查循环：LLM 用工具调查真实环境，返回 (调查摘要, 工具调用日志)。

    每轮 LLM 可以选择调用一个工具或宣布 done。最多 _MAX_INVESTIGATION_ROUNDS 轮。
    """
    from deskmate.memory.llm import llm_available

    if not llm_available():
        return "（LLM 不可用，跳过调查）", []

    ctx_parts = [
        "## 本轮发生了什么",
        f"- 用户：{user_msg[:600]}",
        f"- 助手：{asst_msg[:1200]}",
    ]
    if workspace:
        ctx_parts.append(f"## 工作目录\n{workspace}")
    if memory_context:
        ctx_parts.append(f"## 相关记忆\n{memory_context[:1500]}")
    ctx_parts.append("\n请调查是否存在关联的、未竟的后续动作。")
    initial_prompt = "\n".join(ctx_parts)

    investigation_log: list[str] = []
    conversation: list[dict] = [
        {"role": "system", "content": _INVESTIGATE_SYSTEM_PROMPT},
        {"role": "user", "content": initial_prompt},
    ]

    for round_idx in range(_MAX_INVESTIGATION_ROUNDS):
        # 调用 LLM 决定本轮调查什么
        try:
            from deskmate.memory.llm import _api_key, _base_url, _model
            from openai import OpenAI

            client = OpenAI(api_key=_api_key(), base_url=_base_url(), timeout=60.0)
            resp = client.chat.completions.create(
                model=_model(),
                temperature=0.2,
                max_tokens=600,
                messages=conversation,
                tools=TOOL_DEFINITIONS,
                tool_choice="auto",
            )
            msg = resp.choices[0].message
        except Exception:
            break

        # 检查是否有 tool_calls
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.type == "function":
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    tool_calls.append((tc.function.name, args, tc.id))

        if not tool_calls:
            # LLM 没调用工具，可能直接给了文本回复 -> 调查结束
            if msg.content:
                investigation_log.append(f"LLM: {msg.content[:300]}")
            break

        # 执行工具调用
        # 在 conversation 中加入 assistant 的 tool_calls 消息
        conversation.append(msg.model_dump())

        for tool_name, tool_args, tool_call_id in tool_calls:
            impl = TOOL_IMPLEMENTATIONS.get(tool_name)
            if impl is None:
                result = f"未知工具：{tool_name}"
            else:
                # 注入 workspace 和 base_dir
                kwargs = dict(tool_args)
                if tool_name in ("read_file", "list_dir", "search_files"):
                    kwargs["workspace"] = workspace
                elif tool_name == "git_status":
                    kwargs["workspace"] = workspace
                elif tool_name == "read_memory":
                    kwargs["base_dir"] = base_dir
                    kwargs["workspace"] = workspace
                try:
                    result = str(impl(**kwargs))
                except Exception as e:
                    result = f"工具执行错误：{e}"

            investigation_log.append(f"[{tool_name}({tool_args})] -> {result[:400]}")

            # 把工具结果加入对话
            conversation.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result[:2000],
            })

    return _summarize_investigation(investigation_log), investigation_log


def _summarize_investigation(log: list[str]) -> str:
    """把调查日志压缩为一段摘要供决策阶段使用。"""
    if not log:
        return "（未执行调查）"
    return "\n".join(log[-8:])  # 取最近 8 条


# -----------------------------------------------------------------------
# 阶段 2：决策（基于调查结果做判断）
# -----------------------------------------------------------------------

_DECIDE_SYSTEM_PROMPT = """\
你是一个桌面 AI 助手的「主动闭环判断模块」。用户刚做完一件事，调查员已经调查了
真实环境。基于调查结果，你要判断：是否存在**关联的、未竟的**后续动作，应该被
主动闭环（即用户大概率也希望顺手做完，但还没做）。

判断纪律（非常严格）：
- 只有在「高置信度」且「动作明确、可逆、低风险」时才 should_close_loop=true。
- 不要臆测不存在的关联；关联必须在调查结果中明确存在。
- 动作必须具体可执行（一句话说清要做什么、改哪里），不要笼统。
- reversible=true 表示动作可撤销（如改写一个明确的文件、更新一条记录）；\
删除 / 发布 / 对外发送等不可逆或影响面广的为 false。
- risk_level：low=改单个明确文件或记录；medium=跨系统但可逆；\
high=删除 / 发布 / 对外发送等--high 绝不主动执行。

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


def _parse_decision(response: str, investigation_notes: str) -> CloseLoopDecision:
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
            return CloseLoopDecision(
                False, "", "无法解析 LLM 响应", 0.0, "", False, "low", investigation_notes
            )

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
            investigation_notes=investigation_notes,
        )
    except (json.JSONDecodeError, ValueError, TypeError):
        return CloseLoopDecision(
            False, "", "JSON 解析失败", 0.0, "", False, "low", investigation_notes
        )


def decide_close_loop(
    user_msg: str,
    asst_msg: str,
    *,
    memory_context: str = "",
    workspace: str | None = None,
    base_dir: str | None = None,
) -> CloseLoopDecision:
    """判断本轮对话 / 任务结束后，是否应该主动闭环一个关联的未竟之事。

    **v2 改进**：调查→决策两阶段。
    1. 调查阶段：LLM 用只读工具调查真实环境（最多 _MAX_ROUNDS 轮）。
    2. 决策阶段：基于调查结果做判断，不再只看对话文本。

    Args:
        user_msg: 本轮用户消息（做了什么请求）。
        asst_msg: 本轮助手回复（做了什么）。
        memory_context: 相关记忆上下文（用户偏好/纠正历史）。
        workspace: 当前工作目录（用于工具路径作用域）。
        base_dir: 记忆根目录（用于 read_memory 工具）。

    Returns:
        CloseLoopDecision（含 investigation_notes 透明可审计）
    """
    from deskmate.memory.llm import llm_available

    # 阶段 1：调查
    investigation_notes, raw_log = _run_investigation(
        user_msg, asst_msg, workspace, memory_context, base_dir
    )

    # 阶段 2：决策（基于调查结果）
    if not llm_available():
        return CloseLoopDecision(
            False, "", "未配置 LLM provider，跳过闭环决策", 0.0, "", False, "low",
            investigation_notes,
        )

    ctx_parts = [
        "## 本轮发生了什么",
        f"- 用户：{user_msg[:600]}",
        f"- 助手：{asst_msg[:1200]}",
    ]
    if workspace:
        ctx_parts.append(f"## 工作目录\n{workspace}")
    if memory_context:
        ctx_parts.append(f"## 相关记忆\n{memory_context[:1500]}")
    ctx_parts.append(f"## 调查发现\n{investigation_notes}")
    ctx_parts.append(
        "\n请判断：是否存在关联的、未竟的后续动作应该主动闭环？"
        "只在高置信度、明确可逆、低风险时才建议。"
    )
    prompt = "\n".join(ctx_parts)

    response = _call_llm_plain(
        system=_DECIDE_SYSTEM_PROMPT,
        user=prompt,
        temperature=0.2,
        max_tokens=800,
    )

    return _parse_decision(response, investigation_notes)
