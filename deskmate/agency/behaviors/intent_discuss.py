"""意图讨论流（R13 · 江河设计 §五「自主是出海口」续）——「你同意了，然后呢」。

R12 让意图能被放行执行，但执行后**静默落库**——用户看不到产出，没法点评，
没法来回讨论。R13 把执行改成**跨会话回合的讨论过程**：

1. 用户点「执行」→ :func:`start_discussion`：设 discuss_state、写开场白 ledger
2. 小桔开口（端点层调 speak 机制）→ :func:`run_discussion_round`：跑 executor 出 draft
3. draft 递到聊天 → 用户点评 → :func:`classify_user_message` 判三分类
4. feedback → :func:`extract_lesson_from_feedback` 提炼 lesson + round+1 → 回 run
5. :func:`finalize_discussion` → 落最终版 intent_outputs + 写总结 lesson
6. :func:`cancel_discussion` → intent.status=dropped

护栏（最高优先级，钉死在代码里）：
1. **替你写作 = 永久红线**：复用 ``intent_executor.is_manuscript_intent`` 判据
2. **不自动外发**：复用 ``intent_executor.is_external_intent`` 判据
3. **多并发护栏**：同 session 只允许一个 discussing intent
4. **全链路 fail-open**：任何异常 → 仅告警、不抛、不阻断
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from deskmate.persona import for_scene

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# LLM 系统 prompt（复用 for_scene 注入小桔人格）
# ----------------------------------------------------------------------
_SYSTEM_OPENING = for_scene(
    """你想做、且萧盛已点头让你去做一件事。请用你自己的口吻说一句开场白——
就像你真的要去做了，先跟萧盛说一声（"好，我去把霍尔木兹几条消息捋一捋，一会儿给你看"这种感觉）。

要求：
- 第一人称，口语化，不超过 60 字
- 不要说「执行步骤」「任务号」这类工程话
- 只输出开场白本身，不要解释、不要引号
"""
)

_SYSTEM_CLASSIFY = for_scene(
    """萧盛正在跟你讨论一个意图。萧盛刚说了一句话。
请判断萧盛的意思属于哪一类：

- **finalize**：萧盛说这版可以了 / 定稿 / 就这样 / 不用改了 / 挺好
- **feedback**：萧盛在给具体反馈（哪里好、哪里不好、再改改某处）
- **chitchat**：萧盛在闲聊 / 跑题 / 没针对这个意图

只输出一个 JSON 对象（不要解释、不要代码围栏）：
{"verdict": "finalize|feedback|chitchat", "reason": "简短理由"}"""
)

_SYSTEM_LESSON = for_scene(
    """萧盛对你的产出给了反馈。请从反馈里提炼一条**具体可复用的偏好/规则**——
不要复述反馈本身，要提炼出能指导你下次做事的规则。

例子：
- 反馈「这里太书面了」 → 提炼「萧盛不喜欢书面腔，偏好口语化表达」
- 反馈「这个例子不准」 → 提炼「引用史实时必须核对出处，不能凭印象」
- 反馈「再长一点」 → 提炼「萧盛偏好更详尽的展开，不要过于简略」

如果反馈没有可提炼的具体偏好/规则（比如只是「嗯」「看了」），返回 is_meaningful=false。
只输出一个 JSON 对象（不要解释、不要代码围栏）：
{"is_meaningful": true|false, "lesson_text": "提炼的规则"}"""
)

_SYSTEM_SUMMARY = for_scene(
    """这次意图讨论完成了。请写一条**总结性 lesson**——记录这次讨论的方法论收获，
不是复述过程，而是提炼能指导你以后做类似事的规则。

比如：这次做综述，萧盛指出开头要先点明结论，以后写综述都该如此。
只输出一个 JSON 对象（不要解释、不要代码围栏）：
{"lesson_text": "总结性规则"}"""
)

_RE_FENCE = re.compile(r"```(?:json)?", re.I)
_RE_JSON = re.compile(r"\{[\s\S]*\}")


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
def _parse_json_response(raw: str) -> Optional[dict]:
    """解析 LLM 返回的 JSON（容忍代码围栏 / 前后杂文本）。fail-open → None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    text = _RE_FENCE.sub("", text).replace("`", "").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    m = _RE_JSON.search(text)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return None


