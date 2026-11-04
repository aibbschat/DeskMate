"""出勤三档 —— 无人值守时「绝不挂起」的语义层（B 阶段）。

D3 定稿保持 ``Mode.BYPASS_APPROVALS``：权限层早返回 ``allowed=True,
needs_user=False``，运行期审批卡本就不产生。定时任务引擎也不传
``question_asker``（``ask_user`` 工具不注册）。唯一会挂的是 **唤醒／后台
续跑** 走会话引擎时，``manager.inbox_question_asker()`` 里的
``await inbox.wait(item.id)`` —— 没人来答就永远停在那一轮。

三档语义：
* ``attended``：有人在，正常弹卡／等回答（默认，完全保持原行为）；
* ``inbox``：问题进信箱，真等人（设计如此，不是 hang bug）；
* ``auto``：没人可答，用固定串按「最小破坏」代答并落审计，绝不挂起。

授权原则（萧哥 11-04）：这里**不是任务/站点闸门**，不拦任何动作，
只决定「没人时谁把这一轮接下去」。PoC 用环境变量开关，默认关闭；
C 阶段再做 per-session UI。
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

LEVELS = ("attended", "inbox", "auto")

#: PoC 级开关；默认 attended， unset/非法值一律按「有人在」处理（fail-safe）。
ATTENDANCE_ENV = "DESKMATE_ATTENDANCE"

#: 固定代答串。刻意不按问题内容生成——代答要可预测、可回放、零模型调用。
AUTO_ANSWER = (
    "现在没人能回答。请按最小破坏的方式继续完成原任务，"
    "需要我确认的部分写进结果里，不要停下来等待。"
)

_INBOX_TRUE = {"true", "on", "1", "yes", "inbox"}
_ATTENDED_TRUE = {"false", "off", "0", "no", "", "attended", "none"}


def normalize_attendance(value: Any) -> str:
    """把旧布尔 / 字符串 / 三档名归一成 ``LEVELS`` 之一。

    旧 ``unattended.json`` 的 ``true`` 表示「问题改投信箱」＝ ``inbox``；
    ``false`` / 未设置 ＝ ``attended``。未知值按 attended（有人在）处理，
    绝不因为一个脏配置就把会话变成无人代答。
    """
    if value is True:
        return "inbox"
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _INBOX_TRUE:
            return "inbox"
        if key == "auto":
            return "auto"
        if key in _ATTENDED_TRUE:
            return "attended"
    return "attended"


def current_attendance() -> str:
    """PoC 开关：读环境变量，默认 attended。调用时解析，禁模块级常量。"""
    return normalize_attendance(os.environ.get(ATTENDANCE_ENV))


def should_auto_answer(attendance: Any) -> bool:
    """只有 auto 档允许代答；attended / inbox 都必须真人参与。"""
    return normalize_attendance(attendance) == "auto"


def auto_answer(question: str = "") -> str:
    """固定代答，不随问题变化（避免每轮再花一次 LLM 且结果不可回放）。"""
    return AUTO_ANSWER


def build_auto_question_asker(
    audit: Optional[Callable[[dict[str, Any]], None]] = None,
) -> Callable[..., Any]:
    """返回一个 async asker：不进信箱、不 await 任何人，直接代答并留痕。

    签名与 ``manager.inbox_question_asker()`` 返回的 asker 一致
    ``(args: dict, tool_call_id) -> dict``，可直接替换接线。
    """
    async def ask(
        args: dict[str, Any], tool_call_id: Optional[str] = None
    ) -> dict[str, Any]:
        entry = {
            "kind": "question_auto_answered",
            "answer": AUTO_ANSWER,
            "question": str((args or {}).get("question", ""))[:200],
            "tool_call_id": tool_call_id or "",
        }
        if audit is not None:
            try:
                audit(entry)
            except Exception:
                # 审计失败不能把「不挂起」这件事拖垮
                pass
        return {"answer": AUTO_ANSWER}

    return ask
