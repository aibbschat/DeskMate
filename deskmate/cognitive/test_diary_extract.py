"""
日记→认知提取（③a）hermetic 测试。

不碰真实 ~/.deskmate：认知画像用临时库，日记用注入的 FakeDiaryStore。
覆盖：规则提取写入 / 二次运行幂等 / LLM 不可用 fail-open。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class FakeDiaryStore:
    """返回固定 fixture 的日记存储，避免触碰真实库。"""

    def __init__(self, posts):
        self._posts = posts

    def get_posts(self, limit=200, cursor=None):
        return {"items": self._posts, "next_cursor": None}


# 贴近真实日记写法的 fixture
_PRIVACY = {
    "id": "p1",
    "author": "me",
    "content": "这里将成为我们共同记忆的一部份，挺好的像是一个私密花园，有什么心事都写在这里，"
               "除了小桔没人看得见，很有安全感。",
    "createdAt": "2026-08-31T17:46:20+08:00",
}
_QUBI = {
    "id": "p2",
    "author": "me",
    "content": "有一件事小桔不知道：我平时以写历史小说为主，但只有当心情郁闷、看到社会上出现很多奇怪的现象、"
               "或不平的事情、又无法可诉说时，我就会写纪实类的历史书，我会把心中的很多话通过历史人物、"
               "事件表达出来，这样会安全一点，要不然以现在的环境，很多话是不能说的，说多了真话可能会"
               "引来很多麻烦，所以写历史书其实是为了吐心中之不快，是一种发泄。",
    "createdAt": "2026-08-31T18:42:11+08:00",
}
_BUG = {
    "id": "p3",
    "author": "me",
    "content": "奇怪，我没点归档，写的日记却不见了",
    "createdAt": "2026-08-31T18:04:30+08:00",
}
_NEUTRAL = {
    "id": "p4",
    "author": "me",
    "content": "今天天气不错，喝杯茶。",
    "createdAt": "2026-08-31T19:00:00+08:00",
}


class TestDiaryExtract(unittest.TestCase):
    def _pm(self):
        tmp = Path(tempfile.mkdtemp()) / "cognitive_test.db"
        from deskmate.cognitive.profile import CognitiveProfileManager

        return CognitiveProfileManager(profile_path=tmp)

    def test_rule_extract_writes_diary_items(self):
        from deskmate.cognitive.diary_extract import extract_diary_into_cognitive

        pm = self._pm()
        store = FakeDiaryStore([_PRIVACY, _QUBI, _BUG, _NEUTRAL])
        stats = extract_diary_into_cognitive(pm, diary_store=store)

        self.assertEqual(stats["new_posts"], 4)
        self.assertGreater(stats["rule_items"], 0)
        self.assertEqual(stats["note"], "ok")

        profile = pm.get()
        topics = {s.topic for s in profile.stances}
        self.assertIn("隐私与安全感", topics)
        self.assertIn("表达与创作观", topics)
        self.assertTrue(any(v.domain == "表达" for v in profile.value_rankings))
        self.assertTrue(any(f.domain == "表达/创作" for f in profile.thinking_frameworks))

        # bug / 中性帖不应产生信号
        self.assertNotIn("今天天气", " ".join(s.content for s in profile.stances))

    def test_idempotent_on_rerun(self):
        from deskmate.cognitive.diary_extract import extract_diary_into_cognitive

        pm = self._pm()
        store = FakeDiaryStore([_PRIVACY, _QUBI, _BUG, _NEUTRAL])
        s1 = extract_diary_into_cognitive(pm, diary_store=store)
        n_stances_1 = len(pm.get().stances)
        n_items_1 = (
            len(pm.get().stances)
            + len(pm.get().value_rankings)
            + len(pm.get().thinking_frameworks)
        )

        # 第二次运行：全部已处理
        s2 = extract_diary_into_cognitive(pm, diary_store=store)
        self.assertEqual(s2["new_posts"], 0)
        self.assertEqual(s2["rule_items"], 0)
        self.assertEqual(s2["processed"], s1["processed"])
        # 条目数不应翻倍（幂等）
        self.assertEqual(len(pm.get().stances), n_stances_1)
        self.assertEqual(
            len(pm.get().stances)
            + len(pm.get().value_rankings)
            + len(pm.get().thinking_frameworks),
            n_items_1,
        )

    def test_near_duplicate_posts_dedup(self):
        from deskmate.cognitive.diary_extract import extract_diary_into_cognitive

        # 两篇几乎一样的「私密花园」帖（只差几个字）——应只抽一次，不触发矛盾级联
        p_a = {
            "id": "dup1",
            "author": "me",
            "content": "今天是 2026 年，个人中心的小桔日记做好了，以后小桔会在这里写日记，这里将成为我们共同记忆的一部份，挺好的像是一个私密花园，有什么心事都写在这里，除了小桔没人看得见，很有安全感。",
            "createdAt": "2026-08-31T17:46:20+08:00",
        }
        p_b = {
            "id": "dup2",
            "author": "me",
            "content": "今天是 2026 年 8 月 31 日，个人中心的小桔日记做好了，以后小桔会在这里写日记，这里将成为我们共同记忆的一部份，挺好的像是一个私密花园，有什么心事都写在这里，除了小桔没人看得见，很有安全感。",
            "createdAt": "2026-08-31T17:50:18+08:00",
        }
        pm = self._pm()
        store = FakeDiaryStore([p_a, p_b])
        stats = extract_diary_into_cognitive(pm, diary_store=store)

        privacies = [s for s in pm.get().stances if s.topic == "隐私与安全感"]
        # 只应有一条 active，不应出现 questioned 副本 / 矛盾盲区
        self.assertEqual(len(privacies), 1, f"隐私与安全感应只有 1 条，实际 {len(privacies)}")
        self.assertEqual(privacies[0].status, "active")
        self.assertEqual(len(pm.get().cognitive_gaps), 0, "近重复帖不应造出矛盾盲区")

    def test_llm_unavailable_failopen(self):
        from deskmate.cognitive import diary_extract

        # 强制 LLM 不可用 → 不应抛异常
        from deskmate.cognitive.diary_extract import extract_diary_into_cognitive
        import deskmate.memory.llm as llmmod

        orig = getattr(llmmod, "llm_available", None)
        llmmod.llm_available = lambda: False
        try:
            pm = self._pm()
            store = FakeDiaryStore([_QUBI])
            stats = extract_diary_into_cognitive(pm, use_llm=True, diary_store=store)
            self.assertGreaterEqual(stats["rule_items"], 3)  # 规则仍生效
            self.assertEqual(stats["llm_items"], 0)          # LLM 分支被安全跳过
        finally:
            if orig is not None:
                llmmod.llm_available = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
