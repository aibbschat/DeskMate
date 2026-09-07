"""小桔日记「灵感机制」测试。

锁定几条不能退化的行为（对应 2026-09-02 定调的设计原则）：
1. 素材不够就**不写**——日记不是定时任务，不能产出流水账。
2. 批量事件被单类封顶——一次导入 15 条记忆 ≠ 15 件值得写的事。
3. 素材账本持久——recent_events 只有 50 条窗口，会丢素材。
4. **一天可以写多则**：写过的素材被消费掉，新素材攒够就再记一笔，
   且第二则不重复第一则写过的素材（覆盖/复述都是退化）。
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from deskmate.growth import diary_inspiration as di
from deskmate.growth.diary import DiaryWriter
from deskmate.growth.diary_inspiration import (
    _INSPIRE_THRESHOLD,
    _KIND_CAP,
    _MIN_GAP_MINUTES,
    _NEXT_ENTRY_DELTA,
    DiaryInspiration,
    MaterialLedger,
    score_items,
)


def _evt(kind: str, day: str, n: int = 1, **extra) -> list[dict]:
    """造 n 条同类型事件（ts 按序区分，避免指纹撞车）。"""
    out = []
    for i in range(n):
        out.append({
            "ts": f"{day}T{10 + i // 60:02d}:{i % 60:02d}:00+00:00",
            "kind": kind,
            **extra,
        })
    return out


class ScoreItemsTest(unittest.TestCase):
    """灵感评分：加权 + 单类封顶。"""

    def test_single_memory_not_enough(self):
        """只学到一件小事 → 远不到阈值，不该动笔。"""
        score, detail = score_items(_evt("memory_learned", "2026-08-24"))
        self.assertLess(score, _INSPIRE_THRESHOLD)
        self.assertEqual(detail, {"memory_learned": 0.5})

    def test_batch_memory_capped(self):
        """批量导入 15 条记忆：封顶，不许刷分。"""
        score, detail = score_items(_evt("memory_learned", "2026-09-01", 15))
        self.assertAlmostEqual(detail["memory_learned"], _KIND_CAP)
        self.assertLessEqual(score, _KIND_CAP)

    def test_batch_skill_evolved_capped(self):
        """11 个技能同时进化：一次批处理的副产品，同样封顶。"""
        _, detail = score_items(_evt("skill_evolved", "2026-09-01", 11))
        self.assertAlmostEqual(detail["skill_evolved"], _KIND_CAP)

    def test_corrections_reach_threshold(self):
        """3 次纠正 → 达标（对标真实 08-30：确实值得写一篇）。"""
        score, _ = score_items(_evt("user_corrected", "2026-08-30", 3))
        self.assertGreaterEqual(score, _INSPIRE_THRESHOLD)

    def test_backfill_is_not_material(self):
        """系统补录不算当天素材。"""
        score, detail = score_items(_evt("depth_backfilled", "2026-08-30", 5))
        self.assertEqual(score, 0.0)
        self.assertEqual(detail, {})

    def test_milestone_weighs_more_than_trace(self):
        """里程碑（新建技能）比痕迹（聊天）重。"""
        a, _ = score_items(_evt("skill_created", "2026-09-02"))
        b, _ = score_items(_evt("chat", "2026-09-02"))
        self.assertGreater(a, b)


class LedgerTest(unittest.TestCase):
    """素材账本：持久、去重、抗 recent_events 截断。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sync_is_idempotent(self):
        """同一批事件同步两次，账本不重复。"""
        led = MaterialLedger(self.base)
        evts = _evt("user_corrected", "2026-09-02", 3)
        self.assertEqual(led.sync(evts), 3)
        self.assertEqual(led.sync(evts), 0)
        self.assertEqual(len(led.load("2026-09-02")), 3)

    def test_distinct_subjects_all_kept(self):
        """不同主体（不同技能）各自保留，不被指纹去重误杀。"""
        led = MaterialLedger(self.base)
        evts = _evt("skill_used", "2026-09-02", 1, skill_name="A") + \
               _evt("skill_used", "2026-09-02", 1, skill_name="B")
        led.sync(evts)
        self.assertEqual(len(led.load("2026-09-02")), 2)

    def test_outlives_event_window(self):
        """账本比 recent_events 活得久——窗口滚过去后素材仍在。

        这是账本存在的唯一理由：recent_events 落盘截断到 50 条。
        """
        led = MaterialLedger(self.base)
        led.sync(_evt("user_corrected", "2026-08-30", 3))
        # 模拟窗口滚动：后续事件涌入，旧事件被挤出 recent_events
        led.sync(_evt("memory_learned", "2026-09-01", 30))
        self.assertEqual(len(led.load("2026-08-30")), 3)

    def test_consume_marks_only_pending(self):
        """素材被某一则采用后打上 entry，pending 立刻清空（不重复计分）。"""
        led = MaterialLedger(self.base)
        led.sync(_evt("user_corrected", "2026-09-02", 3))
        led.sync(_evt("skill_created", "2026-09-02", 1, skill_name="新技能"))

        pending = led.pending_items("2026-09-02")
        self.assertEqual(len(pending), 4)
        corrections = [x for x in pending if x.get("kind") == "user_corrected"]
        self.assertEqual(led.consume("2026-09-02", corrections, 1), 3)

        rest = led.pending_items("2026-09-02")
        self.assertEqual(len(rest), 1)
        self.assertEqual(rest[0].get("kind"), "skill_created")
        # 消费只打标记，不删素材——账本仍是当天完整流水
        self.assertEqual(len(led.load("2026-09-02")), 4)


