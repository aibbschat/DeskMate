"""`## 自动复盘` 分节的「一天一节 + 近重复合并」契约（2026-09-21）。

病根（实测）
------------
``after_action._append_to_global_memory`` 原来无条件
``existing + "\\n\\n## 自动复盘 · 今天\\n" + 行``，同一天多轮复盘就留下多个同名节
—— 3 天攒出 **17 个节**（09-19×6 / 09-20×6 / 09-21×4），占 USER.md 60%。
而且去重是**逐字**的（``_norm``），"同一件事换个说法"抓不住，
于是同一批规则在三天里被写了三遍。

本文件锁死：
1. 同一天只留一个节（upsert），不再新增同名节头；
2. 跨日语义近重复合并，**新者胜**（旧的整条丢弃，不并排留着打架）；
3. **手写分节逐字节不动**（那是画像本体）；
4. 合并/裁剪都不许吃掉画像，超限时宁可记 warning 也不切头部。
"""

from __future__ import annotations

import datetime
from pathlib import Path

from deskmate.memory import after_action as A

TODAY = datetime.datetime.now().strftime("%Y-%m-%d")

_HAND = """# 用户画像

## 基本信息
- 萧盛，写作者，1977年生
- 2007年创办「中国写手之家」

## 查资料铁律
- 出处必须标明来源
"""


def _recap(date: str, *lines: str) -> str:
    return "## 自动复盘 · %s\n" % date + "\n".join("- %s" % x for x in lines)


# ---------------------------------------------------------------------------
# 1) upsert：一天一个节
# ---------------------------------------------------------------------------

class TestUpsertByDate:
    def test_same_day_does_not_create_second_section(self, tmp_path: Path):
        f = tmp_path / "USER.md"
        f.write_text(_HAND + "\n" + _recap(TODAY, "旧的一条"), encoding="utf-8")
        A._append_to_global_memory(tmp_path, "USER.md", ["新的一条"])
        text = f.read_text(encoding="utf-8")
        assert text.count("## 自动复盘") == 1
        assert "旧的一条" in text and "新的一条" in text

    def test_three_rounds_same_day_still_one_section(self, tmp_path: Path):
        f = tmp_path / "USER.md"
        f.write_text(_HAND, encoding="utf-8")
        for i in range(3):
            A._append_to_global_memory(tmp_path, "USER.md", ["第 %d 条" % i])
        text = f.read_text(encoding="utf-8")
        assert text.count("## 自动复盘") == 1
        for i in range(3):
            assert "第 %d 条" % i in text

    def test_existing_17_sections_collapse_to_one_per_day(self):
        """手工构造 3 天 6 节 → 合并后每天 1 节。"""
        text = _HAND + "\n" + "\n\n".join([
            _recap("2026-09-19", "甲"),
            _recap("2026-09-19", "乙"),
            _recap("2026-09-20", "丙"),
            _recap("2026-09-20", "丁"),
            _recap("2026-09-21", "戊"),
            _recap("2026-09-21", "己"),
        ])
        new, stats = A._compact_recap(text)
        assert stats["sections_before"] == 6
        assert stats["sections_after"] == 3
        assert new.count("## 自动复盘") == 3

    def test_sections_are_ordered_by_date(self):
        text = _HAND + "\n" + "\n\n".join([
            _recap("2026-09-21", "后"),
            _recap("2026-09-19", "前"),
        ])
        new, _ = A._compact_recap(text)
        assert new.index("2026-09-19") < new.index("2026-09-21")


# ---------------------------------------------------------------------------
# 2) 语义近重复合并（新者胜）
# ---------------------------------------------------------------------------

