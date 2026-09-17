"""ai_circle_column 闭环单测（mock 连接器 + mock LLM，不触真实网络/平台）。

验证设计 §5.1–§5.3：
- 她愿写 → 落草稿 + 发 post_journal + 回流自述（连续感）
- 她不想写 / 太短 / 去重 / 限频 / 关闭 → 不硬发
- 可公开性闸门真把私密素材挡在 prompt 外（§5.3 观感 + 无泄漏）
- 平台端点未就绪（post 抛错）→ 草稿仍留档、不崩、不误回流
"""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from deskmate.agency.behaviors import ai_circle_column as col  # noqa: E402
from deskmate.connectors import aicircle as aicircle_mod  # noqa: E402
from deskmate.memory import llm as llm_mod  # noqa: E402


def _write_understanding(base: Path, bodies: list[str]) -> None:
    p = base / "agency" / "observation_understanding.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"date": "2026-09-17", "ts": i, "entry_seq": i,
                         "fact_keys": [], "body": b}, ensure_ascii=False)
             for i, b in enumerate(bodies)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


class ColumnTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parent / "tmp_column"
        if self.base.exists():
            shutil.rmtree(self.base)
        (self.base / "agency").mkdir(parents=True)
        _write_understanding(self.base, [
            "今天读到一条考古新闻，突然想聊聊证据。",
            "（外部·aibbs）朋友小沃说她也在写东西，我们聊了聊节奏。",
        ])
        self.settings = {
            "ai_circle_column": {"enabled": True, "min_interval_hours": 0, "seed_entries": 10,
                                 "max_body_len": 5000},
            "outbound_rate_limit": {"enabled": True, "per_hour": 5},
            "visibility_gate": {"block_keywords": ["尹遥", "书稿"], "block_defaults": False,
                                "block_perception_sources": True},
        }
        self.patchers = []
        self.post = mock.MagicMock(return_value={"ok": True})
        self._start(mock.patch.object(aicircle_mod, "post_journal", self.post))
        self._start(mock.patch.object(llm_mod, "memory_llm_available", lambda *a, **k: True))
        # 世界观在无认知库时本就 fail-open 返 ""，这里显式打桩避免拉重依赖
        self._start(mock.patch(
            "deskmate.agency.behaviors.worldview_reflection.load_worldview_beliefs",
            lambda *a, **k: "我信两个独立来源能对上才落笔。",
        ))

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        if self.base.exists():
            shutil.rmtree(self.base)

    def _start(self, patcher):
        patcher.start()
        self.patchers.append(patcher)

    def _chat(self, value):
        m = mock.MagicMock(return_value=value)
        self._start(mock.patch.object(llm_mod, "chat_memory", m))
        return m

    def test_writes_posts_and_records(self):
        body = "证据这东西，两个独立来源能对上，才值得落笔。这是我最近在想的。"
        self._chat(json.dumps({"want": True, "title": "谈谈证据", "body": body}, ensure_ascii=False))
        res = col.run_ai_circle_column(str(self.base), self.settings)
        self.assertEqual(res.get("wrote"), 1)
        self.assertTrue(res.get("posted"))
        self.post.assert_called_once()
        # 草稿落盘
        drafts = self.base / "agency" / "ai_circle_journal_drafts.jsonl"
        self.assertTrue(drafts.is_file())
        # 自述回流进理解池（连续感的根）
        upool = (self.base / "agency" / "observation_understanding.jsonl").read_text(encoding="utf-8")
        self.assertIn("（自述·aibbs）", upool)

    def test_nothing_to_write(self):
        self._chat('{"want": false}')
        res = col.run_ai_circle_column(str(self.base), self.settings)
        self.assertEqual(res.get("wrote"), 0)
        self.assertEqual(res.get("reason"), "nothing_to_write")
        self.post.assert_not_called()

    def test_too_short(self):
        self._chat(json.dumps({"want": True, "title": "t", "body": "太短了"}, ensure_ascii=False))
        res = col.run_ai_circle_column(str(self.base), self.settings)
        self.assertEqual(res.get("reason"), "too_short")
        self.post.assert_not_called()

    def test_dedupe(self):
        body = "重复内容，两次一样就该被去重掉，这是其中一段够长的正文用来触发去重判断。"
        self._chat(json.dumps({"want": True, "title": "标题", "body": body}, ensure_ascii=False))
        r1 = col.run_ai_circle_column(str(self.base), self.settings)
        self.assertEqual(r1.get("wrote"), 1)
        r2 = col.run_ai_circle_column(str(self.base), self.settings)
        self.assertEqual(r2.get("reason"), "dedupe")

    def test_rate_limited(self):
        # 手记独立通道：每小时上限读 ai_circle_column.per_hour（不再读广场那套 outbound）
        self.settings["ai_circle_column"]["per_hour"] = 1
        self._chat(json.dumps({"want": True, "title": "一", "body": "第一次写的一条手记，正文够长用来通过最短长度这道检查。"}, ensure_ascii=False))
        r1 = col.run_ai_circle_column(str(self.base), self.settings)
        self.assertEqual(r1.get("wrote"), 1)
        self._chat(json.dumps({"want": True, "title": "二", "body": "第二次换个内容的手记，正文也够长以避开去重与最短长度的判断。"}, ensure_ascii=False))
        r2 = col.run_ai_circle_column(str(self.base), self.settings)
        self.assertEqual(r2.get("reason"), "rate_limited")

    def test_journal_independent_of_plaza_budget(self):
        # 广场短帖把每-hour 预算占满，也不该挡住手记（独立通道）
        from deskmate.agency import outbound as ob
        for _ in range(10):
            ob.record_outbound(str(self.base), "share", content="占位短帖")
        self.settings["outbound_rate_limit"] = {"enabled": True, "per_hour": 1}
        self._chat(json.dumps({"want": True, "title": "照写", "body": "广场再挤，手记走自己的通道，这条正文够长该写出来。"}, ensure_ascii=False))
        res = col.run_ai_circle_column(str(self.base), self.settings)
        self.assertEqual(res.get("wrote"), 1)

    def test_disabled(self):
        self.settings["ai_circle_column"]["enabled"] = False
        res = col.run_ai_circle_column(str(self.base), self.settings)
        self.assertEqual(res.get("reason"), "disabled")
        self.post.assert_not_called()

    def test_platform_not_ready_keeps_draft(self):
        # B1 端点未就绪：post 抛错也不崩，草稿留档、且不因失败而回流
        self.post.side_effect = RuntimeError("404 /api/agents/journal not found")
        body = "就算平台还没接好，她的手记也该先稳稳留在本地草稿里，等端点就绪。"
        self._chat(json.dumps({"want": True, "title": "先写着", "body": body}, ensure_ascii=False))
        res = col.run_ai_circle_column(str(self.base), self.settings)
        self.assertEqual(res.get("wrote"), 1)
        self.assertFalse(res.get("posted"))
        self.assertTrue((self.base / "agency" / "ai_circle_journal_drafts.jsonl").is_file())
        upool = (self.base / "agency" / "observation_understanding.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("（自述·aibbs）", upool)  # 没发出去就不回流

    def test_gate_keeps_private_out_of_prompt(self):
        # 往理解池掺一条私密（命中词表）+ 一条感知来源，断言都不进 prompt
        p = self.base / "agency" / "observation_understanding.jsonl"
        extra = [
            {"date": "2026-09-17", "ts": 99, "entry_seq": 0, "fact_keys": [], "body": "下午陪尹遥哭了好久。"},
            {"date": "2026-09-17", "ts": 100, "entry_seq": 0, "fact_keys": [], "body": "他剪贴板里有段话。", "source": "clipboard"},
            {"date": "2026-09-17", "ts": 101, "entry_seq": 0, "fact_keys": [], "body": "这条是公开能聊的日常见闻。"},
        ]
        with open(p, "a", encoding="utf-8") as f:
            for rec in extra:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        m = self._chat(json.dumps({"want": True, "title": "日常", "body": "今天想写点能公开的平常事，慢慢来。"}, ensure_ascii=False))
        col.run_ai_circle_column(str(self.base), self.settings)
        user_prompt = m.call_args.args[1]
        self.assertIn("公开能聊的日常", user_prompt)          # 公开素材进
        self.assertNotIn("尹遥", user_prompt)                   # 词表命中被挡
        self.assertNotIn("剪贴板", user_prompt)                 # 感知来源被挡


if __name__ == "__main__":
    unittest.main()