class ShouldWriteTest(unittest.TestCase):
    """动笔判断：阈值 + 一天多则（不是写完就锁死）。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        # 造一份 growth.json，让 GrowthEngine 在这个隔离目录里工作
        today = date.today().isoformat()
        (self.base / "growth.json").write_text(
            json.dumps({
                "version": 2, "intimacy": 50.0, "level": 5, "total_chats": 100,
                "recent_events": _evt("user_corrected", today, 3),
            }),
            encoding="utf-8",
        )
        # 本类关心「一天多则」的写入机制（不是 narrator 写得好不好），
        # 关掉 narrator 让断言稳定在规则版措辞。
        # 2026-09-07：narrator 闸门放宽后，纯 ``user_corrected`` 类事件也能
        # 让 narrator 跑通出自然段；本测试不该被 narrator 写得漂不漂亮影响。
        self._narrate_off = mock.patch.dict(
            os.environ, {"DESKMATE_DIARY_NARRATE": "0"}
        )
        self._narrate_off.start()

    def tearDown(self):
        self._narrate_off.stop()
        self._tmp.cleanup()

    def _write_now(self):
        """真的落笔一则（绕过冷却，测试里不关心间隔）。"""
        with mock.patch.object(di, "_MIN_GAP_MINUTES", 0):
            return di.maybe_write_diary(base_dir=self.base)

    def test_writes_when_enough(self):
        insp = DiaryInspiration(self.base)
        verdict = insp.should_write()
        self.assertTrue(verdict["should_write"])
        self.assertFalse(verdict["is_refresh"])
        self.assertEqual(verdict["entries_today"], 0)
        self.assertGreaterEqual(verdict["score"], _INSPIRE_THRESHOLD)

    def test_no_second_entry_without_new_material(self):
        """刚写完、没有新素材 → 不再记（素材已消费，pending 归零）。"""
        first = self._write_now()
        self.assertIsNotNone(first)
        self.assertEqual(first["entry"], 1)

        again = DiaryInspiration(self.base).should_write()
        self.assertFalse(again["should_write"])
        self.assertEqual(again["entries_today"], 1)
        self.assertEqual(again["pending_count"], 0)

    def test_second_entry_keeps_the_first(self):
        """新素材攒够 → 再记一则；第一则的内容**不被覆盖**。

        2026-09-07 备注：本类已在 setUp 关 narrator，断言按规则版措辞。
        """
        first = self._write_now()
        MaterialLedger(self.base).sync(
            _evt("skill_created", first["date"], 1, skill_name="新技能")
        )
        second = self._write_now()
        self.assertIsNotNone(second)
        self.assertEqual(second["entry"], 2)

        writer = DiaryWriter(self.base)
        self.assertEqual(writer.entry_count(second["date"]), 2)
        text = writer.read(second["date"])
        self.assertIn("你纠正了我 3 次", text)   # 第一则原样保留（规则版）
        self.assertIn("建了个新技能", text)      # 第二则写进了新素材

    def test_second_entry_does_not_repeat_first(self):
        """第二则只写后来的事——不把第一则复述一遍（重复叙事比不写更伤）。"""
        first = self._write_now()
        MaterialLedger(self.base).sync(
            _evt("skill_created", first["date"], 1, skill_name="新技能")
        )
        self.assertIsNotNone(self._write_now())

        text = DiaryWriter(self.base).read(first["date"])
        # 取最后一则（第二个 ## 之后）
        parts = text.split("\n## ")
        self.assertEqual(len(parts), 3)  # 标题行 + 两则
        second_entry = parts[-1]
        self.assertIn("建了个新技能", second_entry)
        self.assertNotIn("纠正", second_entry)

    def test_three_entries_when_material_keeps_coming(self):
        """素材持续到位就持续记——一天几条由素材决定，不设上限。"""
        first = self._write_now()
        day = first["date"]
        led = MaterialLedger(self.base)
        for i in range(2):
            # 每轮 2 次 writing = 4.0 分，刚好够「再记一笔」的门槛
            led.sync([
                {"ts": f"{day}T1{i}:00:00+00:00", "kind": "writing"},
                {"ts": f"{day}T1{i}:30:00+00:00", "kind": "writing"},
            ])
            res = self._write_now()
            self.assertIsNotNone(res, f"第 {i + 2} 则没写成")
        self.assertEqual(DiaryWriter(self.base).entry_count(day), 3)

    def test_manual_path_consumes_material(self):
        """手动点「生成今日日记」后，素材必须被消费掉。

        否则自动路径（maybe_write_diary）看这批素材还是「没写过」，
        会把同一批素材再写一则——日记里出现两遍一模一样的话。
        """
        day = date.today().isoformat()
        writer = DiaryWriter(self.base)
        writer.ensure_today()

        led = MaterialLedger(self.base)
        self.assertEqual(led.pending_items(day), [], "手动写完后素材仍是未消费状态")
        self.assertEqual(led.entries(day), 1)

        # 没有新素材时，自动路径不该再动笔
        with mock.patch.object(di, "_MIN_GAP_MINUTES", 0):
            self.assertIsNone(di.maybe_write_diary(base_dir=self.base))
        self.assertEqual(writer.entry_count(day), 1)

    def test_manual_second_click_appends(self):
        """已有日记 + 有未写素材 → 手动再点一次是「再记一笔」，不是覆盖。"""
        day = date.today().isoformat()
        writer = DiaryWriter(self.base)
        writer.ensure_today()

        MaterialLedger(self.base).sync([
            {"ts": f"{day}T20:00:00", "kind": "writing"},
            {"ts": f"{day}T20:10:00", "kind": "roundtable"},
        ])
        path = writer.ensure_today()
        self.assertEqual(writer.entry_count(day), 2)
        self.assertIn("圆桌讨论", writer.read(day))
        self.assertTrue(Path(path).exists())

    def test_cooling_blocks_back_to_back(self):
        """素材集中涌入时，冷却期内不连着刷几则。"""
        first = self._write_now()
        MaterialLedger(self.base).sync(
            _evt("skill_created", first["date"], 1, skill_name="新技能")
        )
        verdict = DiaryInspiration(self.base).should_write()
        # 分数已够，但离上一则太近
        self.assertGreaterEqual(verdict["score"], _NEXT_ENTRY_DELTA)
        self.assertFalse(verdict["should_write"])
        self.assertIn("沉淀", verdict["reason"])

        # 把落笔时间往前挪过冷却期 → 可以再记了
        led = MaterialLedger(self.base)
        led._write_meta(first["date"], {
            "entries": 1,
            "written_score": 6.0,
            "last_entry_at": (
                datetime.now() - timedelta(minutes=_MIN_GAP_MINUTES + 5)
            ).isoformat(timespec="seconds"),
        })
        self.assertTrue(DiaryInspiration(self.base).should_write()["should_write"])

    def test_silent_when_nothing_happened(self):
        """一整天没什么事 → 安静等着，绝不凑一篇。"""
        (self.base / "growth.json").write_text(
            json.dumps({"version": 2, "recent_events": []}), encoding="utf-8"
        )
        insp = DiaryInspiration(self.base)
        verdict = insp.should_write()
        self.assertFalse(verdict["should_write"])
        self.assertIn("素材还不够", verdict["reason"])


class DiaryWriterMultiEntryTest(unittest.TestCase):
    """一天多则的落盘形态：追加、不覆盖、兼容旧格式。

    本类测的是**结构**（小节、落款、顺序），不是叙事润色，所以关掉 L3
    润色层：既避免单测真烧 token，也让断言锁定在规则渲染的确定性文案上。
    润色自身的行为由 ``TestDiaryNarration`` 用 mock 覆盖。
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        (self.base / "growth.json").write_text(
            json.dumps({"version": 2, "intimacy": 50.0, "level": 5,
                        "total_chats": 100, "recent_events": []}),
            encoding="utf-8",
        )
        self.writer = DiaryWriter(self.base)
        self._narrate_off = mock.patch.dict(
            os.environ, {"DESKMATE_DIARY_NARRATE": "0"}
        )
        self._narrate_off.start()

    def tearDown(self):
        self._narrate_off.stop()
        self._tmp.cleanup()

    def test_append_two_entries(self):
        day = date.today().isoformat()
        self.writer.append_entry(day, [{"ts": f"{day}T09:00:00", "kind": "writing"}], [], at="09:10")
        self.writer.append_entry(day, [{"ts": f"{day}T14:00:00", "kind": "roundtable"}], [], at="14:20")
        text = self.writer.read(day)
        self.assertEqual(self.writer.entry_count(day), 2)
        self.assertIn("## 09:10", text)
        self.assertIn("## 14:20", text)
        self.assertIn("整理了文稿", text)      # 第一则在
        self.assertIn("圆桌讨论", text)        # 第二则在
        # 落款只在第一则，不重复
        self.assertEqual(text.count("累计对话"), 1)

    def test_legacy_file_counts_as_one_entry(self):
        """改造前的整篇日记没有 ## 小节：算 1 则，追加时不破坏旧内容。"""
        day = date.today().isoformat()
        legacy = f"# {day} 周三 · 小桔的日记\n\n今天我们一起做了些事，我都记在心里。\n"
        (self.base / "diary").mkdir(parents=True, exist_ok=True)
        (self.base / "diary" / f"{day}.md").write_text(legacy, encoding="utf-8")

        self.assertEqual(self.writer.entry_count(day), 1)
        self.writer.append_entry(day, [{"ts": f"{day}T20:00:00", "kind": "writing"}], [], at="20:30")
        text = self.writer.read(day)
        self.assertIn("今天我们一起做了些事", text)  # 旧正文保留
        self.assertIn("## 20:30", text)              # 新一则追加在旧正文之后
        self.assertEqual(self.writer.entry_count(day), 2)

    def test_skill_batch_merges_into_one_line(self):
        """5 项技能精进 → 合并成一句，不是 5 行同句式流水账。

        萧哥是作家，日记是「小桔第一人称」。素材再真，排成报表就没有人味了。
        """
        day = date.today().isoformat()
        events = [
            {"ts": f"{day}T0{i}:00:00", "kind": "skill_evolved", "skill_name": n}
            for i, n in enumerate(["构思", "校对", "取材", "改写", "定稿"])
        ]
        self.writer.append_entry(day, events, [], at="09:10")
        text = self.writer.read(day)
        bullet = [ln for ln in text.splitlines() if "技能" in ln]
        self.assertEqual(len(bullet), 1, f"技能类应只占一行，实际：{bullet}")
        # 只列前 3 个名字 + 总数，避免一行里挤满书名号
        self.assertIn("5 项技能", bullet[0])
        self.assertIn("「构思」", bullet[0])
        self.assertIn("「取材」", bullet[0])
        self.assertNotIn("「定稿」", bullet[0])

    def test_two_skills_stay_verbose(self):
        """少于 3 条保留逐条——那时候具体名字比一句概括更有画面。"""
        day = date.today().isoformat()
        events = [
            {"ts": f"{day}T0{i}:00:00", "kind": "skill_evolved", "skill_name": n}
            for i, n in enumerate(["构思", "校对"])
        ]
        self.writer.append_entry(day, events, [], at="09:10")
        text = self.writer.read(day)
        self.assertIn("「构思」", text)
        self.assertIn("「校对」", text)
        self.assertNotIn("一口气精进了", text)


if __name__ == "__main__":
    unittest.main()
