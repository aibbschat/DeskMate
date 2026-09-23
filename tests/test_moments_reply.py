"""小桔私密日记留言回复的长度指引与回退模板（P0-1：活人感补足）。

不依赖 LLM：只锁住设计契约，以及 LLM 不可用时的回退模板不再是过短的 10~20 字。

变更史（务必连同本用例一起读，否则会误判是谁改坏了）：
- 2026-09-23 上午：原 15~40 字偏短（萧哥实测常仅 10~20 字），放宽到 40~80 字。
- 2026-09-23 晚间：**彻底去掉字数硬建议**——改为「长短随内容自然来，一两句到一小段
  都行」，并同步把生成上限 max_tokens 200 → 500（否则她想多写会在半句被静默切断）。

注意：笔画 asm 断言曾在第二阶段误锁具体的 "40"/"80" 字样。字数指引取消后，
断言改为锁**真正的设计价值**：不许出现任何形式的字数上限，但必须给出「自然长短」的正向指引。
"""

import re
import unittest

from deskmate.growth.moments import _REPLIES, _XIAOJU_SYSTEM, _fallback_reply


class TestXiaojuReplyLength(unittest.TestCase):
    def test_prompt_has_no_hard_char_cap(self):
        """设计契约：留言不再被任何字数区间/天花板约束。"""
        self.assertNotIn("15~40", _XIAOJU_SYSTEM)
        self.assertNotIn("40~80", _XIAOJU_SYSTEM)
        # 「N~M 字」「不超过 N 字」这类硬约束一律不许出现
        self.assertIsNone(
            re.search(r"\d+\s*[~～-]\s*\d+\s*字", _XIAOJU_SYSTEM),
            f"留言提示里出现了字数区间上限: {_XIAOJU_SYSTEM!r}",
        )
        self.assertIsNone(
            re.search(r"(不超过|控制在|限于|至多)\s*\d+\s*字", _XIAOJU_SYSTEM),
            f"留言提示里出现了字数天花板: {_XIAOJU_SYSTEM!r}",
        )
        # 正向：仍要有「自然长短」的指引，而非撒手不管
        self.assertIn("长短随内容自然来", _XIAOJU_SYSTEM)
        self.assertIn("具体", _XIAOJU_SYSTEM)

    def test_fallback_replies_are_not_too_short(self):
        # 回退模板不再过短（原部分仅 10~20 字）；统一 ≥ 20 字，更像人话。
        for r in _REPLIES:
            self.assertTrue(r.strip(), "回退模板不应为空")
            self.assertGreaterEqual(len(r), 20, f"回退模板过短: {r!r}")

    def test_fallback_reply_emotional_branches(self):
        cases = {
            "今天好累好烦": ("难过|累|烦|压力|焦虑|崩溃"),
            "终于搞定了真开心": ("开心|高兴|棒|成功|搞定|完成|顺利"),
            "又在加班赶项目": ("加班|工作|开会|项目|赶|出差"),
            "今天读了本书在写稿": ("读|书|文章|稿|写|创作|小说"),
        }
        for content, _ in cases.items():
            out = _fallback_reply(content)
            self.assertTrue(out.strip(), f"空回复: {content!r}")
            # 回退路径输出也应 ≥ 20 字（与 LLM 路径对齐，避免回落时突然变短）
            self.assertGreaterEqual(len(out), 20, f"回落回复过短: {content!r} -> {out!r}")

    def test_fallback_reply_empty_content_uses_generic(self):
        out = _fallback_reply("")
        self.assertIn(out, _REPLIES)
        self.assertTrue(out.strip())


if __name__ == "__main__":
    unittest.main()
