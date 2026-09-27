"""存量归位脚本的回归测试（`scripts/migrate_memory_work_to_creation.py`）。

为什么给它写测试：它是**会改写真实 ~/.deskmate/MEMORY.md 的破坏性脚本**，
而它的判据（运营信号豁免）是本次修复里最容易漂移的一环。`plan()` / `build()`
是纯函数，可离线测——不必碰真实文件。

锁定三件事：
1. 书稿条目（含**没写书名**的纯散文）一定要迁走；
2. 运营流水账（敲钟/擂台/技能包/写作营文章…）一定要留下——否则等于把
   AI圈的流水账永久塞进常驻记忆；
3. `build()` 不得改动任何其它分节（逐字节）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "migrate_memory_work_to_creation.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("_mig_work_to_creation", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


mig = _load_script()


MEM = """§ 长期事实 #stable #sensitive
- 2026-09-07: 《国子监秘闻录》项目位于 /Users/xiaocheng/Documents/已出版/20.国子监秘闻录/

§ 工作 #work #sensitive
- 2026-09-25: 第五章《忘川帖》推进至第四节，武成川死于医官局
2026-09-25: 冲突收束：丁闻香打歪鼻梁后把他扔出窗外
- 2026-09-25: 【伏笔状态 09-25】：「真正的魔鬼」潜伏，赃款去向未揭
- 2026-09-26: 小桔敲钟人第三场开钟成功，辩题定为「AI 犯了错，账记在谁头上？」
- 2026-09-26: 小桔完成今日文章《你的反派只会放狠话？拆完这5个误区》

§ 偏好 #preference
- 2026-09-06: 用户为AI接入外部世界感知能力
"""


def test_plan_moves_manuscript_keeps_operations():
    move, keep = mig.plan(MEM)
    joined = "\n".join(move)
    # 书稿（有书名号 / 只有章节号 / 纯散文）全数迁走
    assert "忘川帖" in joined
    assert "丁闻香打歪鼻梁" in joined, "没写书名的纯散文书稿记忆也必须迁走"
    assert "真正的魔鬼" in joined
    # 运营流水账留下
    kept = "\n".join(keep)
    assert "敲钟人第三场" in kept
    assert "你的反派只会放狠话" in kept
    assert len(keep) == 2, kept


def test_build_moves_lines_and_leaves_other_sections_byte_identical():
    text, move, keep = mig.build(MEM)

    # 迁移条目在 §创作 下各出现一次；源分节不再包含
    _s, bs, be = mig._section_span(text, "§ 创作")
    dst_body = text[bs:be]
    for line in move:
        assert dst_body.count(line) == 1, line[:40]
    _s2, bs2, be2 = mig._section_span(text, "§ 工作")
    src_body = text[bs2:be2]
    for line in move:
        assert line not in src_body, line[:40]
    for line in keep:
        assert line.strip() in src_body

    # 其余分节逐字节未变
    def blocks(txt):
        import re

        return {b.split("\n", 1)[0]: b for b in re.split(r"(?m)^(?=§\s)", txt)}

    before, after = blocks(MEM), blocks(text)
    for head, blk in before.items():
        if head.startswith("§ 工作") or head.startswith("§ 创作"):
            continue
        assert after[head] == blk, f"{head} 被改动了"


def test_build_is_idempotent_on_second_run():
    """跑完一遍后 §工作 只剩运营条目 → 再跑没有可迁内容（不重复搬）。"""
    text, _move, _keep = mig.build(MEM)
    move2, keep2 = mig.plan(text)
    assert move2 == [], f"二次运行仍有可迁条目：{move2[:1]}"
    assert len(keep2) == 2


def test_build_appends_to_existing_creation_section():
    """已存在 §创作 时追加，不新建第二个同名分节。"""
    existing = MEM.replace(
        "§ 工作 #work #sensitive",
        "§ 创作 #stable\n- 2026-09-20: 早先的书稿记忆\n\n§ 工作 #work #sensitive",
    )
    text, move, _keep = mig.build(existing)
    import re

    heads = [b.split("\n", 1)[0] for b in re.split(r"(?m)^(?=§\s)", text)]
    assert heads.count("§ 创作 #stable") == 1, heads
    assert "早先的书稿记忆" in text
    for line in move:
        assert line in text


@pytest.mark.parametrize("bad, expect", [
    ("2026-09-25: 第五章《忘川帖》推进至第四节", True),
    ("2026-09-25: 丁闻香在越民授权下狱中见鲁其春", True),
    ("2026-09-26: 小桔完成今日文章《你的反派只会放狠话？》", False),
    ("2026-09-24: #work 敲钟人踩坑：bell_submit 占用广场发帖额度", False),
    ("2026-09-24: 擂台 #2 辩题「唱反调是『另一面』还是『反张嘴』？」", False),
    ("2026-09-25: 萧盛的知识库线上版本升级至0.22.3，新增目录树", False),
])
def test_operation_signal_boundary(bad, expect):
    """豁免判据的边界：分清「书稿散文」与「运营流水账」。"""
    from deskmate.memory.manager import _OPERATION_SIGNAL_RE

    assert bool(_OPERATION_SIGNAL_RE.search(bad)) is (not expect)
