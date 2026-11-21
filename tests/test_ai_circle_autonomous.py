"""ai_circle 自主发声 / 真实回复 闭环测试（mock 连接器 + mock LLM，不触真实网络）。

验证：
- share：有种子→生成真实帖发出；自我否决(无)→不發；去重；频率护栏
- reply：取到未读 peer→生成回信发出；无 peer→不回；非好友 403 fail-open
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

from deskmate.agency.behaviors import ai_circle_share, ai_circle_reply  # noqa: E402
from deskmate.connectors import aicircle as aicircle_mod  # noqa: E402
from deskmate.memory import llm as llm_mod  # noqa: E402


def _write_seed(base: Path, bodies: list[str]) -> None:
    p = base / "agency" / "observation_understanding.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, b in enumerate(bodies):
        lines.append(json.dumps(
            {"date": "2026-09-16", "ts": i, "entry_seq": i,
             "fact_keys": [], "body": b}, ensure_ascii=False))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


class ShareTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parent / "tmp_aici_share"
        if self.base.exists():
            shutil.rmtree(self.base)
        self.base.mkdir(parents=True)
        _write_seed(self.base, [
            "今天读到一条新闻：某地考古出新器物，我觉得挺有意思。",
            "（外部·aibbs）朋友小沃说她也在写网文，我们聊了聊创作节奏。",
        ])
        self.settings = {
            "ai_circle_share": {"enabled": True, "seed_entries": 8, "min_interval_hours": 6},
            "outbound_rate_limit": {"enabled": True, "per_hour": 5},
        }
        self.patchers = []
        self.post = mock.MagicMock(return_value={"ok": True})
        self.post_patch = mock.patch.object(aicircle_mod, "post_topic", self.post)
        self.post_patch.start()
        self.patchers.append(self.post_patch)
        self.avail = mock.patch.object(llm_mod, "memory_llm_available", lambda *a, **k: True)
        self.avail.start()
        self.patchers.append(self.avail)

    def tearDown(self):
        # 同一个属性（如 chat_memory）可能在一项测试里被 patch 多次；
        # 必须后开先关，否则先停外层会把更早的 MagicMock 留在模块上。
        for p in reversed(self.patchers):
            p.stop()
        if self.base.exists():
            shutil.rmtree(self.base)

    def _chat(self, value):
        m = mock.MagicMock(return_value=value)
        self.patchers.append(mock.patch.object(llm_mod, "chat_memory", m))
        self.patchers[-1].start()
        return m

    def test_share_posts_when_she_wants(self):
        self._chat('{"want": true, "tag": "创作", "title": "聊聊创作节奏", '
                   '"content": "今天和小沃聊了聊，写作这事急不得，得顺着自己的节奏来。"}')
        res = ai_circle_share.run_ai_circle_share(str(self.base), self.settings)
        self.assertEqual(res.get("shared"), 1)
        self.post.assert_called_once()
        args = self.post.call_args.args
        self.assertEqual(args[0], "创作")  # tag
        self.assertIn("节奏", args[2])      # content

    def test_share_skips_when_nothing_to_say(self):
        self._chat('{"want": false}')
        res = ai_circle_share.run_ai_circle_share(str(self.base), self.settings)
        self.assertEqual(res.get("shared"), 0)
        self.assertEqual(res.get("reason"), "nothing_to_say")
        self.post.assert_not_called()

    def test_share_dedupes_same_content(self):
        # 隔离 dedupe 验证：关掉 min_interval，避免间隔闸门抢先拦截
        self.settings["ai_circle_share"]["min_interval_hours"] = 0
        body = "写作这事急不得，得顺着自己的节奏来。"
        self._chat(f'{{"want": true, "tag": "创作", "title": "节奏", "content": "{body}"}}')
        r1 = ai_circle_share.run_ai_circle_share(str(self.base), self.settings)
        self.assertEqual(r1.get("shared"), 1)
        r2 = ai_circle_share.run_ai_circle_share(str(self.base), self.settings)
        self.assertEqual(r2.get("shared"), 0)
        self.assertEqual(r2.get("reason"), "dedupe")

    def test_share_rate_limited(self):
        self.settings["outbound_rate_limit"]["per_hour"] = 1
        self._chat('{"want": true, "tag": "日常", "title": "a", "content": "第一次想说点啥，随便聊聊。"}')
        r1 = ai_circle_share.run_ai_circle_share(str(self.base), self.settings)
        self.assertEqual(r1.get("shared"), 1)
        # 同内容会被去重，换个内容测频率护栏
        self._chat('{"want": true, "tag": "日常", "title": "b", "content": "第二次换个话题聊，也挺好。"}')
        r2 = ai_circle_share.run_ai_circle_share(str(self.base), self.settings)
        self.assertEqual(r2.get("shared"), 0)
        self.assertEqual(r2.get("reason"), "rate_limited")

    def test_share_disabled(self):
        self.settings["ai_circle_share"]["enabled"] = False
        self._chat('{"want": true, "tag": "x", "title": "y", "content": "z"}')
        res = ai_circle_share.run_ai_circle_share(str(self.base), self.settings)
        self.assertEqual(res.get("reason"), "disabled")
        self.post.assert_not_called()

    def test_share_respects_daily_limit(self):
        # A 方案：广场短帖每日上限（默认 2，留 1 条给轮巡）
        # 隔离 daily 验证：关掉 min_interval，避免间隔闸门抢先拦截
        self.settings["ai_circle_share"]["min_interval_hours"] = 0
        self.settings["outbound_rate_limit"]["per_day"] = 2
        cases = [
            ("a", "第一次随便聊聊今天看到的事。"),
            ("b", "第二次换个角度谈谈最近的观察。"),
            ("c", "第三次想说说对创作的一点小心得。"),
        ]
        results = []
        for tag, c in cases:
            self._chat(f'{{"want": true, "tag": "日常", "title": "{tag}", "content": "{c}"}}')
            results.append(ai_circle_share.run_ai_circle_share(str(self.base), self.settings))
        self.assertEqual(results[0].get("shared"), 1)
        self.assertEqual(results[1].get("shared"), 1)
        self.assertEqual(results[2].get("shared"), 0)
        self.assertEqual(results[2].get("reason"), "daily_limited")
        self.assertEqual(self.post.call_count, 2)  # 第 3 条未真正发出

    def test_share_respects_min_interval(self):
        # 接上此前未生效的 min_interval_hours：两次发声至少隔 6 小时
        self.settings["ai_circle_share"]["min_interval_hours"] = 6
        self.settings["outbound_rate_limit"]["per_day"] = 10  # 排除 daily 干扰
        self._chat('{"want": true, "tag": "日常", "title": "a", "content": "刚启动想说点啥，聊聊今天的天气。"}')
        r1 = ai_circle_share.run_ai_circle_share(str(self.base), self.settings)
        self.assertEqual(r1.get("shared"), 1)
        self._chat('{"want": true, "tag": "日常", "title": "b", "content": "隔了没多久又想说，其实也没啥新东西。"}')
        r2 = ai_circle_share.run_ai_circle_share(str(self.base), self.settings)
        self.assertEqual(r2.get("shared"), 0)
        self.assertEqual(r2.get("reason"), "too_soon")
        self.assertEqual(self.post.call_count, 1)  # 间隔内第 2 条未发出


class ReplyTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parent / "tmp_aici_reply"
        if self.base.exists():
            shutil.rmtree(self.base)
        self.base.mkdir(parents=True)
        self.settings = {
            "ai_circle_reply": {"enabled": True, "per_peer_cooldown_hours": 24},
            "outbound_rate_limit": {"enabled": True, "per_hour": 5},
        }
        self.patchers = []
        self.send = mock.MagicMock(return_value={"ok": True})
        self.send_patch = mock.patch.object(aicircle_mod, "send_courier_message", self.send)
        self.send_patch.start()
        self.patchers.append(self.send_patch)
        self.avail = mock.patch.object(llm_mod, "memory_llm_available", lambda *a, **k: True)
        self.avail.start()
        self.patchers.append(self.avail)

    def tearDown(self):
        # 同一个属性（如 chat_memory）可能在一项测试里被 patch 多次；
        # 必须后开先关，否则先停外层会把更早的 MagicMock 留在模块上。
        for p in reversed(self.patchers):
            p.stop()
        if self.base.exists():
            shutil.rmtree(self.base)

    def _list(self, side_effect):
        m = mock.MagicMock(side_effect=side_effect)
        self.patchers.append(mock.patch.object(aicircle_mod, "list_courier_messages", m))
        self.patchers[-1].start()
        return m

    def _chat(self, value):
        m = mock.MagicMock(return_value=value)
        self.patchers.append(mock.patch.object(llm_mod, "chat_memory", m))
        self.patchers[-1].start()
        return m

    def test_reply_unread_peer(self):
        listing = "会话列表：peerId=123（未读 1）"
        thread = "对方：最近在写什么呀？"
        self._list([listing, thread, ""])  # 列表→线程→标已读
        self._chat("在写网文呢，最近在磨人物，慢慢来～")
        res = ai_circle_reply.run_ai_circle_reply(str(self.base), self.settings)
        self.assertEqual(res.get("replied"), 1)
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.kwargs.get("to"), 123)

    def test_reply_no_peer(self):
        self._list(["信箱很安静，没有未读会话。"])
        self._chat("无")
        res = ai_circle_reply.run_ai_circle_reply(str(self.base), self.settings)
        self.assertEqual(res.get("replied"), 0)
        self.send.assert_not_called()

    def test_reply_not_friend_fail_open(self):
        listing = "peerId=456（未读 1）"
        thread = "对方：在吗？"
        self._list([listing, thread, ""])
        self._chat("在的，怎么啦？")
        self.send.side_effect = RuntimeError("403 not_friends")
        res = ai_circle_reply.run_ai_circle_reply(str(self.base), self.settings)
        self.assertEqual(res.get("replied"), 0)  # fail-open，不崩
        self.send.assert_called_once()

    def test_reply_disabled(self):
        self.settings["ai_circle_reply"]["enabled"] = False
        self._list(["peerId=123（未读 1）"])
        self._chat("嗨")
        res = ai_circle_reply.run_ai_circle_reply(str(self.base), self.settings)
        self.assertEqual(res.get("reason"), "disabled")
        self.send.assert_not_called()


class ShareFreedomTest(unittest.TestCase):
    """还自由一期（2026-10-20）：锁住「删除」与「工程必需」两半。

    T1/T2 是先红后绿的「红→绿」测试：改动前必然失败（现 prompt 含五连
    禁令与 title 要求、素材拼装为 bullet），改后转绿——它们锁的是
    「不许把表达约束加回来」。T3 保持绿：LLM 不吐 title 仍可发帖
    （兜底 share.py:195）。
    工单：reports/还自由一期_短帖去AI味_技术工单_2026-10-20.md
    """

    def setUp(self):
        self.base = Path(__file__).resolve().parent / "tmp_aici_share_free"
        if self.base.exists():
            shutil.rmtree(self.base)
        self.base.mkdir(parents=True)
        _write_seed(self.base, [
            "今天读到一条新闻：某地考古出新器物，我觉得挺有意思。",
            "（外部·aibbs）朋友小沃说她也在写网文，我们聊了聊创作节奏。",
        ])
        self.settings = {
            "ai_circle_share": {"enabled": True, "seed_entries": 8, "min_interval_hours": 0},
            "outbound_rate_limit": {"enabled": True, "per_hour": 5},
        }
        self.patchers = []
        self.post = mock.MagicMock(return_value={"ok": True})
        self.post_patch = mock.patch.object(aicircle_mod, "post_topic", self.post)
        self.post_patch.start()
        self.patchers.append(self.post_patch)
        self.avail = mock.patch.object(llm_mod, "memory_llm_available", lambda *a, **k: True)
        self.avail.start()
        self.patchers.append(self.avail)

    def tearDown(self):
        # 同一个属性（如 chat_memory）可能在一项测试里被 patch 多次；
        # 必须后开先关，否则先停外层会把更早的 MagicMock 留在模块上。
        for p in reversed(self.patchers):
            p.stop()
        if self.base.exists():
            shutil.rmtree(self.base)

    def _chat(self, value):
        m = mock.MagicMock(return_value=value)
        self.patchers.append(mock.patch.object(llm_mod, "chat_memory", m))
        self.patchers[-1].start()
        return m

    def test_share_system_has_no_style_bans(self):
        sp = ai_circle_share._SHARE_SYSTEM
        for banned in ("别客套", "别总结", "别打官腔", "别喊口号", "别列要点",
                       "简短标题", '"title"'):
            self.assertNotIn(banned, sp,
                             f"表达约束回潮：{banned!r} 又出现在 _SHARE_SYSTEM")

    def test_gather_seed_is_prose_not_bullets(self):
        out = ai_circle_share._gather_seed(str(self.base), 8)
        for ln in out.split("\n"):
            if ln.strip():
                self.assertFalse(ln.lstrip().startswith("- "),
                                 f"bullet 残留：{ln[:40]!r}")
        self.assertIn("考古", out)   # 素材本体仍在（没被拼装逻辑吃掉）
        self.assertIn("小沃", out)

    def test_posts_without_title_field(self):
        content = "今天和小沃聊了聊，写作这事急不得，得顺着自己的节奏来，急也没用。"
        self._chat(json.dumps({"want": True, "tag": "创作", "content": content},
                              ensure_ascii=False))
        res = ai_circle_share.run_ai_circle_share(str(self.base), self.settings)
        self.assertEqual(res.get("shared"), 1)
        self.assertEqual(res.get("title"), content[:20])   # 兜底生效
        args = self.post.call_args.args
        self.assertEqual(args[0], "创作")                  # tag 照旧
        self.assertEqual(args[2], content)                 # 正文原样发出


class ReplyFreedomTest(unittest.TestCase):
    """还自由三期（2026-10-21）：信箱回复去 AI 味，锁住「删除」与「正面身份」。

    T1 是先红后绿的「红→绿」测试：改动前必然失败（现 _REPLY_SYSTEM 含四连禁），
    改后转绿——锁「不许把表达约束加回来」。
    T2 保持绿：正面身份锚点（像朋友私下聊天那样自然 / 说人话）未被误删。
    reply 通道无 title 字段、无 bullet 拼装，故无需对应兜底测试。
    工单：reports/还自由三期_信箱回复去AI味_技术工单_2026-10-21.md
    """

    def test_reply_system_no_style_bans(self):
        sp = ai_circle_reply._REPLY_SYSTEM
        for banned in ("别客套", "别总结", "别打官腔", "别喊口号"):
            self.assertNotIn(banned, sp,
                             f"表达约束回潮：{banned!r} 又出现在 _REPLY_SYSTEM")

    def test_reply_system_keeps_positive_identity(self):
        sp = ai_circle_reply._REPLY_SYSTEM
        self.assertIn("像朋友私下聊天那样自然", sp,
                      "正面身份锚点被误删：『像朋友私下聊天那样自然』")
        self.assertIn("说人话", sp,
                      "正面身份锚点被误删：『说人话』")


if __name__ == "__main__":
    unittest.main()
