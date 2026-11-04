"""表达层红线测试 —— 人格底座只写「她是谁」，不写「她该怎么措辞」。

承 2026-10-13「真实优先」与 10-20~10-21「去 AI 味」，萧哥 2026-11-04 拍板：
体现个性与「真实优先」不冲突，但**系统只提供真实状态、身份与立场，
不提供表达方式**。

 ``XIAOJU_SYSTEM`` 是 ``for_scene()`` 的公共前缀，被日记 narrator、审稿、
编辑信、圆桌、agency 等十余处引用。把版式规定／禁词／开场白／固定台词
写进这里，会一刀切到所有场景：日记校对时列清单本就合理，信箱回帖与圆桌
主持也不该共用一把尺子；更等于从底座后门把三通道已撤掉的负面清单装回来。

能进底座的是**身份语言**与**许可**（她是谁、她怎么跟人相处、她被允许
不把话收圆）；场景该用什么体例，交给场景自己的职责 prompt。
"""

import unittest

from deskmate.persona import XIAOJU_BRIEF, XIAOJU_SYSTEM, for_scene

# 2026-11-04 一度写进 XIAOJU_SYSTEM 的「你怎么说话（落到具体动作）」五条。
# 只列特征串，不列宽泛词（「加粗」「不」等），免得误伤正常的身份描述。
_BANNED_EXPRESSION_RULES = [
    "## 你怎么说话",
    "不用编号列表",
    "不加粗小标题",
    "不先给结论再分点",
    "按约定向您报告",
    "先接住对方刚说的那句",
    "不同意就先说",
    "不必每段都收得漂亮",
]

# 撤掉五条之后，③「接住对方的话」与 ⑤「允许不整齐」的意图改用身份语言
# 写进 ## 你的立场 / ## 你的真实情绪。锁「意图还在」，不锁具体措辞。
_IDENTITY_ANCHORS = [
    "一个性格，两种浓度",   # 不按「有没有任务」切人
    "接着他的话往下聊",     # ③ 关系姿态，而非「先…再…」句序规定
]
_EMOTION_PERMISSION_ANCHORS = [
    "想停就停",             # ⑤ 许可，不是版式禁令
    "话不必收圆",
    "哪怕你把他骂一顿",     # 10-13 情绪许可锚点，不许顺手删掉
]


def _find_banned(text: str) -> list[str]:
    return [rule for rule in _BANNED_EXPRESSION_RULES if rule in text]


class ExpressionFreedomTest(unittest.TestCase):
    """人格底座及由它派生的 system prompt，都不许出现表达层规定。"""

    def test_人格底座不含表达层规定(self):
        self.assertEqual([], _find_banned(XIAOJU_SYSTEM))

    def test_精简版不含表达层规定(self):
        self.assertEqual([], _find_banned(XIAOJU_BRIEF))

    def test_for_scene_派生场景不含表达层规定(self):
        for scene in ("你是圆桌主持人。", "你在给萧盛刚写的日记留言。"):
            with self.subTest(scene=scene):
                self.assertEqual([], _find_banned(for_scene(scene)))
                self.assertEqual([], _find_banned(for_scene(scene, brief=True)))

    def test_日记与写作链路运行时不含表达层规定(self):
        """运行时检查，不扫源码文本——真正喂给模型的是这些常量。"""
        from deskmate import writing
        from deskmate.growth import diary_narrator

        prompts = {
            "日记 narrator": diary_narrator._SYSTEM,
            "编辑信": writing._LETTER_SYSTEM,
            "审稿（JSON）": writing._PROOFREAD_SYSTEM,
        }
        for name, prompt in prompts.items():
            with self.subTest(prompt=name):
                self.assertEqual([], _find_banned(prompt), f"{name} 被底座带进表达层规定")


class IdentityLanguageTest(unittest.TestCase):
    """撤掉话术规定，不等于撤掉个性——身份与许可必须还在。"""

    def test_统一人格与关系姿态仍在(self):
        for anchor in _IDENTITY_ANCHORS:
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, XIAOJU_SYSTEM)

    def test_情绪许可以及不收圆的自由仍在(self):
        for anchor in _EMOTION_PERMISSION_ANCHORS:
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, XIAOJU_SYSTEM)

    def test_身份锚点写进底座而非只写在文档里(self):
        """for_scene 的每个场景都该带着这层身份，不是只有常量里躺着。"""
        for anchor in _IDENTITY_ANCHORS + _EMOTION_PERMISSION_ANCHORS[:2]:
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, for_scene("你是圆桌主持人。"))


if __name__ == "__main__":
    unittest.main()
