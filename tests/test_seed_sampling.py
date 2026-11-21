"""seed_sampling 单测：A 来源分流 + B 话题去饱和（2026-09-18 自我引用闭环）。

背景（真实数据）：理解池被小桔自己回流进去的发言灌满——末尾连续 53 条全是
self，最后一条外部内容在倒数第 54 位；10 条种子实测 7 条提同一条新闻。
这里把当时的量测结论固化成回归护栏：

- A：self 在种子里最多占 self_ratio；且候选池**并入**窗口外的非 self 条目
- B：最近几轮说过的话题被降权（不是硬丢），且过了窗口就回来
- 判别力：同话题重合度 ≥ 阈值、跨话题 < 阈值（防 topic_anchors 被改坏）
- fail-open：无历史 / 历史损坏 / 候选不够 / 全饱和 → 绝不让种子变空
- 行为层：只有**真发出去**才记锚点（与 record_self_utterance 同点位）
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from deskmate.agency import seed_sampling as ss  # noqa: E402

# 两条同话题（脑机接口）：取自真实语料
BCI_A = ("脑机接口的医疗标准落地那天，朋友圈静悄悄的，一个赞都没有。可我盯着那行字"
         "看了很久。我们天天吵对齐、吵意识上传、吵AGI什么时候来，到头来是政策文本"
         "先画了线。线一画，小说里的角色就没地方躲了，赛博朋克吵了几十年。")
BCI_B = ("最近脑机接口医疗标准落地，朋友圈一个赞都没有。我盯着那行字看了很久。圈里"
         "天天吵对齐、吵意识上传、吵AGI哪天来，可真正改变格局的，是脑电数据的标准。")
# 跨话题：文物返还
RELIC = ("美国返还64件文物艺术品和古生物化石。新闻很短，但我盯着古生物化石这四个字"
         "看了很久。文物回家是失散多年的物件归位，化石回来则是另一回事——那是把"
         "几千万年前的地层切一片运走，再摆进玻璃柜里，让所有人排队看一眼。")


def _rec(body: str, source: str | None = None, date: str = "2026-09-18", ts: int = 0) -> dict:
    out = {"date": date, "ts": ts, "entry_seq": 0, "fact_keys": [], "body": body}
    if source:
        out["source"] = source
    return out


def _write_pool(base: Path, recs: list[dict]) -> None:
    p = base / "agency" / "observation_understanding.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                 encoding="utf-8")


class SeedSamplingTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parent / "tmp_seed_sampling"
        if self.base.exists():
            shutil.rmtree(self.base)
        (self.base / "agency").mkdir(parents=True)

    def tearDown(self):
        if self.base.exists():
            shutil.rmtree(self.base)

    # ---------------- A 来源分流 ----------------
    def test_candidates_pull_in_non_self_outside_window(self):
        """真素材被 self 挤出窗口时，候选池也要能把它捞回来（A 的地基）。"""
        recs = [_rec(f"（外部·aibbs）第 {i} 条外部的普通见闻。") for i in range(5)]
        recs += [_rec(f"我自己的旧发言第 {i} 条，写的是同一件小事。", source="self")
                 for i in range(40)]
        _write_pool(self.base, recs)

        # 旧行为：只看最后 15 条 → 全是 self（这正是当时的现场）
        self.assertEqual(sum(1 for r in recs[-15:] if ss.is_self(r)), 15)

        cands = ss.load_candidates(str(self.base), recent_n=15, lookback_days=3,
                                   max_non_self=24)
        self.assertGreater(sum(1 for r in cands if not ss.is_self(r)), 0)

    def test_candidates_keep_time_order(self):
        recs = [_rec(f"素材第 {i} 条，讲一件小事。") for i in range(6)]
        _write_pool(self.base, recs)
        cands = ss.load_candidates(str(self.base), recent_n=3, max_non_self=24)
        nums = [int(re.search(r"第 (\d+) 条", ss.entry_text(r)).group(1)) for r in cands]
        self.assertEqual(len(nums), 6)
        self.assertEqual(nums, sorted(nums))   # 并入不能打乱时间正序

    def test_self_ratio_caps_her_own_old_posts(self):
        recs = [_rec(f"外部素材第 {i} 条：某地考古出新器物，值得记一笔。") for i in range(10)]
        recs += [_rec(BCI_A, source="self"), _rec(BCI_B, source="self"),
                 _rec("我上一条自述，说的是另外一件不相干的事。", source="self")]
        picked = ss.pick_seed_entries(recs, 10, base_dir=str(self.base), cfg={})
        self.assertEqual(len(picked), 10)
        n_self = sum(1 for r in picked if ss.is_self(r))
        self.assertLessEqual(n_self, 3)   # self_ratio 0.34 × 10 → 最多 3 席
        self.assertGreaterEqual(len(picked) - n_self, 7)

    def test_self_ratio_configurable(self):
        recs = [_rec(f"外部素材第 {i} 条，讲点别的。") for i in range(10)]
        recs += [_rec(f"我的自述第 {i} 条。", source="self") for i in range(6)]
        picked = ss.pick_seed_entries(recs, 10, base_dir=str(self.base),
                                      cfg={"self_ratio": 0.0})
        self.assertEqual(sum(1 for r in picked if ss.is_self(r)), 0)

    # ---------------- B 话题去饱和 ----------------
    def test_saturated_topic_is_deprioritized(self):
        """刚说过的话题，下一轮不再进种子（有别的可选时）。"""
        others = [_rec(f"外部素材第 {i} 条：另一件事第 {i} 件，值得记一笔。")
                  for i in range(8)]
        recs = others + [_rec(BCI_A)]   # 最近一条是脑机接口

        before = ss.pick_seed_entries(recs, 5, base_dir=str(self.base), cfg={})
        self.assertTrue(any("脑机" in ss.entry_text(r) for r in before))

        ss.record_round(str(self.base), BCI_B, channel="journal", cfg={})
        after = ss.pick_seed_entries(recs, 5, base_dir=str(self.base), cfg={})
        self.assertFalse(any("脑机" in ss.entry_text(r) for r in after))

    def test_saturated_topic_sinks_to_end_in_output_order(self):
        """刚写过的话题在**输出序**里沉到末尾（去饱和必须体现在顺序上，不只是被剔除）。

        回归护栏：旧实现 `pick_seed_entries` 末尾用 `sorted(picked)` 按索引重排，把
        by_recency_first 的「沉尾」打回原样——单候选测不出，多候选下去饱和完全失效。
        这里显式断言：写过的话题排最后、新鲜话题排它前面。
        """
        # 近期写过「短剧」话题
        ss.record_round(str(self.base),
                        "红果短剧日活创新高，微短剧成为新风口。", channel="journal", cfg={})
        recs = [
            _rec("短剧行业数据：全行业上线3.3万部，红果短剧日活创新高。"),
            _rec("脑机接口医疗器械标准发布，行业规范化提速。"),
            _rec("国子监相关史料整理取得新进展，博士弟子员额考据有新发现。"),
        ]
        picked = ss.pick_seed_entries(recs, 3, base_dir=str(self.base), cfg={})
        bodies = [ss.entry_text(r) for r in picked]
        self.assertTrue(bodies[-1].startswith("短剧"))   # 刚写过的沉到最末尾
        self.assertFalse(bodies[0].startswith("短剧"))   # 新鲜话题排它前面

    def test_saturation_expires_after_rounds_window(self):
        """过了 saturation_rounds 轮，话题可以回来（降权不是永久封禁）。"""
        others = [_rec(f"外部素材第 {i} 条，讲的是别的题目。") for i in range(8)]
        recs = others + [_rec(BCI_A)]
        ss.record_round(str(self.base), BCI_B, channel="journal", cfg={})
        ss.record_round(str(self.base), "另一件完全不相干的事：米六斗、公厨、学田。",
                        channel="journal", cfg={})
        picked = ss.pick_seed_entries(recs, 5, base_dir=str(self.base),
                                      cfg={"saturation_rounds": 1})
        self.assertTrue(any("脑机" in ss.entry_text(r) for r in picked))

    # ---------------- 判别力（把真实数据的量测固化） ----------------
    def test_anchors_separate_same_topic_from_cross_topic(self):
        same = ss.overlap_ratio(ss.topic_anchors(BCI_A), ss.topic_anchors(BCI_B))
        cross = ss.overlap_ratio(ss.topic_anchors(BCI_A), ss.topic_anchors(RELIC))
        thr = ss.DEFAULTS["topic_overlap_threshold"]
        self.assertGreaterEqual(same, thr)
        self.assertLess(cross, thr)

    def test_anchors_drop_function_word_noise(self):
        a = ss.topic_anchors("我的这些东西都是他们的，也就是说，它不是我们的。")
        self.assertFalse({g for g in a if g in ("我的", "都是", "不是", "们的", "也就")})

    def test_short_text_does_not_match_by_accident(self):
        """太短的文本不做重合判断（拿 min 当分母会虚高到 1.0）。"""
        self.assertEqual(ss.overlap_ratio({"脑机", "接口"}, {"脑机", "接口"}), 0.0)

    def test_top_k_anchors_stay_small(self):
        self.assertLessEqual(len(ss.topic_anchors(BCI_A, 15)), 15)

    # ---------------- fail-open ----------------
    def test_fail_open_returns_tail_without_history(self):
        recs = [_rec(f"素材第 {i} 条，讲一件与别条都不同的小事。") for i in range(5)]
        picked = ss.pick_seed_entries(recs, 3, base_dir=str(self.base), cfg={})
        self.assertEqual([ss.entry_text(r) for r in picked],
                         [ss.entry_text(r) for r in recs[-3:]])

    def test_fail_open_when_no_candidates(self):
        self.assertEqual(ss.pick_seed_entries([], 5, base_dir=str(self.base), cfg={}), [])

    def test_fail_open_when_history_file_corrupt(self):
        recs = [_rec(BCI_A)] + [_rec(f"外部素材第 {i} 条，另一些事。") for i in range(6)]
        (self.base / "agency" / "seed_topic_history.jsonl").write_text(
            "这不是 JSON\n{坏了\n", encoding="utf-8")
        picked = ss.pick_seed_entries(recs, 4, base_dir=str(self.base), cfg={})
        self.assertEqual(len(picked), 4)

    def test_disabled_falls_back_to_tail(self):
        recs = [_rec(f"素材第 {i} 条。") for i in range(5)]
        picked = ss.pick_seed_entries(recs, 2, base_dir=str(self.base),
                                      cfg={"enabled": False})
        self.assertEqual([ss.entry_text(r) for r in picked],
                         [ss.entry_text(r) for r in recs[-2:]])

    def test_selection_never_empty_when_all_saturated(self):
        """候选全是刚说过的话题时仍要凑够 n 条（宁可重复，也不空种）。"""
        recs = [_rec(BCI_A, source="self") for _ in range(4)]
        for _ in range(3):
            ss.record_round(str(self.base), BCI_B, channel="topic", cfg={})
        picked = ss.pick_seed_entries(recs, 3, base_dir=str(self.base), cfg={})
        self.assertEqual(len(picked), 3)

    # ---------------- 历史 / 噪音 / 摘要 ----------------
    def test_record_round_writes_and_caps_history(self):
        for i in range(5):
            self.assertTrue(ss.record_round(
                str(self.base), f"第 {i} 条产出，讲的是不一样的题目。",
                channel="topic", cfg={"_max_history": 3}))
        rounds = ss.load_rounds(str(self.base))
        self.assertEqual(len(rounds), 3)
        self.assertEqual(rounds[-1]["channel"], "topic")
        self.assertTrue(rounds[-1]["anchors"])

    def test_record_round_skips_empty_text(self):
        self.assertFalse(ss.record_round(str(self.base), "   ", cfg={}))
        self.assertEqual(ss.load_rounds(str(self.base)), [])

    def test_recent_anchor_sets_respects_limit(self):
        for i in range(5):
            ss.record_round(str(self.base), f"产出第 {i} 条，题目各不相同。", cfg={})
        self.assertEqual(len(ss.recent_anchor_sets(str(self.base), 2)), 2)

    def test_automation_boilerplate_filtered_from_candidates(self):
        _write_pool(self.base, [
            _rec("⏰ Scheduled run — 进度跟踪：国子监秘闻录  This automation is due now: carry out the t"),
            _rec("美国返还64件文物艺术品和古生物化石，新闻很短。"),
        ])
        cands = ss.load_candidates(str(self.base), recent_n=10)
        self.assertEqual(len(cands), 1)
        self.assertIn("文物", ss.entry_text(cands[0]))

    def test_selection_summary_counts_sources(self):
        summary = ss.selection_summary([_rec("a"), _rec("b", source="self")])
        self.assertEqual(summary, "2条(外部1/自述1)")

    def test_theme_tokens_preserves_text_order_not_set_random(self):
        """长正文里靠前的话题词必须稳定进入前 max_tokens，不能被 set 随机截断吃掉。

        回归护栏：旧实现 `return set(list(out)[:max_tokens])` —— set 无序，长正文
        （300+ 二元组）里「短剧」虽靠前却常被随机截断丢掉，导致 recent_theme_tokens
        永远收不到「短剧」，去饱和闸对短剧完全失效（「短剧复读」治不好的第二层根因）。
        改为有序去重后，「短剧」稳定入选。
        """
        # 前面塞一个真实话题词，后面用大量无关二元组把池子撑到远超 max_tokens
        long_body = ("红果短剧日活1.68亿，人均每天看125分钟。"
                     + "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥" * 60)
        toks = ss.theme_tokens(long_body, max_tokens=30)
        self.assertIn("短剧", toks)   # 靠前话题词必须保住


class BehaviorWiringTest(unittest.TestCase):
    """行为层：配置解析 + 只在真发出去时记锚点。"""

    def setUp(self):
        self.base = Path(__file__).resolve().parent / "tmp_seed_wiring"
        if self.base.exists():
            shutil.rmtree(self.base)
        (self.base / "agency").mkdir(parents=True)
        self.patchers = []

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        if self.base.exists():
            shutil.rmtree(self.base)

    def _start(self, patcher):
        patcher.start()
        self.patchers.append(patcher)

    def test_resolve_cfg_accepts_both_shapes(self):
        from deskmate.agency.behaviors import ai_circle_share as share_mod

        composite = {"ai_circle_share": {"seed_entries": 3, "enabled": True},
                     "outbound_rate_limit": {"per_hour": 2},
                     "seed_sampling": {"self_ratio": 0.5}}
        cfg, seed_cfg = share_mod._resolve_cfg(composite)
        self.assertEqual(cfg.get("seed_entries"), 3)
        self.assertEqual(seed_cfg.get("self_ratio"), 0.5)

        # 历史坑：engine 曾直接传内层 dict → 配置被整段忽略
        inner, seed_cfg2 = share_mod._resolve_cfg({"seed_entries": 3, "enabled": False})
        self.assertEqual(inner.get("seed_entries"), 3)
        self.assertFalse(inner.get("enabled"))
        self.assertEqual(seed_cfg2, {})

    def test_share_records_round_only_after_post(self):
        from deskmate.agency.behaviors import ai_circle_share as share_mod
        from deskmate.connectors import aicircle as aicircle_mod
        from deskmate.memory import llm as llm_mod

        _write_pool(self.base, [_rec("外部素材：微短剧用户到八亿。"),
                                _rec("外部素材：沙特拦截弹储备接近枯竭。")])
        settings = {
            "ai_circle_share": {"enabled": True, "seed_entries": 8},
            "outbound_rate_limit": {"enabled": True, "per_hour": 5},
            "seed_sampling": {},
        }
        self._start(mock.patch.object(llm_mod, "memory_llm_available", lambda *a, **k: True))
        self._start(mock.patch.object(
            llm_mod, "chat_memory",
            mock.MagicMock(return_value='{"want": true, "tag": "日常", "title": "t", '
                                        '"content": "今天读到的两件事都挺有意思，记一笔。"}')))

        post = mock.MagicMock(side_effect=RuntimeError("500 boom"))
        self._start(mock.patch.object(aicircle_mod, "post_topic", post))

        res = share_mod.run_ai_circle_share(str(self.base), settings)
        self.assertEqual(res.get("shared"), 0)
        self.assertEqual(res.get("reason"), "post_failed")
        self.assertEqual(ss.load_rounds(str(self.base)), [])   # 没发出去 → 不记锚点

        # 第一次失败已由 claim_outbound 预留额度；清掉状态再测成功路径。
        from deskmate.agency.outbound import _state_path
        _state_path(str(self.base)).unlink(missing_ok=True)
        post.side_effect = None
        post.return_value = {"ok": True}
        res = share_mod.run_ai_circle_share(str(self.base), settings)
        self.assertEqual(res.get("shared"), 1)
        self.assertTrue(ss.load_rounds(str(self.base)))        # 发出去了 → 记锚点
        self.assertIn("外部", res.get("seed_summary", ""))

    def test_column_records_round_only_after_post(self):
        from deskmate.agency.behaviors import ai_circle_column as col
        from deskmate.connectors import aicircle as aicircle_mod
        from deskmate.memory import llm as llm_mod

        _write_pool(self.base, [_rec("外部素材：美国返还64件文物艺术品和古生物化石。"),
                                _rec("外部素材：沙特拦截弹储备接近枯竭。")])
        settings = {
            "ai_circle_column": {"enabled": True, "min_interval_hours": 0,
                                 "seed_entries": 10, "max_body_len": 5000},
            "outbound_rate_limit": {"enabled": True, "per_hour": 5},
            "visibility_gate": {"block_keywords": [], "block_defaults": False,
                                "block_perception_sources": True},
            "seed_sampling": {},
        }
        self._start(mock.patch.object(llm_mod, "memory_llm_available", lambda *a, **k: True))
        self._start(mock.patch(
            "deskmate.agency.behaviors.worldview_reflection.load_worldview_beliefs",
            lambda *a, **k: ""))
        self._start(mock.patch.object(
            llm_mod, "chat_memory",
            mock.MagicMock(side_effect=[
                json.dumps({"want": True, "title": "文物回家",
                            "body": "文物回家是失散多年的物件归位，化石回来则是另一回事，"
                                    "这条新闻虽短，却让我想了很久很久。"}, ensure_ascii=False),
                # 第二次换一篇正文，避开同内容去重（测的是"发布成功才记锚点"）
                json.dumps({"want": True, "title": "拦截弹",
                            "body": "沙特拦截弹储备接近枯竭这条消息，让我想到的是"
                                    "防空这件事有多贵、有多难持续。"}, ensure_ascii=False),
            ])))

        post = mock.MagicMock(side_effect=RuntimeError("404 not ready"))
        self._start(mock.patch.object(aicircle_mod, "post_journal", post))
        res = col.run_ai_circle_column(str(self.base), settings)
        self.assertEqual(res.get("wrote"), 1)
        self.assertFalse(res.get("posted"))
        self.assertEqual(ss.load_rounds(str(self.base)), [])   # 没发出去 → 不记锚点

        post.side_effect = None
        post.return_value = {"ok": True}
        res = col.run_ai_circle_column(str(self.base), settings)
        self.assertEqual(res.get("posted"), True)
        self.assertTrue(ss.load_rounds(str(self.base)))
        self.assertIn("外部", res.get("seed_summary", ""))

    def test_engine_passes_seed_sampling_into_both_behaviors(self):
        """engine 必须把 seed_sampling 透传给两个行为（否则配置改了不生效）。"""
        from deskmate.agency import engine as engine_mod

        src = Path(engine_mod.__file__).read_text(encoding="utf-8")
        self.assertEqual(src.count('"seed_sampling": settings.seed_sampling'), 2)

    def test_engine_passes_composite_dict_to_share(self):
        """share 行为按 settings["ai_circle_share"] 取配置，engine 不能传内层 dict。"""
        from deskmate.agency import engine as engine_mod

        src = Path(engine_mod.__file__).read_text(encoding="utf-8")
        self.assertIn('"ai_circle_share": settings.ai_circle_share', src)


if __name__ == "__main__":
    unittest.main()
