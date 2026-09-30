"""「内部·萧盛」溯源标防污染回归测试（2026-09-30 修复配套）。

覆盖三件事，确保写入侧止血 + 存量清理 + 主导判定质量不再回退：
1. `_is_automation_noise`：自动化检索转储 / 定时任务提示词判为噪音，真·萧盛文本不误判。
2. `clean_xiaosheng_provenance`：对理解池摘/移冒牌「内部·萧盛」标，源文件只保守移除自动化提示词。
3. 主导判定（复刻 run_worldview_reflection 的 split）：冒牌萧盛不进 xs 桶、不撑大 dominant。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deskmate.agency.behaviors.worldview_reflection import (  # noqa: E402
    _is_automation_noise,
    _XS_PROVENANCE,
)
from scripts.clean_xiaosheng_provenance import (  # noqa: E402
    clean_xiaosheng_provenance,
    _is_unambiguous_automation_observation,
)


# ---------------------------------------------------------------------------
# 1. 噪音判定
# ---------------------------------------------------------------------------
def test_is_automation_noise_websearch_dump():
    assert _is_automation_noise("检索到以下资料：\n- 吞噬星空AI漫剧…") is True


def test_is_automation_noise_scheduled_run():
    assert _is_automation_noise("⏰ Scheduled run — AI圈·小桔每日自主巡检\nThis automation is due now") is True


def test_is_automation_noise_real_xiaosheng_not_flagged():
    assert _is_automation_noise("萧盛说写东西要慢一点。（内部·萧盛）") is False
    assert _is_automation_noise("小沃那条删不掉的评论出来那天，我正好又翻回萧盛审讯那章的草稿。") is False


def test_is_unambiguous_automation_observation():
    # 源文件保守判定：只认 unambiguously 自动化会话提示词，不按「检索到以下资料」删
    assert _is_unambiguous_automation_observation("⏰ Scheduled run — 写作营每日\nThis automation is due now") is True
    assert _is_unambiguous_automation_observation("检索到以下资料：\n- 萧盛让我查的明朝史料…") is False


# ---------------------------------------------------------------------------
# 2. 清理脚本（临时目录，dry-run + 真实）
# ---------------------------------------------------------------------------
def _write(path: str, lines: list[dict]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in lines:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_clean_removes_tagged_noise_from_understanding():
    d = tempfile.mkdtemp()
    up = os.path.join(d, "agency", "observation_understanding.jsonl")
    op = os.path.join(d, "agency", "xiaosheng_observations.jsonl")
    _write(up, [
        {"body": "检索到以下资料：\n- 麻豆头条…（内部·萧盛）", "ts": 1.0},   # 冒牌→移除
        {"body": "⏰ Scheduled run — 写作营（内部·萧盛）", "ts": 2.0},          # 冒牌→移除
        {"body": "萧盛说写东西要慢一点。（内部·萧盛）", "ts": 3.0},            # 真→保留
        {"body": "（外部·news）伊拉克撤军新闻", "ts": 4.0},                    # 外部→保留
    ])
    _write(op, [
        {"body": "⏰ Scheduled run — AI圈巡检", "ts": 1.0},   # 自动化提示词→移除
        {"body": "检索到以下资料：\n- 萧盛让我查的史料", "ts": 2.0},  # 保守：保留（可能是真·萧盛）
        {"body": "和萧盛聊了聊大纲", "ts": 3.0},               # 保留
    ])

    s = clean_xiaosheng_provenance(d, dry_run=False)
    assert s["understanding"]["tagged"] == 3
    assert s["understanding"]["removed_noise"] == 2
    assert s["understanding"]["kept"] == 2
    assert s["observations"]["removed_automation"] == 1
    assert s["observations"]["kept"] == 2

    kept = [json.loads(l) for l in open(up, encoding="utf-8") if l.strip()]
    assert all(_XS_PROVENANCE not in str(r.get("body", "")) or "萧盛说写东西要慢" in str(r.get("body", "")) for r in kept)
    assert sum(1 for r in kept if _XS_PROVENANCE in str(r.get("body", ""))) == 1  # 仅真·萧盛保留标


# ---------------------------------------------------------------------------
# 3. 主导判定（复刻 split 逻辑，不调 LLM）
# ---------------------------------------------------------------------------
def _split_dominant(bodies: list[str]):
    xs, ext, noise = [], [], 0
    for b in bodies:
        if _XS_PROVENANCE in b:
            _b = b.replace(_XS_PROVENANCE, "").strip()
            if _is_automation_noise(_b):
                noise += 1
                continue
            xs.append(_b)
        else:
            ext.append(b)
    return len(xs) >= len(ext), len(xs), len(ext), noise


def test_dominant_excludes_fake_xiaosheng():
    # 2 冒牌转储 + 1 真·萧盛 + 1 外部：旧逻辑 xs=3 误判 dominant；新逻辑剔除冒牌→xs=1
    batch = [
        "检索到以下资料：\n- 麻豆头条…（内部·萧盛）",
        "检索到以下资料：\n- 被舅舅操喷的侄媳…（内部·萧盛）",
        "萧盛说写东西要慢一点。（内部·萧盛）",
        "（外部·news）伊拉克撤军新闻",
    ]
    dom, nx, ne, nz = _split_dominant(batch)
    assert nz == 2
    assert nx == 1 and ne == 1
    assert dom is True  # 真·萧盛(1) >= 外部(1)，正确 dominant，而非被冒牌撑大


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
