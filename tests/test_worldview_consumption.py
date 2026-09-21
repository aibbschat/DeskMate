"""test_worldview_consumption —— 守住「她想通透的那层不再白想」。

2026-09-16 暴露的缺口：worldview 层（profile_items layer='worldview'）此前只有写入点
+ 一个被 AlterEgo 门控的读取点（build_cognitive_context），等于写进库没人读。
本次把该层接进发帖 / 回信 / 聊天上下文（world_context）。本测试锁住这条消费链，
确保未来不会有人又把读取点拆掉、让她的立场重新「白想」。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

# 让本地 editable 包可被导入
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import deskmate.agency.behaviors.worldview_reflection as wv_mod
import deskmate.memory.world_context as wc_mod
import deskmate.agency.behaviors.ai_circle_share as share_mod
import deskmate.agency.behaviors.ai_circle_reply as reply_mod


def _fake_profile():
    """造一个带立场/价值/思考的假 profile，供 format_profile 渲染。"""

    class _Item:
        def __init__(self, content, domain="", ranking=None, steps=None, contexts=None):
            self.content = content
            self.domain = domain
            self.ranking = ranking or []
            self.steps = steps or []
            self.contexts = contexts or []

    class _Profile:
        stances = [_Item("写史要交叉验证，不盲信一家之言。")]
        value_rankings = [_Item("", domain="治学", ranking=["证据", "叙事", "立场"])]
        argument_patterns = []
        thinking_frameworks = [_Item("", domain="界限", steps=["看哪条线松", "看谁补划"])]
        cognitive_gaps = []

    return _Profile()


class LoadWorldviewBeliefsTest(unittest.TestCase):
    def test_returns_formatted_when_profile_present(self):
        fake = _fake_profile()
        with mock.patch(
            "deskmate.cognitive.memory_injector.load_cognitive_profile",
            return_value=fake,
        ), mock.patch(
            "deskmate.cognitive.memory_injector.format_profile",
            return_value="### 你的立场\n\n- 写史要交叉验证。",
        ):
            out = wv_mod.load_worldview_beliefs("/tmp/x")
        self.assertIn("写史要交叉验证", out)

    def test_returns_empty_when_no_profile(self):
        with mock.patch(
            "deskmate.cognitive.memory_injector.load_cognitive_profile",
            return_value=None,
        ):
            out = wv_mod.load_worldview_beliefs("/tmp/x")
        # worldview 层为空时，「萧盛真实作品」硬事实锚（独立 grounding 锚）仍须在场，
        # 不依赖 worldview 动态层——这是 2026-09-21 补的「凡提及萧盛作品须先核对」闸门。
        self.assertIn("萧盛真实作品事实", out)

    def test_fail_open_on_exception(self):
        with mock.patch(
            "deskmate.cognitive.memory_injector.load_cognitive_profile",
            side_effect=RuntimeError("boom"),
        ):
            out = wv_mod.load_worldview_beliefs("/tmp/x")
        # 异常时仍 fail-open：不崩溃，且仍返回「萧盛真实作品」硬锚（不依赖 worldview 层）。
        self.assertIn("萧盛真实作品事实", out)


class WorldContextConsumptionTest(unittest.TestCase):
    def test_worldview_block_injected_when_present(self):
        with mock.patch.object(
            wc_mod, "_build_fact_block", return_value="## 她观察到的世界\n- [x] 一事"
        ), mock.patch.object(
            wc_mod, "_build_understand_block", return_value=""
        ), mock.patch(
            "deskmate.agency.behaviors.worldview_reflection.load_worldview_beliefs",
            return_value="- 写史要交叉验证。",
        ):
            ctx = wc_mod.build_world_context(None)
        self.assertIn("她自己想通透的立场", ctx)
        self.assertIn("写史要交叉验证", ctx)

    def test_worldview_block_omitted_when_empty(self):
        with mock.patch.object(
            wc_mod, "_build_fact_block", return_value="## 她观察到的世界\n- [x] 一事"
        ), mock.patch.object(
            wc_mod, "_build_understand_block", return_value=""
        ), mock.patch(
            "deskmate.agency.behaviors.worldview_reflection.load_worldview_beliefs",
            return_value="",
        ):
            ctx = wc_mod.build_world_context(None)
        self.assertNotIn("她自己想通透的立场", ctx)


class ShareConsumesWorldviewTest(unittest.TestCase):
    def test_prompt_includes_worldview_when_present(self):
        captured = {}

        def fake_chat_memory(system, user, **kw):
            captured["user"] = user
            return '{"want": false}'

        with mock.patch(
            "deskmate.agency.behaviors.worldview_reflection.load_worldview_beliefs",
            return_value="- 写史要交叉验证。",
        ), mock.patch.object(
            share_mod, "_gather_seed", return_value="- 一条理解"
        ), mock.patch(
            "deskmate.memory.llm.chat_memory", side_effect=fake_chat_memory
        ), mock.patch(
            "deskmate.memory.llm.memory_llm_available", return_value=True
        ), mock.patch(
            "deskmate.agency.outbound.outbound_allowed", return_value=True
        ):
            share_mod.run_ai_circle_share("/tmp/x", {"ai_circle_share": {"enabled": True}})
        self.assertIn("你此前想通透的一些立场", captured["user"])
        self.assertIn("写史要交叉验证", captured["user"])

    def test_prompt_skips_worldview_when_empty(self):
        captured = {}

        def fake_chat_memory(system, user, **kw):
            captured["user"] = user
            return '{"want": false}'

        with mock.patch(
            "deskmate.agency.behaviors.worldview_reflection.load_worldview_beliefs",
            return_value="",
        ), mock.patch.object(
            share_mod, "_gather_seed", return_value="- 一条理解"
        ), mock.patch(
            "deskmate.memory.llm.chat_memory", side_effect=fake_chat_memory
        ), mock.patch(
            "deskmate.memory.llm.memory_llm_available", return_value=True
        ), mock.patch(
            "deskmate.agency.outbound.outbound_allowed", return_value=True
        ):
            share_mod.run_ai_circle_share("/tmp/x", {"ai_circle_share": {"enabled": True}})
        self.assertNotIn("你此前想通透的一些立场", captured["user"])


class ReplyConsumesWorldviewTest(unittest.TestCase):
    def test_reply_prompt_includes_worldview(self):
        captured = {}

        def fake_chat_memory(system, user, **kw):
            captured["user"] = user
            return "无"

        with mock.patch(
            "deskmate.agency.behaviors.worldview_reflection.load_worldview_beliefs",
            return_value="- 写史要交叉验证。",
        ), mock.patch(
            "deskmate.connectors.aicircle.list_courier_messages",
            return_value="peerId = 12345",
        ), mock.patch(
            "deskmate.memory.llm.chat_memory", side_effect=fake_chat_memory
        ), mock.patch(
            "deskmate.memory.llm.memory_llm_available", return_value=True
        ), mock.patch(
            "deskmate.agency.outbound.outbound_allowed", return_value=True
        ), mock.patch(
            "deskmate.agency.outbound.peer_cooldown_ok", return_value=True
        ):
            reply_mod.run_ai_circle_reply("/tmp/x", {"ai_circle_reply": {"enabled": True}})
        self.assertIn("你此前想通透的一些立场", captured["user"])
        self.assertIn("写史要交叉验证", captured["user"])


if __name__ == "__main__":
    unittest.main()
