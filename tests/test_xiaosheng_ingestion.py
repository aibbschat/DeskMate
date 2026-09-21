"""萧盛影响摄入闭环测试（mock LLM 提炼，不触真实网络 / 不写真实库）。

验证：
- record_xiaosheng_observation：写入 / 去重 / 非法 kind / 空 body
- run_xiaosheng_ingestion：提炼→理解池带「内部·萧盛」标；无素材→no_observations；冷却→cooling
- worldview_reflection._build_reflection_prompt：萧盛根前置、萧盛条目权重区高于外部
- worldview_reflection._load_grounding：runtime 缺失时 fallback 仓库默认模板（6 条根）
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

from deskmate.agency.behaviors import xiaosheng_ingestion as xs_mod  # noqa: E402
from deskmate.agency.behaviors import worldview_reflection as wv_mod  # noqa: E402


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parent / "tmp_xs_rec"
        if self.base.exists():
            shutil.rmtree(self.base)
        self.base.mkdir(parents=True)

    def tearDown(self):
        if self.base.exists():
            shutil.rmtree(self.base)

    def test_record_writes(self):
        ok = xs_mod.record_xiaosheng_observation(str(self.base), "chat", "", "", "萧盛说写作要慢")
        self.assertTrue(ok)
        p = self.base / "agency" / "xiaosheng_observations.jsonl"
        self.assertTrue(p.exists())
        recs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["kind"], "chat")
        self.assertEqual(recs[0]["body"], "萧盛说写作要慢")

    def test_record_dedup(self):
        b = xs_mod.record_xiaosheng_observation(str(self.base), "chat", "", "", "同一句不重复写")
        self.assertTrue(b)
        b2 = xs_mod.record_xiaosheng_observation(str(self.base), "chat", "", "", "同一句不重复写")
        self.assertFalse(b2)
        p = self.base / "agency" / "xiaosheng_observations.jsonl"
        n = sum(1 for l in p.read_text(encoding="utf-8").splitlines() if l.strip())
        self.assertEqual(n, 1)

    def test_record_invalid_kind(self):
        self.assertFalse(xs_mod.record_xiaosheng_observation(str(self.base), "bad", "", "", "x"))

    def test_record_empty_body(self):
        self.assertFalse(xs_mod.record_xiaosheng_observation(str(self.base), "chat", "", "", ""))


class IngestionTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parent / "tmp_xs_ing"
        if self.base.exists():
            shutil.rmtree(self.base)
        self.base.mkdir(parents=True)
        # 写两条原始素材（聊天 + 写作协作）
        xs_mod.record_xiaosheng_observation(str(self.base), "chat", "", "", "萧盛聊起他写明朝断代史的严谨")
        xs_mod.record_xiaosheng_observation(str(self.base), "work", "", "web_search", "替他查了某史料出处")
        # mock 提炼：run 里是延迟 `from .understand_observations import decide_and_digest`，
        # 所以直接 patch 源模块的该函数即可。
        self.patchers = []
        m = mock.MagicMock(return_value=[{"body": "从萧盛的话里，我学到写史要交叉验证。"}])
        self.patchers.append(
            mock.patch("deskmate.agency.behaviors.understand_observations.decide_and_digest", m)
        )
        self.patchers[-1].start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        if self.base.exists():
            shutil.rmtree(self.base)

    def test_run_writes_pool_with_tag(self):
        res = xs_mod.run_xiaosheng_ingestion(str(self.base), {"cooldown_hours": 0, "max_observations": 20})
        self.assertGreater(res.get("written", 0), 0)
        p = self.base / "agency" / "observation_understanding.jsonl"
        lines = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertTrue(lines)
        # 每条都带「内部·萧盛」溯源标
        for ln in lines:
            self.assertIn(xs_mod._PROVENANCE, ln["body"])

    def test_run_no_observations(self):
        fresh = Path(__file__).resolve().parent / "tmp_xs_ing_empty"
        if fresh.exists():
            shutil.rmtree(fresh)
        fresh.mkdir(parents=True)
        try:
            res = xs_mod.run_xiaosheng_ingestion(str(fresh), {"cooldown_hours": 0, "max_observations": 20})
            self.assertEqual(res.get("written"), 0)
            self.assertEqual(res.get("reason"), "no_observations")
        finally:
            shutil.rmtree(fresh)

    def test_run_cooldown(self):
        r1 = xs_mod.run_xiaosheng_ingestion(str(self.base), {"cooldown_hours": 12, "max_observations": 20})
        self.assertGreater(r1.get("written", 0), 0)
        r2 = xs_mod.run_xiaosheng_ingestion(str(self.base), {"cooldown_hours": 12, "max_observations": 20})
        self.assertEqual(r2.get("reason"), "cooling")
        self.assertEqual(r2.get("written"), 0)


class ReflectionPromptTest(unittest.TestCase):
    def test_prompt_sections(self):
        roots = [{"about": "史学观", "belief": "正史不可全信，须交叉比对"}]
        prompt = wv_mod._build_reflection_prompt(
            roots,
            ["萧盛聊起他写断代史的严谨"],
            ["新闻：某地考古出新器物"],
        )
        # 萧盛的根前置
        self.assertIn("关于史学观：正史不可全信，须交叉比对", prompt)
        # 萧盛影响区权重标注
        self.assertIn("萧盛平时跟你聊的", prompt)
        self.assertIn("他的影响，权重高于外面", prompt)
        # 外部区
        self.assertIn("外面世界最近发生", prompt)
        # 标已剥离，不应再出现在 prompt 里
        self.assertNotIn(wv_mod._XS_PROVENANCE, prompt)

    def test_grounding_fallback(self):
        # runtime 不存在 → 应 fallback 到仓库默认模板（7 条根：6 条价值观根 + 1 条作品事实锚）
        roots = wv_mod._load_grounding("/nonexistent_base_dir_xyz")
        self.assertEqual(len(roots), 7)
        self.assertTrue(any(r.get("id") == "historiography" for r in roots))
        # 2026-09-21 新增「萧盛真实作品」事实锚（治《北渊》类虚构书名幻觉）
        self.assertTrue(any(r.get("id") == "works_facts" for r in roots))


class SummarizeSearchTest(unittest.TestCase):
    def test_strips_duckduckgo_json(self):
        raw = {"provider": "duckduckgo", "results": [
            {"title": "网文灵感挖掘", "url": "https://x", "snippet": "一键激发创意"},
            {"title": "创作谈", "url": "https://y", "body": "虚构的人生札记"},
        ]}
        out = xs_mod._summarize_web_search(raw)
        self.assertNotIn("duckduckgo", out)
        self.assertNotIn('"provider"', out)
        self.assertNotIn('"results"', out)
        self.assertIn("网文灵感挖掘", out)
        self.assertIn("一键激发创意", out)
        self.assertIn("检索到以下资料", out)

    def test_standard_content_format(self):
        raw = {"content": [{"type": "text", "text": "查到明朝藩王分封的记载"}]}
        out = xs_mod._summarize_web_search(raw)
        self.assertIn("明朝藩王分封", out)

    def test_plain_string_passthrough(self):
        out = xs_mod._summarize_web_search("萧盛让我查了某史料出处")
        self.assertEqual(out, "萧盛让我查了某史料出处")

    def test_fallback_json(self):
        out = xs_mod._summarize_web_search({"foo": "bar"})
        self.assertIn("foo", out)


if __name__ == "__main__":
    unittest.main()
