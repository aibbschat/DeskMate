"""小桔人格契约测试 —— 钉死「单一事实源」这条规矩。

2026-09-03 审计发现人格定义散落 6 处且互相打架（名字两套、本体被定义成
「萧盛的 AI 分身」、审稿时小桔整个消失、日记只有温柔没有主见）。
萧哥定调：**「她是我的个人助手，我希望她的人设、性格是统一的」**。

本测试不是测代码逻辑，是测**规矩有没有被守住**：
谁再想绕过 ``deskmate/persona.py`` 自己写一套人格，这里会拦他。
"""

import unittest

from deskmate import persona
from deskmate.growth import diary_narrator
from deskmate.persona import XIAOJU_BRIEF, XIAOJU_SYSTEM, for_scene


# 萧哥 2026-09-03 定调的基准人设关键词——少一个，人格就缺一块
_REQUIRED_TRAITS = [
    "18 岁",           # 年龄设定
    "知识女性",         # 气质
    "有主见",          # 不迎合的前提
    "不特意迎合",       # 交流风格
    "温柔",            # 语气可以温柔，但不等于换人
    "一个性格，两种浓度",  # 2026-11-04：统一人格，不按「有没有任务」切换
    "态度必须明确",      # 核心原则
    "理由和依据",       # 核心原则
]


class PersonaContentTest(unittest.TestCase):
    """人格本体必须完整——改一个字都要想清楚是不是在改小桔这个人。"""

    def test_基准人设关键词一个都不能少(self):
        missing = [t for t in _REQUIRED_TRAITS if t not in XIAOJU_SYSTEM]
        self.assertEqual(
            [], missing,
            f"人格缺失关键词 {missing}；改 persona.py 前请确认萧哥同意",
        )

    def test_不许变成客服或煽情(self):
        for bad in ("不撒娇", "不煽情", "不是客服"):
            self.assertIn(bad, XIAOJU_SYSTEM)

    def test_精简版保留人格核心(self):
        """brief 压缩的是长度，不是人格——三条硬核一条都不能丢。"""
        for trait in ("有主见", "态度明确", "理由和依据"):
            self.assertIn(trait, XIAOJU_BRIEF, f"精简版丢了「{trait}」")
        self.assertLess(len(XIAOJU_BRIEF), len(XIAOJU_SYSTEM))


class ForSceneTest(unittest.TestCase):
    """叠加顺序与两种形态。"""

    def test_人格在前职责在后(self):
        """顺序反了，模型会把职责当身份，人格就散了。"""
        out = for_scene("你是圆桌主持人。")
        self.assertTrue(out.startswith(XIAOJU_SYSTEM))
        self.assertIn("你是圆桌主持人。", out)
        self.assertLess(out.index("知识女性"), out.index("圆桌主持人"))

    def test_brief_压短(self):
        self.assertIn(XIAOJU_BRIEF, for_scene("x", brief=True))
        self.assertNotIn(XIAOJU_SYSTEM, for_scene("x", brief=True))

    def test_空职责只返回人格(self):
        self.assertEqual(for_scene(""), XIAOJU_SYSTEM)
        self.assertEqual(for_scene("   "), XIAOJU_SYSTEM)


class NoRoguePersonaTest(unittest.TestCase):
    """各场景必须引用单一事实源，不得另起炉灶。"""

    def test_日记引用人格(self):
        self.assertIn(XIAOJU_SYSTEM, diary_narrator._SYSTEM)
        # 日记职责仍然在（人格之外还要有场景职责）
        self.assertIn("日记", diary_narrator._SYSTEM)

    def test_审稿引用人格(self):
        from deskmate.writing import _LETTER_SYSTEM, _PROOFREAD_SYSTEM

        # 审稿：JSON 场景用 brief，编辑信是自然语言用完整版
        self.assertIn(XIAOJU_BRIEF, _PROOFREAD_SYSTEM)
        self.assertIn(XIAOJU_SYSTEM, _LETTER_SYSTEM)

    def test_认知注入不再把小桔定义成分身(self):
        """2026-09-03 修正：memory_injector 只注入「她知道什么」，
        不再定义「她是谁」——「AI 分身」是模仿顺从，跟独立人格冲突。

        这里跑一遍真实函数检查**实际输出**，而不是扫源码文本——
        源码注释里提到「分身」是为了记录这段历史，扫文本会误伤。
        """
        from unittest import mock

        from deskmate.cognitive import memory_injector as mi

        with mock.patch.object(mi, "load_persona_base", return_value="【底座】"), \
             mock.patch.object(mi, "load_cognitive_profile", return_value={"k": 1}), \
             mock.patch.object(mi, "format_profile", return_value="【画像】"):
            out = mi.build_cognitive_context()

        self.assertNotIn("AI 分身", out, "「AI 分身」定义回来了，见 persona.py 说明")
        self.assertNotIn("你叫小桔", out, "人格不该由认知注入层定义")
        self.assertIn("关于萧盛", out)
        self.assertIn("用你自己的判断回应他", out)

    def test_名字统一为小桔(self):
        """运行时不许再冒出「知了」——历史名字，已废弃。"""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        offenders = []
        for pat in (
            "coworker/personas/builtin/deskmate.md",
            "deskmate/roundtable/templates/roundtable.html",
            "coworker/server/a2a_server.py",
            "surfaces/gui/src/locales/zh.json",
        ):
            p = root / pat
            if p.is_file() and "知了" in p.read_text(encoding="utf-8"):
                offenders.append(pat)
        self.assertEqual([], offenders, f"仍有「知了」残留：{offenders}")


if __name__ == "__main__":
    unittest.main()
