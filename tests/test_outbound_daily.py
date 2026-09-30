"""outbound 护栏单测：每日上限(per_day) + 最小间隔(min_interval) + 持久化（重启不丢计数）。

不依赖 LLM / 网络，直接操作 outbound 状态文件（base/agency/outbound_state.jsonl）。
验证 A 方案核心：广场短帖 share 每日上限 + 最小间隔，且状态落盘 → app 重启后当天
已发条数仍计入，根治「启动即把日额度刷光」。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(REPO))

from deskmate.agency import outbound as ob  # noqa: E402


class OutboundDailyTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="tmp_ob_daily_"))

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _rewrite_last_ts(self, secs_ago):
        """把状态文件最后一条记录的 ts 改到 secs_ago 秒前（模拟昨天/更早）。"""
        p = ob._state_path(self.base)
        lines = p.read_text(encoding="utf-8").splitlines()
        rec = json.loads(lines[-1])
        rec["ts"] = time.time() - secs_ago
        lines[-1] = json.dumps(rec, ensure_ascii=False)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_daily_ok_counts_today_only(self):
        # 1 条 share → per_day=2 内，可再发
        ob.record_outbound(str(self.base), "share", content="a")
        self.assertTrue(ob.outbound_daily_ok(str(self.base), ("share",), 2))
        # 第 2 条 share → 达每日上限，不可再发
        ob.record_outbound(str(self.base), "share", content="b")
        self.assertFalse(ob.outbound_daily_ok(str(self.base), ("share",), 2))
        # reply 不占 share 的每日额度（即使发了 reply，share 仍满）
        ob.record_outbound(str(self.base), "reply", content="r")
        self.assertFalse(ob.outbound_daily_ok(str(self.base), ("share",), 2))

    def test_daily_ignores_other_days(self):
        ob.record_outbound(str(self.base), "share", content="old")
        self._rewrite_last_ts(86400 * 2)  # 改成两天前
        # 昨/前天的记录不计入今天 → 今天还能发满 per_day
        self.assertTrue(ob.outbound_daily_ok(str(self.base), ("share",), 2))

    def test_interval_ok(self):
        ob.record_outbound(str(self.base), "share", content="x")
        # 刚发过，6 小时间隔内 → 不可再发
        self.assertFalse(ob.outbound_interval_ok(str(self.base), "share", 6))
        # journal 不受 share 间隔约束
        self.assertTrue(ob.outbound_interval_ok(str(self.base), "journal", 6))

    def test_claim_daily_blocks_share_only(self):
        ob.record_outbound(str(self.base), "share", content="a")
        ob.record_outbound(str(self.base), "share", content="b")
        rate = {"enabled": True, "per_hour": 5, "per_day": 2}
        ok, reason = ob.claim_outbound(str(self.base), "share", rate_cfg=rate,
                                       content="c", max_per_day=2)
        self.assertFalse(ok)
        self.assertEqual(reason, "daily")
        # reply 走独立通道，不受 share per_day 约束
        ok2, _ = ob.claim_outbound(str(self.base), "reply", rate_cfg=rate, content="r")
        self.assertTrue(ok2)

    def test_claim_interval_blocks_share(self):
        ob.record_outbound(str(self.base), "share", content="a")
        rate = {"enabled": True, "per_hour": 5}
        ok, reason = ob.claim_outbound(str(self.base), "share", rate_cfg=rate,
                                       content="b", min_interval_hours=6)
        self.assertFalse(ok)
        self.assertEqual(reason, "interval")

    def test_persistence_across_reopen(self):
        """模拟「重启」：记录落盘，再次读取（不共享内存）仍计入每日上限。"""
        rate = {"enabled": True, "per_hour": 5, "per_day": 1}
        ok1, _ = ob.claim_outbound(str(self.base), "share", rate_cfg=rate,
                                   content="a", max_per_day=1)
        self.assertTrue(ok1)
        # 不清理、不共享内存，直接再 claim（_read 每次读盘 → 模拟重启后）
        ok2, reason = ob.claim_outbound(str(self.base), "share", rate_cfg=rate,
                                        content="b", max_per_day=1)
        self.assertFalse(ok2)
        self.assertEqual(reason, "daily")


if __name__ == "__main__":
    unittest.main()
