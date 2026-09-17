"""可公开性闸门单测 —— 证明「生成前按来源/类型拦」真拦得住。

设计 §5.3 / §8.1 的牙齿：感知来源 + 私密词表命中一律不放行。
纯函数测试，不触 LLM / 网络。
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from deskmate.agency import visibility_gate as vg  # noqa: E402


class BlocklistTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parent / "tmp_vg"
        if self.base.exists():
            shutil.rmtree(self.base)
        (self.base / "agency").mkdir(parents=True)

    def tearDown(self):
        if self.base.exists():
            shutil.rmtree(self.base)

    def test_defaults_empty_after_relax(self):
        # 萧盛 2026-09-17 放开：内置默认词表清空，"书稿/未发表"不再默认拦
        kws = vg.load_blocklist(str(self.base), {})
        self.assertEqual(kws, [])

    def test_owner_file_is_the_family_line(self):
        (self.base / "agency" / "journal_blocklist.txt").write_text(
            "# 家人，一行一词\n尹遥\n尹逍\n", encoding="utf-8"
        )
        kws = vg.load_blocklist(str(self.base), {})
        self.assertEqual(sorted(kws), ["尹逍", "尹遥"])

    def test_file_and_config_merge(self):
        (self.base / "agency" / "journal_blocklist.txt").write_text(
            "# 一行一词\n尹遥\n尹逍\n", encoding="utf-8"
        )
        kws = vg.load_blocklist(str(self.base), {"block_keywords": ["某本书"]})
        self.assertIn("尹遥", kws)
        self.assertIn("某本书", kws)

    def test_config_keywords_survive_defaults_disabled(self):
        kws = vg.load_blocklist(str(self.base), {"block_defaults": False, "block_keywords": ["张三"]})
        self.assertIn("张三", kws)


class SafeClassifyTest(unittest.TestCase):
    KW = ["尹遥", "书稿"]

    def test_normal_record_safe(self):
        self.assertTrue(vg.is_public_safe({"body": "今天读到一条考古新闻，有意思。"}, self.KW))

    def test_perception_source_blocked(self):
        self.assertFalse(vg.is_public_safe({"body": "他复制了一段话", "source": "clipboard"}, self.KW))
        self.assertFalse(vg.is_public_safe({"body": "日程提醒", "kind": "calendar"}, self.KW))

    def test_blocklist_hit_blocked(self):
        self.assertFalse(vg.is_public_safe({"body": "下午和尹遥聊了很久"}, self.KW))
        self.assertFalse(vg.is_public_safe({"body": "在帮他改书稿第三章", "source": "self"}, self.KW))

    def test_blocklist_hit_on_title_blocked(self):
        self.assertFalse(vg.is_public_safe({"title": "关于书稿的第三章", "body": "随便写点"}, self.KW))

    def test_perception_block_can_be_disabled(self):
        self.assertTrue(
            vg.is_public_safe({"body": "剪贴板内容", "source": "clipboard"}, self.KW, block_perception=False)
        )

    def test_self_source_still_gated_by_keywords(self):
        # 回流自述也不豁免词表——万一她以前公开说过被后加入词表的内容
        self.assertFalse(vg.is_public_safe({"body": "尹遥的事", "source": "self"}, self.KW))


class HelperTest(unittest.TestCase):
    KW = ["书稿"]

    def test_filter_drops_private(self):
        recs = [{"body": "公开想法"}, {"body": "替他排书稿"}, {"body": "另一条公开"}]
        out = vg.filter_public_safe(recs, self.KW)
        self.assertEqual([r["body"] for r in out], ["公开想法", "另一条公开"])

    def test_scrub_worldview_drops_line_keeps_rest(self):
        text = "我信两个独立来源能对上才落笔。\n他在书稿里埋了伏笔。"
        scrubbed = vg.scrub_worldview(text, self.KW)
        self.assertIn("独立来源", scrubbed)
        self.assertNotIn("书稿", scrubbed)

    def test_bad_input_conservative(self):
        # 判断内部异常 → 保守拦（返回 False），不抛
        class Boom(dict):
            def get(self, *a, **k):
                raise RuntimeError("boom")

        self.assertFalse(vg.is_public_safe(Boom(), self.KW))


if __name__ == "__main__":
    unittest.main()
