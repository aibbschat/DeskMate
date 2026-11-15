"""端到端：world 块三段必须真的穿过上下文预算，抵达最终注入文本。

回归背景（2026-11-14 巡查）：``build_world_context`` 拼出 9432 字符（事实 4740 /
理解 778 / 立场 3914），``manager.py`` 给 world 块 ``cap=4500``，
``context_budget._cap_text`` 从头截 ⇒「她对世界的理解」「她自己想通透的立场」
连同防虚构的「萧盛真实作品事实」硬锚全部被静默切掉。

既有 ``test_world_context.py`` 只断言各段**单独生成**正确，``test_context_budget.py``
用的是人造等长块；全仓没有测试守「拼装 → 预算渲染」这一跳。本文件补的就是这一跳：
改配额、改顺序、改 cap 只要再把后两段挤掉，这里立刻红。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from deskmate.memory.context_budget import ContextBlock, render_blocks

# 与 manager.py:build_full_context_for_turn 里 world 块的真实参数保持一致
# （2026-11-15：闲聊轮改为 keep_light=True + cap_light 收窄，不再整块丢）
WORLD_CAP = 4500
WORLD_CAP_LIGHT = 1200


def _seed_long_world_facts(base_dir: Path, count: int = 30) -> None:
    """造一批长新闻剪报，模拟真实库里 4740+ 字符的事实段。"""
    from deskmate.memory.learned_store import LearnedStore

    store = LearnedStore(base_dir=base_dir)
    for i in range(count):
        store.append(
            [f"（外部·news）第{i}条世界观察：" + "财经与科技动向、" * 30 + f"编号{i}"],
            valid_until=(datetime.now() + timedelta(days=7)).isoformat(timespec="seconds"),
        )


def _seed_understanding(base_dir: Path) -> None:
    agency = base_dir / "agency"
    agency.mkdir(parents=True, exist_ok=True)
    (agency / "observation_understanding.jsonl").write_text(
        json.dumps(
            {"ts": time.time(), "body": "她对这条新闻的理解：结构比事件更值得看。"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _render_world(base_dir: Path) -> tuple[str, str]:
    from deskmate.memory.world_context import build_world_context

    raw = build_world_context(base_dir)
    block = ContextBlock("world", raw, priority=48, cap=WORLD_CAP,
                         cap_light=WORLD_CAP_LIGHT, keep_light=True)
    # 工作轮口径（命中 _WORK_HINTS 的「接着聊」）；闲聊轮 compact 口径另见 test_user_episodes.py
    rendered, _stats = render_blocks([block], user_message="接着聊一下这段情节")
    return raw, rendered


def _uncapped_fact_length(base_dir: Path) -> int:
    """未经配额裁剪的事实段长度——用它守住「这个用例确实复现了当年的超长场景」。"""
    from deskmate.memory.world_context import _build_fact_block

    return len(_build_fact_block(base_dir, 25))


def test_all_three_sections_survive_context_budget(tmp_path: Path):
    """事实段再长，她的理解 / 立场 / 作品硬锚也必须同时抵达注入文本。"""
    _seed_long_world_facts(tmp_path)
    _seed_understanding(tmp_path)
    _raw, rendered = _render_world(tmp_path)

    assert _uncapped_fact_length(tmp_path) > WORLD_CAP, "前提：未裁剪的事实段就超过 world 块上限"
    assert "她观察到的世界" in rendered
    assert "她对世界的理解" in rendered
    assert "结构比事件更值得看" in rendered
    assert "萧盛真实作品事实" in rendered, "防虚构闸门不能被预算裁掉"


def test_works_anchor_survives_without_world_facts(tmp_path: Path):
    """没有新闻 / 没有立场时，硬锚仍须在（既有测试只守了 raw，这里守渲染后）。"""
    _seed_understanding(tmp_path)
    _raw, rendered = _render_world(tmp_path)

    assert "萧盛真实作品事实" in rendered
    assert "她观察到的世界" not in rendered


def test_understanding_survives_even_with_max_facts(tmp_path: Path):
    """极端：事实段塞满，理解段依旧不能被挤掉。"""
    _seed_long_world_facts(tmp_path, count=60)
    _seed_understanding(tmp_path)
    _raw, rendered = _render_world(tmp_path)

    assert "她对世界的理解" in rendered
    assert "结构比事件更值得看" in rendered
    assert len(rendered) <= WORLD_CAP + 200, "修复不得靠放大 world 块总预算"
