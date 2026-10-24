"""小桔主动性改造阶段六单测：建议直达可执行任务 + 跟进状态流转。"""

from deskmate.proactivity.ledger import pending_items, record_proactive
from deskmate.proactivity.next_steps import (
    finish_task,
    next_steps_for_item,
    render_next_steps_section,
    start_task,
)


def _chapter_item():
    return {
        "id": "writing:chapter_updated:abc123",
        "kind": "chapter_updated",
        "content": "《国子监秘闻录》有新改动（第一章.md），要不要我核对人物 / 时间线？",
        "evidence": "文档: 第一章.md; 最后修改: 2026-10-24",
    }


def test_chapter_updated_steps():
    steps = next_steps_for_item(_chapter_item())
    assert [s["step_key"] for s in steps] == [
        "check_characters",
        "continue_outline",
        "douyin_topic",
    ]
    assert "国子监秘闻录" in steps[0]["instruction"]
    assert "novel_memory" in steps[0]["instruction"]
    text, numbered = render_next_steps_section([_chapter_item()])
    assert numbered[0]["step_no"] == "S1"
    assert "可以直接开工" in text


def test_stall_and_fallback_steps():
    stall = next_steps_for_item({
        "id": "x", "kind": "stall_flag",
        "content": "你《国子监秘闻录》已经 5 天没动笔了", "evidence": "文档: 第一章.md",
    })
    assert [s["step_key"] for s in stall] == [
        "continue_outline", "diagnose_block", "switch_book",
    ]
    fallback = next_steps_for_item({
        "id": "y", "kind": "whatever", "content": "某事", "evidence": "某依据",
    })
    assert len(fallback) == 2


def test_start_and_finish_flow(tmp_path):
    rid = record_proactive(
        base_dir=tmp_path, source="writing", kind="chapter_updated",
        content=_chapter_item()["content"], evidence=_chapter_item()["evidence"],
        confidence=0.8, anchor="writing:chapter:guozijian",
    )
    assert rid
    started = start_task(tmp_path, rid, "check_characters")
    assert started["ok"] and "novel_memory" in started["instruction"]
    items = {it["id"]: it for it in pending_items(base_dir=tmp_path)}
    assert rid not in items
    finished = finish_task(tmp_path, rid, "done")
    assert finished["ok"] and finished["status"] == "actioned"
    assert start_task(tmp_path, "不存在的事项", "check_characters")["ok"] is False