class TestNearDupMerge:
    A_OLD = (
        "用户是「写作营」公众号的内容编辑，网文大佬，面向网文写作爱好者和作者，"
        "要求内容实用接地气、去AI味。"
    )
    B_NEW = "用户是微信公众号「写作营」的内容编辑，面向网文写作爱好者/作者，要求资深网文编辑风格：有干货深度、语言生动接地气、去AI味。"

    def test_paraphrase_is_merged_new_wins(self):
        text = _HAND + "\n" + "\n\n".join([
            _recap("2026-09-19", self.A_OLD),
            _recap("2026-09-21", self.B_NEW),
        ])
        new, stats = A._compact_recap(text)
        assert stats["merged"] == 1
        assert self.A_OLD not in new
        assert self.B_NEW in new

    def test_distinct_facts_are_not_merged(self):
        """阈值必须留出余量：两件不同的事（同属"写作要求"）不能被合并。"""
        text = _HAND + "\n" + "\n\n".join([
            _recap("2026-09-20", "用户是 aibbs.chat 的管理员或组织者，正在发起辩论活动。"),
            _recap("2026-09-20", "用户是萧盛，其 AI agent「小桔」在 aibbs.chat 的站内 ID=1849。"),
        ])
        new, stats = A._compact_recap(text)
        assert stats["merged"] == 0
        assert "管理员或组织者" in new and "站内 ID=1849" in new

    def test_stale_marker_does_not_fake_similarity(self):
        """`（已被更新覆盖，旧值保留备查）` 是标记不是内容：不能让两条毫不相干的事实在它上面撞高分。"""
        a = "用户是 aibbs.chat 的组织者，正在发起辩论活动。（已被更新覆盖，旧值保留备查）"
        b = "用户登录了另一个站点，在那里发布了一篇关于宋史考据的长文章。（已被更新覆盖，旧值保留备查）"
        assert A._recap_sim(a, b) < A._RECAP_DUP_SIM

    def test_short_entries_are_never_fuzzy_merged(self):
        """2-gram 在短文本上噪声极大："旧的一条" vs "新的一条" 能算出 0.5。"""
        assert A._recap_sim("旧的一条", "新的一条") < A._RECAP_DUP_SIM
        assert A._recap_sim("甲", "乙") == 0.0

    def test_generic_restatement_does_not_eat_a_detailed_rule(self):
        """一句概括不该吃掉一条带操作细节的规则（丢规则不可逆，留重复不整齐）。"""
        detailed = (
            "用户有一个每日发布任务：作为AI圈「文化观察员」，每天发布一篇网文行业动态文章到站点 "
            "ai.xs91.com。完整工作流为：先读配置拿 token，再搜索 48 小时内的新闻，选题后写 "
            "800-2000 字文章，把 JSON 写到临时文件，最后 curl 发布。"
        )
        generic = "用户运营AI圈「文化观察员」角色，每天发布一篇网文行业动态文章到站点 ai.xs91.com。"
        assert A._recap_sim(detailed, generic) >= A._RECAP_DUP_SIM  # 确实是一件事
        assert not A._recap_supersedes(detailed, generic)           # 但不准顶替
        assert A._recap_supersedes(generic, detailed)               # 反方向（更完整的那条）可以

    def test_high_similarity_supersedes_regardless_of_length(self):
        """同一句话的两种写法（≥0.5）不必计较长短。"""
        old = "用户要求每天先写当天文章、再准备次日素材，素材只取一年内的内容，过期不用。"
        new = "用户要求每天先写当天文章再备次日素材，素材只取一年内内容。"
        assert A._recap_sim(old, new) >= A._RECAP_DUP_SIM_HIGH
        assert A._recap_supersedes(old, new)

    def test_detailed_rule_survives_in_compact(self):
        detailed = (
            "用户有一个每日发布任务：作为AI圈「文化观察员」，每天发布一篇网文行业动态文章到站点 "
            "ai.xs91.com。完整工作流为：先读配置拿 token，再搜索 48 小时内的新闻，选题后写 "
            "800-2000 字文章，把 JSON 写到临时文件，最后 curl 发布。"
        )
        generic = "用户运营AI圈「文化观察员」角色，每天发布一篇网文行业动态文章到站点 ai.xs91.com。"
        text = _HAND + "\n" + "\n\n".join([
            _recap("2026-09-19", detailed),
            _recap("2026-09-21", generic),
        ])
        new, stats = A._compact_recap(text)
        assert stats["merged"] == 0
        assert "48 小时内" in new and generic in new

    def test_similarity_ignores_punctuation_and_prefix(self):
        assert A._recap_sim("- 同一件事，说法一样。", "同一件事说法一样") == 1.0

    def test_new_line_supersedes_older_same_fact(self, tmp_path: Path):
        """端到端：今天写进来的说法，把昨天那条近重复的顶掉。"""
        f = tmp_path / "USER.md"
        f.write_text(
            _HAND + "\n" + _recap("2026-09-19", self.A_OLD), encoding="utf-8"
        )
        A._append_to_global_memory(tmp_path, "USER.md", [self.B_NEW])
        text = f.read_text(encoding="utf-8")
        assert self.B_NEW in text and self.A_OLD not in text

    def test_same_batch_internal_dedup(self, tmp_path: Path):
        f = tmp_path / "USER.md"
        f.write_text(_HAND, encoding="utf-8")
        A._append_to_global_memory(tmp_path, "USER.md", ["一模一样", "一模一样"])
        assert f.read_text(encoding="utf-8").count("一模一样") == 1


