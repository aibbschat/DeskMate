"""下一步任务直达 —— 让递送出来的建议直接变成可执行任务（2026-10-24 主动性改造阶段六）。

上一阶段的问题：建议能暂存、开聊时能递出来，但递出来还是一句问话，
主对话要再等一轮“你说做哪个、是哪本书、从哪儿开始”才能开工。
这一层解决的就是这一步：每条台账项自带 2–3 个预置下一步，
每个下一步都是一份可直接执行的任务指令（含事项原文、依据、要用的工具、
不做的事），萧盛回复一个编号或一句话，主对话调 proactive_start_task
拿到指令就能开工，不用他再交代一遍。

安全边界（与阶段一至五一致）：
- 这里的“草稿”是任务指令，不是正文：核对出清单、提纲出草稿、选题出候选，
  改稿正文与外发永远等萧盛点头。
- 不读手稿正文、不写稿件文件：指令里的书名与文件来自台账 evidence，
  真正核对时由主对话用 novel_memory 工具按章读。
- 本模块是纯函数 + 台账状态流转（acknowledge_ledger），不调 LLM、不联网。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_STEP_TEMPLATES: dict[str, list[tuple[str, str, str]]] = {
    "chapter_updated": [
        (
            "check_characters",
            "核对人物 / 时间线",
            "针对台账事项：{item}（依据：{evidence}）。先用 novel_memory 的增量更新把新章节并入人物圣经，"
            "再逐项列出人物关系与时间线冲突清单（每条带章节依据）；只出清单不改正文，改法等萧盛点头。",
        ),
        (
            "continue_outline",
            "续写下一章提纲",
            "针对台账事项：{item}（依据：{evidence}）。基于最新章节的收尾，写下一章的三段式提纲草稿"
            "（开局 / 转折 / 收尾），并标出未收的伏笔；只出提纲草稿，不写正文。",
        ),
        (
            "douyin_topic",
            "顺手排一个抖音选题",
            "针对台账事项：{item}（依据：{evidence}）。把新章节改成一个以古鉴今的抖音选题："
            "给 3 个 30 字内标题候选 + 一句话现实对应点 + 长文还是视频的建议；只出选题，不写全文、不发布。",
        ),
    ],
    "stall_flag": [
        (
            "continue_outline",
            "续写提纲：从断点接上",
            "针对台账事项：{item}（依据：{evidence}）。先用 novel_memory 回顾该书最近章节的收尾与未收伏笔，"
            "再写一份从断点接上的三段式提纲草稿；只出提纲，不写正文。",
        ),
        (
            "diagnose_block",
            "复盘卡点：列出卡在哪儿",
            "针对台账事项：{item}（依据：{evidence}）。列出该书当前最可能卡住的 3 个点"
            "（人物动机 / 情节转折 / 史料考据），每个点给一个可验证的下一步；只出复盘，不替萧盛做决定。",
        ),
        (
            "switch_book",
            "换一本：列出最近动笔的书",
            "针对台账事项：{item}（依据：{evidence}）。列出最近动笔的另外 2 本书及其最后动笔时间，"
            "问萧盛要不要先换一本写；不自动切换、不改任何文件。",
        ),
    ],
    "plot_contradiction": [
        (
            "check_characters",
            "按圣经逐项核对冲突",
            "针对台账事项：{item}（依据：{evidence}）。以人物圣经为准，逐项核对冲突点，"
            "每条给出章节依据与两种改法（改前文 / 改后文）；只出核对清单，不改正文。",
        ),
        (
            "continue_outline",
            "给一个能圆回来的提纲",
            "针对台账事项：{item}（依据：{evidence}）。写一份能把冲突圆回来的下一段提纲草稿，"
            "并标出需要补的伏笔；只出提纲，不写正文。",
        ),
    ],
    "character_inconsistency": [
        (
            "check_characters",
            "核对人物关系",
            "针对台账事项：{item}（依据：{evidence}）。以人物圣经为准，列出该人物在各章的阵营与关系变化，"
            "标出不一致处与章节依据；只出清单，不改正文。",
        ),
    ],
    "timeline_error": [
        (
            "check_characters",
            "核对时间线",
            "针对台账事项：{item}（依据：{evidence}）。按时间线文件逐项核对事件顺序与间隔，"
            "列出冲突清单与改法；只出清单，不改正文。",
        ),
    ],
    "missing_foreshadow": [
        (
            "continue_outline",
            "补一个收伏笔的提纲",
            "针对台账事项：{item}（依据：{evidence}）。写一份收这个伏笔的三段式提纲草稿，"
            "并标出伏笔最早出现的章节；只出提纲，不写正文。",
        ),
    ],
    "writing_review": [
        (
            "check_characters",
            "出审校清单",
            "针对台账事项：{item}（依据：{evidence}）。按人物圣经与时间线出一份审校清单"
            "（人物 / 时间 / 官职 / 史实四项，每项带章节依据）；只出清单，不改正文。",
        ),
        (
            "continue_outline",
            "续写提纲",
            "针对台账事项：{item}（依据：{evidence}）。基于审校结果写下一段的三段式提纲草稿；只出提纲，不写正文。",
        ),
    ],
}

_FALLBACK_STEPS: list[tuple[str, str, str]] = [
    (
        "make_checklist",
        "列出可执行清单",
        "针对台账事项：{item}（依据：{evidence}）。把这件事拆成 3–5 条可执行清单，"
        "每条写清产出物与完成标准；只出清单，不改文件、不外发。",
    ),
    (
        "draft_next",
        "起草下一步草稿",
        "针对台账事项：{item}（依据：{evidence}）。起草下一步的草稿（提纲或清单），"
        "并标出需要萧盛确认的点；只出草稿，等点头再动。",
    ),
]


def _book_from_item(item: dict) -> str:
    text = f"{item.get('content') or ''} {item.get('evidence') or ''}"
    m = re.search(r"《([^》]+)》", text)
    return m.group(1) if m else ""


def next_steps_for_item(item: dict) -> list[dict]:
    """给一条台账项生成预置下一步列表；每条含 step_key / title / instruction。"""
    try:
        kind = str(item.get("kind") or "")
        templates = _STEP_TEMPLATES.get(kind) or _FALLBACK_STEPS
        content = str(item.get("content") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        steps: list[dict] = []
        for key, title, template in templates:
            steps.append({
                "step_key": key,
                "title": title,
                "instruction": template.format(item=content, evidence=evidence),
                "item_id": item.get("id"),
                "item_kind": kind,
                "book": _book_from_item(item),
            })
        return steps
    except Exception:
        return []


def render_next_steps_section(chosen_items: list[dict], *, max_steps: int = 6) -> tuple[str, list[dict]]:
    """把 chosen 台账项的下一步拼成开场白附带段，返回 (文本, 带 S 编号的步骤列表)。

    S 编号只在本段内有效（S1、S2……），主对话拿到编号后仍需用 step_key + item_id
    去查台账原文（见 start_task），不要只凭编号猜。
    """
    steps: list[dict] = []
    try:
        for item in chosen_items or []:
            for step in next_steps_for_item(item):
                if len(steps) >= max_steps:
                    break
                numbered = dict(step)
                numbered["step_no"] = f"S{len(steps) + 1}"
                steps.append(numbered)
            if len(steps) >= max_steps:
                break
    except Exception:
        return "", []
    if not steps:
        return "", []
    lines = [
        "\n下一步我可以直接开工的事（不用你再交代一遍，回复 S 编号或直接说做哪条）："
    ]
    for s in steps:
        lines.append(f"{s['step_no']} · {s['title']} —— 针对：{(s.get('item_id') or '')}")
    lines.append("（开工后我会先出清单或草稿，改稿与外发仍等你点头。）")
    return "\n".join(lines), steps


def _find_item(base_dir, handle: str):
    from .ledger import lookup_items

    handle = (handle or "").strip()
    if not handle:
        return None
    hits = lookup_items(base_dir, handle=handle) if (":" in handle or len(handle) <= 12) else []
    if not hits:
        hits = lookup_items(base_dir, query=handle)
    if not hits:
        return None
    for it in hits:
        if it.get("status") in ("pending", "snoozed", "in_progress"):
            return it
    return hits[0]


def start_task(base_dir, handle: str, step_key: str) -> dict:
    """开始一个下一步任务：把台账项置为 in_progress，并返回可直接执行的任务指令。

    返回 {"ok": bool, "instruction": str, "item": dict | None, "error": str}。
    找不到事项或步骤时 ok=False，主对话应如实告诉用户，不要编一个任务。
    """
    try:
        from .ledger import acknowledge_ledger

        item = _find_item(base_dir, handle)
        if item is None:
            return {"ok": False, "instruction": "", "item": None, "error": "台账里找不到该事项"}
        steps = next_steps_for_item(item)
        step = None
        for s in steps:
            if s["step_key"] == step_key:
                step = s
                break
        if step is None:
            if not steps:
                return {"ok": False, "instruction": "", "item": item, "error": "该事项没有预置下一步"}
            step = steps[0]
        acknowledge_ledger(base_dir=base_dir, rid=item.get("id"), new_status="in_progress")
        return {
            "ok": True,
            "instruction": step["instruction"],
            "item": item,
            "step": step,
            "error": "",
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("开始下一步任务失败: %s", e, exc_info=True)
        return {"ok": False, "instruction": "", "item": None, "error": str(e)}


def finish_task(base_dir, handle: str, outcome: str = "done") -> dict:
    """结束一个下一步任务：done → actioned，dismiss → dismissed，later → snoozed。"""
    try:
        from .ledger import acknowledge_ledger

        item = _find_item(base_dir, handle)
        if item is None:
            return {"ok": False, "error": "台账里找不到该事项"}
        mapping = {"done": "actioned", "dismiss": "dismissed", "later": "snoozed"}
        status = mapping.get((outcome or "done").strip().lower(), "actioned")
        ok = acknowledge_ledger(base_dir=base_dir, rid=item.get("id"), new_status=status)
        return {"ok": bool(ok), "item": item, "status": status, "error": "" if ok else "状态流转被拒绝"}
    except Exception as e:  # noqa: BLE001
        logger.debug("结束下一步任务失败: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}
