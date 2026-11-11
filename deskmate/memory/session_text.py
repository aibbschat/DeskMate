"""会话文本的来源标记与「系统注入段」剥离。

## 为什么需要（2026-11-11）

自动化任务 / 目标制协议由系统拼进 user 消息：

- ``⏰ Scheduled run — <任务名>`` 开场
- ``--- 目标制任务（本次下达启用）---`` 协议块
- ``--- 你之前几轮的尝试（已经失败的做法不要再原样重复）---`` 重试块

它们不是萧盛口述，但 session 文件统一写成 ``## User``，记忆抽取会把
「不要再原样重复）---」这类半句抽成永久偏好（vault 11-04 / 11-06 / 11-11
三条残句就是这么来的）。仅在下游按 ``⏰ Scheduled run`` 跳过「萧盛影响」
不够——偏好抽取、纠正抽取、alter-ego 学习都还在读同一段文本。

这里把判定前移到**会话落盘边界**：系统轮次写一个机器可读标记，抽取入口
（本地规则与 LLM 共用）整段剥离。标记随 session 文件持久化，历史重抽同样
生效；没有标记的旧 session 按协议块指纹兜底。

为什么不误伤真人消息：只认系统独有的开场前缀（``⏰ Scheduled run``）和
由 ``build_goal_opening`` 生成的完整协议块；真人正常聊天不会以这些字符串
开头/包含整块协议。剥离只作用于命中的 ``## User`` 段，其余段落原样保留。
"""

from __future__ import annotations

import re

# 写在 ``## User`` 前一行的机器标记（HTML 注释，不影响人读与 Markdown 渲染）。
SYSTEM_TURN_MARKER = "<!-- deskmate:system-injected -->"

# 系统独有的 user 消息前缀。
SYSTEM_PROMPT_PREFIXES = ("⏰ Scheduled run",)

# 由系统注入的协议块（build_goal_opening / automation runner 生成）。
SYSTEM_PROMPT_BLOCKS = (
    "--- 目标制任务（本次下达启用）---",
    "--- 你之前几轮的尝试（已经失败的做法不要再原样重复）---",
)

_HEADER_RE = re.compile(r"(?m)^##\s+[^\n]*$")
_USER_HEADER_RE = re.compile(r"^##\s*User\b", re.IGNORECASE)


def is_system_injected_prompt(text: str | None) -> bool:
    """判断一条 user 消息是否由系统拼装（而非萧盛本人输入）。

    纯函数、零副作用。只认系统独有的前缀与协议块，避免把正文里转述的
    「⏰」或分隔线误判成注入。
    """
    if not text or not isinstance(text, str):
        return False
    head = text.lstrip()
    if head.startswith(SYSTEM_TURN_MARKER):
        return True
    if head.startswith(SYSTEM_PROMPT_PREFIXES):
        return True
    sample = head[:800]
    return any(block in sample for block in SYSTEM_PROMPT_BLOCKS)


def format_session_turn(
    user_msg: str, assistant_msg: str, *, system_injected: bool = False
) -> str:
    """生成 session 文件内容；系统轮次在 ``## User`` 前打标记。"""
    marker = f"{SYSTEM_TURN_MARKER}\n" if system_injected else ""
    return f"{marker}## User\n{user_msg}\n\n## Assistant\n{assistant_msg}\n"


def strip_system_injected_turns(session_text: str) -> str:
    """删除系统注入的 ``## User`` 段，保留其余内容。

    兼容三类输入：
    - 新格式：``SYSTEM_TURN_MARKER`` + ``## User``（标记随段落一起删除）；
    - 旧格式：没有标记，但 User 段以 ``⏰ Scheduled run`` 开头或含协议块；
    - 普通对话：原样返回。
    """
    if not session_text:
        return ""

    headers = list(_HEADER_RE.finditer(session_text))
    if not headers:
        return "" if is_system_injected_prompt(session_text) else session_text

    kept: list[str] = []
    pos = 0
    for i, match in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(session_text)
        header = match.group(0)
        body = session_text[match.end():end]
        # 标记可能落在本段 header 之前（含文件开头）。
        marked = SYSTEM_TURN_MARKER in session_text[pos:match.start()]
        if _USER_HEADER_RE.match(header.strip()) and (
            marked or is_system_injected_prompt(body)
        ):
            pos = end  # 连标记带整段一起丢弃
            continue
        kept.append(session_text[pos:end])
        pos = end
    kept.append(session_text[pos:])
    return "".join(kept).replace(SYSTEM_TURN_MARKER, "")