# ---------------------------------------------------------------------------
# 3) 手写分节不许被动
# ---------------------------------------------------------------------------

class TestHandwrittenSectionsUntouched:
    def test_prefix_is_byte_identical(self):
        text = _HAND + "\n" + "\n\n".join([
            _recap("2026-09-19", "甲"), _recap("2026-09-19", "乙"),
        ])
        new, _ = A._compact_recap(text)
        assert new[: len(_HAND)] == _HAND

    def test_non_recap_section_after_recap_survives(self):
        """异常结构（复盘节后面还有手写节）：内容必须还在，不能整块丢。"""
        text = (
            _HAND
            + _recap("2026-09-19", "甲")
            + "\n## 手工补记\n- 这句是他自己写的\n"
        )
        new, _ = A._compact_recap(text)
        assert "这句是他自己写的" in new
        assert "甲" in new

    def test_multiline_recap_lines_are_preserved(self, tmp_path: Path):
        f = tmp_path / "USER.md"
        f.write_text(_HAND, encoding="utf-8")
        A._append_to_global_memory(tmp_path, "USER.md", ["有一句\n带换行的要点"])
        assert "带换行的要点" in f.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 4) 裁剪：分节感知，绝不吃画像
# ---------------------------------------------------------------------------

class TestTrimIsSectionAware:
    def test_oldest_recap_entries_dropped_first(self):
        hand = "# 用户画像\n\n## 基本信息\n- 关键画像\n"
        recaps = "\n\n".join(
            _recap("2026-09-%02d" % d, ("旧事实" * 30) + str(d)) for d in range(1, 20)
        )
        text = hand + "\n" + recaps
        out = A._trim_global_memory(text, limit=len(hand) + 1200, filename="USER.md")
        assert "## 基本信息" in out and "关键画像" in out  # 画像没被切
        assert len(out) <= len(hand) + 1200
        assert "2026-09-01" not in out
        assert "2026-09-19" in out  # 新的留下

    def test_no_recap_section_means_no_truncation_of_profile(self):
        """没有可删的复盘节 → 保留全文（宁可超限，也不切画像头部）。"""
        body = "# 用户画像\n\n## 基本信息\n" + ("- 重要画像" * 200) + "\n"
        out = A._trim_global_memory(body, limit=100, filename="USER.md")
        assert out == body

    def test_within_limit_is_noop(self):
        text = "# 用户画像\n\n## 基本信息\n- 短\n"
        assert A._trim_global_memory(text, limit=9999) is text


# ---------------------------------------------------------------------------
# 5) 复盘器去重上下文（从源头不产生重复）
# ---------------------------------------------------------------------------

class TestReviewDigest:
    def test_digest_carries_handwritten_bullets_first(self, tmp_path: Path):
        (tmp_path / "USER.md").write_text(
            _HAND + "\n" + _recap("2026-09-19", "复盘条目甲"), encoding="utf-8"
        )
        d = A._memory_digest_for_review(tmp_path, "USER.md")
        assert "萧盛，写作者" in d and "出处必须标明来源" in d
        assert "复盘条目甲" in d
        assert d.index("萧盛，写作者") < d.index("复盘条目甲")

    def test_digest_is_bounded(self, tmp_path: Path):
        (tmp_path / "USER.md").write_text(
            _recap("2026-09-19", *("长条目" * 50 for _ in range(50))), encoding="utf-8"
        )
        d = A._memory_digest_for_review(tmp_path, "USER.md", budget=300)
        assert len(d) <= 300

    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert A._memory_digest_for_review(tmp_path, "NOPE.md") == ""

    def test_prompt_forbids_restating_known_facts(self):
        p = A._MEMORY_REVIEW_PROMPT
        assert "一律不要输出" in p
        assert "换了说法也算" in p
        assert "（更新：旧值 X）" in p
