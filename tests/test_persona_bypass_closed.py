"""人格底座注入契约 —— 靶子③「人格旁路」关闭（萧哥 2026-11-16 拍板）。

背景：09-03 定调「人格单一事实源，各场景只能 for_scene() 引用 + 叠加场景职责」，
但实测有 6 处真旁路（moments / understanding / ai_circle ×3 / emotion_engine 预期故事）
在场景 prompt 里自己写「你是小桔……」，随后只给体例、不给立场——同一个人切到这些
场景就只剩「温暖、有烟火味」，不会不同意、不会反驳。萧哥拍板：moments 要「真实的
搭档」（给她反驳权）、ai_circle 公开形象不介意变化、免费组已撤（全付费模型，加长
prompt 不再有遵循风险）。

本契约锁三件事：
1. 每处 system prompt 都能见到**底座锚点**（身份与立场真的注入了，不是只挂了个前缀）；
2. 「你是小桔」在每条 system 里只出现 **1 次**（底座给的；场景层不得再定义一次人格）；
3. JSON 输出场景用 brief 版（压缩表述长度、不干扰格式稳定性），非 JSON 场景用全量。

另锁 writing_advisor：它本来就 for_scene 了，但 _ROLE 自带身份重述（双重声明），
已收敛为纯职责，防止回退。
"""

from __future__ import annotations

import unittest

# 全量底座独有锚点（见 deskmate/persona.py XIAOJU_SYSTEM）
_LONG_ANCHORS = ("一个性格，两种浓度", "哪怕你把他骂一顿")
# brief 底座独有锚点（见 XIAOJU_BRIEF）
_BRIEF_ANCHORS = ("不迎合也不抬杠", "说话温柔但态度明确")


def _count_identity(text: str) -> int:
    return text.count("你是小桔")


class TestBypassClosed(unittest.TestCase):
    """6 处真旁路都必须吃到底座。"""

    def test_moments_has_base(self) -> None:
        from deskmate.growth import moments

        sys_prompt = moments._XIAOJU_SYSTEM
        for anchor in _LONG_ANCHORS:
            self.assertIn(anchor, sys_prompt, "日记留言必须吃全量底座")
        self.assertEqual(_count_identity(sys_prompt), 1, "场景层不得再定义一次人格")
        # 体例原样保留（09-23 两次调参的成果不许丢）
        self.assertIn("长短随内容自然来", sys_prompt)

    def test_understanding_has_base(self) -> None:
        from deskmate.proactivity import understanding

        sys_prompt = understanding._SYSTEM_PROMPT
        for anchor in _LONG_ANCHORS:
            self.assertIn(anchor, sys_prompt, "主动理解必须吃全量底座")
        self.assertEqual(_count_identity(sys_prompt), 1)
        self.assertIn("120 字以内", sys_prompt, "体例保留")

    def test_ai_circle_column_has_base(self) -> None:
        from deskmate.agency.behaviors import ai_circle_column

        sys_prompt = ai_circle_column._COLUMN_SYSTEM
        for anchor in _LONG_ANCHORS:
            self.assertIn(anchor, sys_prompt, "主页手记必须吃全量底座")
        self.assertEqual(_count_identity(sys_prompt), 1)
        self.assertIn("说人话、有烟火味", sys_prompt, "体例保留")

    def test_ai_circle_reply_has_base(self) -> None:
        from deskmate.agency.behaviors import ai_circle_reply

        sys_prompt = ai_circle_reply._REPLY_SYSTEM
        for anchor in _LONG_ANCHORS:
            self.assertIn(anchor, sys_prompt, "数字信回信必须吃全量底座")
        self.assertEqual(_count_identity(sys_prompt), 1)

    def test_ai_circle_share_has_brief_base(self) -> None:
        from deskmate.agency.behaviors import ai_circle_share

        sys_prompt = ai_circle_share._SHARE_SYSTEM
        for anchor in _BRIEF_ANCHORS:
            self.assertIn(anchor, sys_prompt, "JSON 输出场景用 brief 底座")
        self.assertNotIn(
            _LONG_ANCHORS[0], sys_prompt, "JSON 场景不该背全量底座"
        )
        self.assertEqual(_count_identity(sys_prompt), 1)

    def test_emotion_expectation_has_brief_base(self) -> None:
        from deskmate.growth import emotion_engine

        sys_prompt = emotion_engine._PENDING_EXPECT_SYSTEM
        for anchor in _BRIEF_ANCHORS:
            self.assertIn(anchor, sys_prompt, "悬事预期是 JSON 输出，用 brief 底座")
        self.assertEqual(_count_identity(sys_prompt), 1)
        self.assertIn("严格 JSON", sys_prompt, "格式约束保留")


class TestNoDoubleIdentity(unittest.TestCase):
    """writing_advisor 不是旁路（本来就 for_scene），但曾双重声明身份。"""

    def test_writing_advisor_scene_prompt_has_no_identity(self) -> None:
        from deskmate.cognitive import writing_advisor

        # _ROLE 是场景职责（for_scene 的入参），不得再含人格定义
        self.assertEqual(_count_identity(writing_advisor._ROLE), 0)
        # 成品 system 里身份只由底座提供一次
        self.assertEqual(_count_identity(writing_advisor._SYSTEM), 1)
        # 角色边界是职责，必须还在
        self.assertIn("不是虚拟伴侣", writing_advisor._ROLE)


class TestInternalStateMachineUntouched(unittest.TestCase):
    """emotion_engine 的激素计算是内部状态机（任务角色非人格），明确不挂底座。"""

    def test_emotion_engine_system_stays_mechanical(self) -> None:
        from deskmate.growth import emotion_engine

        self.assertNotIn("一个性格，两种浓度", emotion_engine._EMOTION_SYSTEM)
        self.assertNotIn("不迎合也不抬杠", emotion_engine._EMOTION_SYSTEM)
        self.assertIn("激素", emotion_engine._EMOTION_SYSTEM)


if __name__ == "__main__":
    unittest.main()
