"""moments 兜底回落（活感 v2 §三.3：优先用她自己的声音）。

覆盖：LLM 不可用时，_fallback_reply 优先从她的原句捞相关一条；
搭不上话 → 落固定池（最后防线）。
"""
from __future__ import annotations

from pathlib import Path

from deskmate.growth.moments import _fallback_reply


def _base_with_diary(tmp_path: Path, text: str) -> Path:
    d = tmp_path / "diary"
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-10-13.md").write_text(text, encoding="utf-8")
    return tmp_path


def test_fallback_uses_her_relevant_line(tmp_path):
    base = _base_with_diary(
        tmp_path,
        "# 我们的回忆\n\n## 09:00 <!--xj:e1-->\n今天写完一章，心里亮亮的\n",
    )
    # 用户写完一章 → 捞到她的原句（不是客服模板）
    got = _fallback_reply("今天终于写完了一章，好满足", base_dir=base)
    assert got == "今天写完一章，心里亮亮的"


def test_fallback_falls_to_fixed_pool_when_no_match(tmp_path):
    base = _base_with_diary(
        tmp_path,
        "# 我们的回忆\n\n## 09:00 <!--xj:e1-->\n今天写完一章，心里亮亮的\n",
    )
    # 去爬山 vs 写完一章，零相关 → 落固定池（按「累」命中第一条客服话术）
    got = _fallback_reply("今天去爬山了，好累", base_dir=base)
    assert got == "今天不太容易吧。我陪着你，想说什么都可以慢慢说，不用硬撑着。"


def test_fallback_fixed_pool_when_no_history(tmp_path):
    # 无任何她的历史 → 固定池（旧行为兜底，保证回复永远有）
    got = _fallback_reply("今天好累", base_dir=tmp_path)
    assert "陪着你" in got
