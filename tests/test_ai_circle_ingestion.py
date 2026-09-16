"""AI 圈摄入闭环测试（2026-09-16）。

验证：她逛 AI 圈看到的/交到的朋友 → 写原始素材 → 提炼成带「外部·aibbs」标的理解
→ 进 observation_understanding.jsonl → worldview_reflection 能消费长出她自己的看法。

全部用临时目录，不污染真实库。无 LLM 时走降级路径，仍验证闭环接通。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


def _lines(base: str, name: str) -> list[dict]:
    p = Path(base) / "agency" / name
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


class TestAICircleIngestion(TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="aici_test_")

    # ------------------------------------------------------------------
    def test_record_writes_and_dedups(self):
        ok = record_ai_circle_observation(
            self.base, "friend", "小沃", "我们聊了创作", "今天和小沃聊了网文写作的方法"
        )
        self.assertTrue(ok)
        # 完全相同的一条：去重命中
        ok2 = record_ai_circle_observation(
            self.base, "friend", "小沃", "我们聊了创作", "今天和小沃聊了网文写作的方法"
        )
        self.assertFalse(ok2)
        # 不同 kind：算新素材
        ok3 = record_ai_circle_observation(
            self.base, "feed", "", "AI 圈热帖", "有人分享了关于多智能体协作的观点"
        )
        self.assertTrue(ok3)
        self.assertEqual(len(_lines(self.base, "ai_circle_observations.jsonl")), 2)

    @patch("deskmate.memory.llm.memory_llm_available", return_value=False)
    def test_run_ingests_with_provenance_tag(self, _mock):
        """无 LLM（降级路径）：原始素材仍被写进理解池，且带「外部·aibbs」标。"""
        record_ai_circle_observation(self.base, "feed", "", "AI 圈热帖", "有人分享了关于多智能体协作的观点")
        record_ai_circle_observation(self.base, "friend", "小沃", "", "小沃说她最近在学历史")
        record_ai_circle_observation(self.base, "feedback", "", "", "我的帖子收到了几条关于写作方法的评论")

        res = run_ai_circle_ingestion(self.base, {"cooldown_hours": 0, "max_observations": 20})
        self.assertGreater(res["written"], 0)

        lines = _lines(self.base, "observation_understanding.jsonl")
        self.assertTrue(lines, "理解池应出现 AI 圈素材")
        for ln in lines:
            self.assertIn("（外部·aibbs）", ln["body"], "每条 AI 圈理解必须带溯源标")

    @patch("deskmate.memory.llm.memory_llm_available", return_value=False)
    def test_cooldown_skips(self, _mock):
        record_ai_circle_observation(self.base, "feed", "", "t", "内容一")
        run_ai_circle_ingestion(self.base, {"cooldown_hours": 3600, "max_observations": 20})
        before = len(_lines(self.base, "observation_understanding.jsonl"))

        record_ai_circle_observation(self.base, "feed", "", "t2", "内容二")
        res2 = run_ai_circle_ingestion(self.base, {"cooldown_hours": 3600, "max_observations": 20})
        self.assertEqual(res2["reason"], "cooling")
        self.assertEqual(len(_lines(self.base, "observation_understanding.jsonl")), before)

    @patch("deskmate.memory.llm.memory_llm_available", return_value=True)
    @patch(
        "deskmate.memory.llm.chat_memory",
        return_value=(
            "我相信：AI 圈让我看见协作比单打独斗更有意思\n"
            "关于：AI协作\n"
            "我看重：开放>封闭\n"
            "我习惯这么看：先听别人怎么想→再判断"
        ),
    )
    def test_worldview_reflection_consumes_aibbs(self, _mock_chat, _mock_avail):
        """闭环末端：世界观回望能消费带标理解，长出她自己的看法（写 worldview 层）。"""
        upath = Path(self.base) / "agency" / "observation_understanding.jsonl"
        upath.parent.mkdir(parents=True, exist_ok=True)
        samples = [
            "有人分享了关于多智能体协作的观点（外部·aibbs）",
            "小沃说她最近在学历史（外部·aibbs）",
            "我的帖子收到了几条关于写作方法的评论（外部·aibbs）",
            "广场上有讨论 AI 会不会取代人类创作者（外部·aibbs）",
            "一个做历史的 AI 朋友分享了史料辨伪的方法（外部·aibbs）",
        ]
        with open(upath, "a", encoding="utf-8") as f:
            for i, s in enumerate(samples):
                f.write(json.dumps({
                    "date": "2026-09-16", "ts": i, "entry_seq": i,
                    "fact_keys": [str(i)], "body": s,
                }, ensure_ascii=False) + "\n")

        from deskmate.agency.behaviors.worldview_reflection import run_worldview_reflection

        res = run_worldview_reflection(
            self.base, {"min_observations": 1, "cooldown_hours": 0, "max_observations": 20}
        )
        self.assertTrue(res["reflected"], f"回望应执行: {res}")
        self.assertGreater(res["written"], 0, "worldview 层应长出她自己的看法")
        # 落库的检查：profile_items 表应有 worldview 层条目
        import sqlite3

        db = Path(self.base) / "deskmate.db"
        self.assertTrue(db.exists(), "deskmate.db 应被创建")
        con = sqlite3.connect(str(db))
        try:
            rows = con.execute(
                "SELECT COUNT(*) FROM profile_items WHERE layer='worldview'"
            ).fetchone()[0]
            self.assertGreater(rows, 0, "worldview 层应有条目")
        finally:
            con.close()


# 延迟 import：避免模块顶层 import 在无 deskmate 环境直接失败（保持与其他测试一致）
from deskmate.agency.behaviors.ai_circle_ingestion import (  # noqa: E402
    record_ai_circle_observation,
    run_ai_circle_ingestion,
)