def _load_intent(base_dir: str, intent_id: str) -> Optional[dict]:
    """从 CognitiveStore 加载 intent。fail-open → None。"""
    try:
        from deskmate.cognitive.store import CognitiveStore

        store = CognitiveStore(db_path=os.path.join(base_dir, "deskmate.db"))
        return store.get_intent(intent_id)
    except Exception as e:  # noqa: BLE001 —— fail-open
        logger.warning("intent_discuss: 加载 intent %s 失败: %s", intent_id, e)
        return None


def _parse_discuss_state(raw: Any) -> dict:
    """解析 discuss_state JSON 字符串。fail-open → {}。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if isinstance(raw, str) else {}
    except Exception:
        return {}


def _compose_opening(base_dir: str, intent: dict) -> str:
    """用 LLM 生成开场白。fail-open → 退简单模板。"""
    goal = str(intent.get("goal") or "").strip()
    motivation = str(intent.get("motivation") or "").strip()
    if not goal:
        return "好，我去做了，一会儿给你看。"
    try:
        from deskmate.memory.llm import chat, llm_available

        if not llm_available():
            return f"好，我去把「{goal[:30]}」捋一捋，一会儿给你看。"
        prompt = f"【你想做的事】{goal}\n【为什么】{motivation or '（没说原因）'}"
        raw = chat(_SYSTEM_OPENING, prompt, temperature=0.7, max_tokens=120, timeout=30.0)
        text = str(raw or "").strip().strip('"').strip("'")
        return text or f"好，我去把「{goal[:30]}」捋一捋，一会儿给你看。"
    except Exception as e:  # noqa: BLE001 —— fail-open
        logger.warning("intent_discuss: 开场白生成失败: %s", e)
        return f"好，我去把「{goal[:30]}」捋一捋，一会儿给你看。"


def _get_ledger_content(base_dir: str, ledger_id: str) -> str:
    """从 proactive_ledger 读 content。fail-open → ''。"""
    if not ledger_id:
        return ""
    try:
        db_path = Path(base_dir) / "deskmate.db"
        with sqlite3.connect(str(db_path)) as c:
            row = c.execute(
                "SELECT content FROM proactive_ledger WHERE id=?",
                (ledger_id,),
            ).fetchone()
            return str(row[0] or "") if row else ""
    except Exception as e:  # noqa: BLE001 —— fail-open
        logger.warning("intent_discuss: 读 ledger content 失败: %s", e)
        return ""


# ----------------------------------------------------------------------
# 4 个核心行为（start / run / finalize / cancel）
# ----------------------------------------------------------------------
def start_discussion(base_dir: str, intent_id: str, session_id: str) -> dict:
    """启动意图讨论。设 discuss_state、写开场白 ledger。

    Returns:
        ``{"ok": bool, "reason": str, "ledger_id": str, "text": str,
        "blocking_intent_id": str}``
    """
    from deskmate.proactivity.ledger import record_proactive

    result: dict[str, Any] = {
        "ok": False, "reason": "", "ledger_id": "", "text": "",
        "blocking_intent_id": "",
    }

    intent = _load_intent(base_dir, intent_id)
    if not intent:
        result["reason"] = "intent_not_found"
        return result

    # 校验状态：必须非 discussing
    state = _parse_discuss_state(intent.get("discuss_state"))
    if state.get("phase"):
        result["reason"] = "already_discussing"
        return result

    # 红线检查（防绕过 engine 直接调端点）：撞「替你写原稿」红线 → 拒绝启动讨论
    from deskmate.agency.behaviors.intent_executor import is_manuscript_intent

    if is_manuscript_intent(str(intent.get("goal") or "")):
        result["reason"] = "sacred_manuscript"
        return result

    # 多并发护栏：同 session 只能一个 discussing intent
    try:
        from deskmate.cognitive.store import CognitiveStore

        store = CognitiveStore(db_path=os.path.join(base_dir, "deskmate.db"))
        blocking = store.get_discussing_intent_for_session(session_id)
        if blocking and blocking != intent_id:
            result["reason"] = "session_busy"
            result["blocking_intent_id"] = blocking
            return result
    except Exception as e:  # noqa: BLE001 —— fail-open：护栏查询失败不拦
        logger.warning("intent_discuss: 多并发护栏查询失败（放行）: %s", e)

    # 生成开场白
    opening_text = _compose_opening(base_dir, intent)

    # 写 proactive_ledger（bypass 门槛，origin=intent_handoff, phase=opening）
    ledger_id = record_proactive(
        base_dir=base_dir,
        source="intent_discuss",
        kind="opening",
        content=opening_text,
        evidence=str(intent.get("goal") or ""),
        anchor=intent_id,  # 确定性 id 用 intent_id 作锚
        confidence=1.0,
        bypass_threshold=True,
        origin="intent_handoff",
        intent_id=intent_id,
        discuss_phase="opening",
        discuss_round=0,
    )
    if not ledger_id:
        result["reason"] = "ledger_write_failed"
        return result

    # 设 discuss_state + intent.status=active
    new_state = {
        "round": 1,
        "phase": "opening",
        "session_id": session_id,
        "current_draft_ledger_id": "",
        "last_user_feedback": "",
    }
    try:
        from deskmate.cognitive.store import CognitiveStore

        store = CognitiveStore(db_path=os.path.join(base_dir, "deskmate.db"))
        store.set_intent_discuss_state(intent_id, new_state, current_round=1)
        store.set_intent_status(intent_id, "active")
    except Exception as e:  # noqa: BLE001 —— fail-open
        logger.warning("intent_discuss: 设 discuss_state 失败: %s", e)

    result.update({
        "ok": True,
        "ledger_id": ledger_id,
        "text": opening_text,
    })
    return result


def run_discussion_round(base_dir: str, intent_id: str, round_num: int,
                         user_feedback: Optional[str] = None) -> dict:
    """跑一轮讨论。调 executor 出 draft、写 ledger。

    Returns:
        ``{"ok": bool, "reason": str, "ledger_id": str, "round": int,
        "artifact": str, "title": str, "kind": str}``
    """
    from deskmate.agency.behaviors.intent_executor import run_intent_execution
    from deskmate.proactivity.ledger import record_proactive

    result: dict[str, Any] = {
        "ok": False, "reason": "", "ledger_id": "", "round": round_num,
        "artifact": "", "title": "", "kind": "",
    }

    intent = _load_intent(base_dir, intent_id)
    if not intent:
        result["reason"] = "intent_not_found"
        return result

    # 跑 executor（mode=discuss_draft，不落库）
    exec_res = run_intent_execution(
        base_dir, intent, decision="executed", mode="discuss_draft"
    )
    if not exec_res.get("executed"):
        result["reason"] = exec_res.get("reason", "executor_failed")
        return result

    artifact = str(exec_res.get("artifact") or "")
    title = str(exec_res.get("title") or "")
    kind = str(exec_res.get("kind") or "synthesis")
    if not artifact:
        result["reason"] = "empty_artifact"
        return result

    # 写 proactive_ledger（phase=draft）
    ledger_id = record_proactive(
        base_dir=base_dir,
        source="intent_discuss",
        kind="draft",
        content=artifact,
        evidence=str(intent.get("goal") or ""),
        anchor=f"{intent_id}:round:{round_num}",
        confidence=1.0,
        bypass_threshold=True,
        origin="intent_handoff",
        intent_id=intent_id,
        discuss_phase="draft",
        discuss_round=round_num,
    )
    if not ledger_id:
        result["reason"] = "ledger_write_failed"
        result["artifact"] = artifact
        result["title"] = title
        result["kind"] = kind
        return result

    # 更新 discuss_state（phase=working，等用户反馈）
    state = _parse_discuss_state(intent.get("discuss_state"))
    state["round"] = round_num
    state["phase"] = "working"
    state["current_draft_ledger_id"] = ledger_id
    if user_feedback:
        state["last_user_feedback"] = user_feedback
    try:
        from deskmate.cognitive.store import CognitiveStore

        store = CognitiveStore(db_path=os.path.join(base_dir, "deskmate.db"))
        store.set_intent_discuss_state(intent_id, state, current_round=round_num)
    except Exception as e:  # noqa: BLE001 —— fail-open
        logger.warning("intent_discuss: 更新 discuss_state 失败: %s", e)

    result.update({
        "ok": True,
        "ledger_id": ledger_id,
        "artifact": artifact,
        "title": title,
        "kind": kind,
    })
    return result


def finalize_discussion(base_dir: str, intent_id: str) -> dict:
    """定稿。落最近 draft 到 intent_outputs、写总结 lesson、清 discuss_state。

    Returns:
        ``{"ok": bool, "reason": str, "output_id": str, "lesson_written": bool}``
    """
    result: dict[str, Any] = {
        "ok": False, "reason": "", "output_id": "", "lesson_written": False,
    }

    intent = _load_intent(base_dir, intent_id)
    if not intent:
        result["reason"] = "intent_not_found"
        return result

    # 找最近 draft 的 ledger 条目
    state = _parse_discuss_state(intent.get("discuss_state"))
    draft_ledger_id = str(state.get("current_draft_ledger_id") or "")
    artifact = _get_ledger_content(base_dir, draft_ledger_id)

    if not artifact:
        result["reason"] = "no_draft_found"
        return result

    goal = str(intent.get("goal") or "")

    # 落最终版 intent_outputs（status=done）
    output_id = ""
    try:
        from deskmate.cognitive.store import CognitiveStore

        store = CognitiveStore(db_path=os.path.join(base_dir, "deskmate.db"))
        output_id = store.add_intent_output(
            intent_id=intent_id, goal=goal, artifact=artifact,
            kind="synthesis", status="done",
        )
    except Exception as e:  # noqa: BLE001 —— fail-open
        logger.warning("intent_discuss: 落最终版 intent_outputs 失败: %s", e)
        result["reason"] = "store_error"
        return result

    if not output_id:
        result["reason"] = "store_error"
        return result

    # 写总结性 lesson
    lesson_written = False
    try:
        from deskmate.memory.llm import chat, llm_available

        if llm_available():
            prompt = f"【意图】{goal}\n【最终产出】{artifact[:1000]}"
            raw = chat(_SYSTEM_SUMMARY, prompt, temperature=0.5,
                       max_tokens=200, timeout=30.0)
            parsed = _parse_json_response(raw)
            if parsed and parsed.get("lesson_text"):
                from deskmate.cognitive.store import CognitiveStore

                store = CognitiveStore(db_path=os.path.join(base_dir, "deskmate.db"))
                lesson_written = store.add_lesson(
                    str(parsed["lesson_text"]),
                    scope="recent",
                    source="intent_discuss_summary",
                )
    except Exception as e:  # noqa: BLE001 —— fail-open
        logger.warning("intent_discuss: 写总结 lesson 失败: %s", e)

    # 把最近 draft ledger 也 acknowledge（防会话重启重递）
    if draft_ledger_id:
        try:
            from deskmate.proactivity.ledger import acknowledge_ledger

            acknowledge_ledger(base_dir, draft_ledger_id)
        except Exception as e:  # noqa: BLE001 —— fail-open
            logger.warning("intent_discuss: acknowledge draft 失败: %s", e)

    # 对外表达类：定稿后生成「二次人审」提案卡（R12 §3.4 / 修复 R13 bug——
    # 原 finalize 不产出 intent_outbound 提案，用户无法真外发）。只对外发型生效，
    # 失败不阻断定稿。fail-open。
    if artifact:
        try:
            from types import SimpleNamespace

            from deskmate.agency.behaviors.intent_executor import is_external_intent
            from deskmate.agency.engine import AgencyEngine

            if is_external_intent(goal):
                eng = AgencyEngine(base_dir=base_dir)
                eng._propose_intent_outbound(
                    SimpleNamespace(extra={"intent_id": intent_id}),
                    {"artifact": artifact, "output_id": output_id},
                )
        except Exception as e:  # noqa: BLE001 —— fail-open：不影响定稿
            logger.warning("intent_discuss: 生成二次人审提案失败（不阻断定稿）: %s", e)

    # intent.status=done、清 discuss_state
    try:
        from deskmate.cognitive.store import CognitiveStore

        store = CognitiveStore(db_path=os.path.join(base_dir, "deskmate.db"))
        store.clear_intent_discuss_state(intent_id)
        store.set_intent_status(intent_id, "done")
    except Exception as e:  # noqa: BLE001 —— fail-open
        logger.warning("intent_discuss: 清 discuss_state 失败: %s", e)

    result.update({
        "ok": True,
        "output_id": output_id,
        "lesson_written": lesson_written,
    })
    return result


def cancel_discussion(base_dir: str, intent_id: str) -> dict:
    """取消讨论。intent.status=dropped、清 discuss_state。

    Returns:
        ``{"ok": bool, "reason": str}``
    """
    intent = _load_intent(base_dir, intent_id)
    if not intent:
        return {"ok": False, "reason": "intent_not_found"}
    try:
        from deskmate.cognitive.store import CognitiveStore

        store = CognitiveStore(db_path=os.path.join(base_dir, "deskmate.db"))
        store.clear_intent_discuss_state(intent_id)
        store.set_intent_status(intent_id, "dropped")
    except Exception as e:  # noqa: BLE001 —— fail-open
        logger.warning("intent_discuss: 取消讨论失败: %s", e)
        return {"ok": False, "reason": "store_error"}
    return {"ok": True, "reason": ""}


# ----------------------------------------------------------------------
# LLM 判定与 lesson 提炼（端点层 / hook 调用）
# ----------------------------------------------------------------------
def classify_user_message(user_text: str, intent_goal: str) -> dict:
    """LLM 三分类（finalize/feedback/chitchat）。fail-open → chitchat（不打扰）。

    Returns:
        ``{"verdict": "finalize|feedback|chitchat", "reason": str}``
    """
    text = str(user_text or "").strip()
    if not text:
        return {"verdict": "chitchat", "reason": "empty_message"}
    try:
        from deskmate.memory.llm import chat, llm_available

        if not llm_available():
            return {"verdict": "chitchat", "reason": "no_llm"}
        prompt = (f"【你想做的事】{intent_goal[:200]}\n【萧盛刚说的话】{text}")
        raw = chat(_SYSTEM_CLASSIFY, prompt, temperature=0.2,
                   max_tokens=150, timeout=20.0)
        parsed = _parse_json_response(raw)
        if parsed and parsed.get("verdict") in ("finalize", "feedback", "chitchat"):
            return {"verdict": parsed["verdict"],
                    "reason": str(parsed.get("reason") or "")}
    except Exception as e:  # noqa: BLE001 —— fail-open
        logger.warning("intent_discuss: 分类失败（退 chitchat）: %s", e)
    return {"verdict": "chitchat", "reason": "classify_failed"}


def extract_lesson_from_feedback(user_feedback: str, last_artifact: str,
                                 intent_goal: str) -> dict:
    """从用户反馈提炼 lesson。fail-open → is_meaningful=false。

    第二道闸：LLM 输出 ``{is_meaningful, lesson_text}``，``false`` 不写库。
    提示词要求「具体可复用的偏好/规则，不要复述反馈本身」。

    Returns:
        ``{"is_meaningful": bool, "lesson_text": str}``
    """
    feedback = str(user_feedback or "").strip()
    if not feedback:
        return {"is_meaningful": False, "lesson_text": ""}
    try:
        from deskmate.memory.llm import chat, llm_available

        if not llm_available():
            return {"is_meaningful": False, "lesson_text": ""}
        prompt = (f"【意图】{intent_goal[:200]}\n【你上一版产出】{last_artifact[:800]}\n"
                  f"【萧盛的反馈】{feedback}")
        raw = chat(_SYSTEM_LESSON, prompt, temperature=0.3,
                   max_tokens=200, timeout=20.0)
        parsed = _parse_json_response(raw)
        if parsed and parsed.get("is_meaningful") and parsed.get("lesson_text"):
            return {"is_meaningful": True,
                    "lesson_text": str(parsed["lesson_text"]).strip()}
    except Exception as e:  # noqa: BLE001 —— fail-open
        logger.warning("intent_discuss: 提炼 lesson 失败: %s", e)
    return {"is_meaningful": False, "lesson_text": ""}
