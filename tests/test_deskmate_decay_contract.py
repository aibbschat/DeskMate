"""记忆衰减的「条目格式契约」回归测试。

背景（2026-09-21 实测）：真实 MEMORY.md 的条目写作 ``- 2026-08-13: 内容``（带列表
前缀），而 decay_memory 的日期正则只认「紧贴行首」的日期 → 匹配失败 → 走
「无日期，安全起见保留」分支 → **衰减永远不触发**，``traces/forgot.jsonl`` 永不生成。

既有测试全部使用「裸日期 + 空行分隔」格式（恰好匹配旧正则），所以这个 bug 长期
藏在覆盖盲区里：测试全绿，线上空转。本文件专门锁住格式契约，防止再次漂移。
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from deskmate.memory.manager import FORGOT_TRACE_FILE, DeskMateMemoryManager


def _days_ago(n: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


def _write(tmp_path: Path, mem: str) -> None:
    (tmp_path / "MEMORY.md").write_text(mem, encoding="utf-8")


@pytest.fixture
def mgr(tmp_path: Path) -> DeskMateMemoryManager:
    (tmp_path / "memory").mkdir(exist_ok=True)
    (tmp_path / "projects").mkdir(exist_ok=True)
    return DeskMateMemoryManager(tmp_path)


class TestDecayFormatContract:
    """衰减必须认得真实落盘格式。"""

    def test_dash_prefixed_entries_are_decayed(self, mgr, tmp_path):
        """带 "- " 列表前缀的超龄条目必须被衰减（本次修复的核心）。"""
        _write(tmp_path,
            "§ 工作 #work\n"
            f"- {_days_ago(5)}: 仍然有效的工作条目\n"
            f"- {_days_ago(90)}: 早已过时的工作条目\n"
        )
        res = mgr.decay_memory()
        assert res["archived"] == 1, "带 - 前缀的超龄条目必须被归档"
        content = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        assert "仍然有效的工作条目" in content, "未超龄条目不得被误删"
        assert "早已过时的工作条目" not in content

    def test_all_prefix_and_colon_forms_recognized(self, mgr, tmp_path):
        """短横线 / 星号 / 裸日期 / 全角冒号 四种写法都要认。"""
        _write(tmp_path,
            "§ 工作 #work\n"
            f"- {_days_ago(90)}: 前缀短横线\n"
            f"* {_days_ago(91)}: 前缀星号\n"
            f"{_days_ago(92)}: 裸日期\n"
            f"- {_days_ago(93)}：全角冒号\n"
        )
        assert mgr.decay_memory()["archived"] == 4

    def test_entries_judged_individually_not_as_block(self, mgr, tmp_path):
        """连续多行必须逐条判定：不能因首条未超龄就把整块留下。"""
        _write(tmp_path,
            "§ 工作 #work\n"
            f"- {_days_ago(3)}: 新条目\n"
            f"- {_days_ago(100)}: 老条目\n"
            f"- {_days_ago(4)}: 另一条新的\n"
        )
        assert mgr.decay_memory()["archived"] == 1
        content = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        assert "新条目" in content and "另一条新的" in content
        assert "老条目" not in content

    def test_note_ttl_deletes_but_forgot_trace_records(self, mgr, tmp_path):
        """#note 超 7 天删除，且写遗忘 trace。"""
        _write(tmp_path, f"§ 笔记 #note\n- {_days_ago(20)}: 过期临时备忘\n")
        res = mgr.decay_memory()
        assert res["deleted"] == 1
        assert res["forgot_count"] == 1
        assert "过期临时备忘" in (tmp_path / "traces" / FORGOT_TRACE_FILE).read_text(encoding="utf-8")

    def test_no_ttl_section_untouched(self, mgr, tmp_path):
        """preference / correction 永不衰减。"""
        _write(tmp_path,
            "§ 纠正 #correction\n"
            f"- {_days_ago(500)}: 很久以前的纠正，必须保留\n"
        )
        assert mgr.decay_memory()["decayed"] == 0
        assert "很久以前的纠正" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")

    def test_undated_entries_preserved(self, mgr, tmp_path):
        """无日期条目保留（安全起见，宁留不误删）。"""
        _write(tmp_path, "§ 工作 #work\n- 一条没有日期的条目\n")
        assert mgr.decay_memory()["decayed"] == 0
        assert "一条没有日期的条目" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")

    def test_sibling_section_not_reformatted(self, mgr, tmp_path):
        """未发生衰减的分节不应被重排（防格式漂移）。"""
        _write(tmp_path,
            "§ 偏好 #preference\n"
            f"{_days_ago(10)}: 偏好甲\n\n{_days_ago(11)}: 偏好乙\n\n"
            "§ 工作 #work\n"
            f"- {_days_ago(120)}: 超龄条目\n"
        )
        mgr.decay_memory()
        content = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        assert "偏好甲\n\n" in content, "偏好分节应保持原有的空行分隔"


class TestDatePrefixWriteSide:
    """写入端不得与读取端的正则打架。"""

    def test_dash_prefixed_entry_not_double_dated(self, mgr):
        """已带日期（含 - 前缀）的条目不得被再次加日期。"""
        entry = f"- {_days_ago(10)}: 原有条目"
        assert mgr._ensure_date_prefix(entry) == entry

    def test_bare_dated_entry_untouched(self, mgr):
        entry = f"{_days_ago(10)}: 原有条目"
        assert mgr._ensure_date_prefix(entry) == entry

    def test_undated_entry_gets_today_prefix(self, mgr):
        out = mgr._ensure_date_prefix("一条新记忆")
        assert out.startswith(datetime.date.today().isoformat())
        assert out.endswith("一条新记忆")


class TestCompressObservability:
    """超限却压不下去时必须留痕（此前三条路径全是静默 return False）。"""

    def test_stall_recorded_when_no_llm(self, mgr, tmp_path, monkeypatch):
        import deskmate.memory.manager as M

        monkeypatch.setattr(M, "llm_available", lambda: False)
        oversized = "§ 工作 #work\n" + "\n".join(
            f"- {_days_ago(1)}: {'填充内容' * 40}" for _ in range(150)
        )
        _write(tmp_path, oversized)
        assert mgr._compress_if_needed("memory") is False
        assert "memory" in getattr(mgr, "_compress_stall_state", {}), \
            "超限未压缩必须留下可观测状态"

    def test_no_stall_when_under_limit(self, mgr, tmp_path, monkeypatch):
        import deskmate.memory.manager as M

        monkeypatch.setattr(M, "llm_available", lambda: False)
        _write(tmp_path, "§ 工作 #work\n- 短内容\n")
        assert mgr._compress_if_needed("memory") is False
        assert not getattr(mgr, "_compress_stall_state", {})


class TestStableLongTermSection:
    """「长期事实 #stable」：给长效条目一个**永不衰减**的显式归属。

    背景（2026-09-21 实测）：``MEMORY_DECAY_TTL`` 只把 ``work`` 定为 30 天，而真实
    的「§ 工作」分节里堆着运维配置 / 协作模式 / 项目位置 / 硬规则 / 创作设定——
    全都在 30 天后被归档或删除。当天 dry-run 显示 **28 条长效条目即将被 TTL 误杀**，
    其中包含「串书禁令（最高优先级）」这类丢失即事故的红线。

    归属为何是全局层而非项目记忆：项目记忆的注入依赖 ``active_project``，而
    应用层从未调用 ``set_active_project``（只有 MCP memory server 能手动设），
    迁到 ``projects/<名>/`` 等于永久不可达。
    """

    def test_stable_section_never_decays(self, mgr, tmp_path):
        _write(tmp_path,
            "§ 长期事实 #stable #sensitive\n"
            f"- {_days_ago(500)}: 串书禁令：不得把读书书目内容写进 .deskmate/\n"
            f"- {_days_ago(400)}: 托管站点配置 /Users/xiaocheng/OpenWorker/d8d7c4ac-b7d/\n"
        )
        assert mgr.decay_memory()["decayed"] == 0
        content = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        assert "串书禁令" in content, "红线条目绝不能被 TTL 吃掉"
        assert "托管站点配置" in content

    def test_stable_not_registered_in_ttl(self):
        """#stable 一旦被登记进 TTL 表，长期事实又会开始被悄悄淘汰——锁死。"""
        import deskmate.memory.manager as M

        assert "stable" not in M.MEMORY_DECAY_TTL
        assert "长期事实" not in M.MEMORY_DECAY_TTL
        assert M._TITLE_TO_TAG["长期事实"] == "stable"

    def test_section_header_is_canonical(self, mgr):
        """标题与标签两种写法必须渲染成同一规范头（自动归并的前提）。"""
        assert mgr._section_header("长期事实") == "§ 长期事实 #stable"
        assert mgr._section_header("stable") == "§ 长期事实 #stable"

    def test_clean_global_memory_keeps_stable(self, mgr, tmp_path):
        """分层净化不得把长期事实迁进项目记忆（迁出即不可达）。"""
        _write(tmp_path,
            "§ 长期事实 #stable\n"
            "- 《国子监秘闻录》人物圣经已合并，含姜阿大、陈哥儿\n"
        )
        res = mgr.clean_global_memory()
        assert res["entries_migrated"] == 0
        assert "人物圣经已合并" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")

    def test_append_routes_into_stable_section(self, mgr, tmp_path, monkeypatch):
        """写入端用 target_section='长期事实' 应落进 #stable 分节。"""
        import deskmate.memory.manager as M

        monkeypatch.setattr(M, "llm_available", lambda: False)
        _write(tmp_path, "§ 工作 #work\n- 2026-09-01: 一条工作进展\n")
        mgr.append_to_section("memory", "托管站点 ai.xs91.com 的发布配置", target_section="长期事实")
        content = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        assert "§ 长期事实 #stable" in content
        assert "托管站点" in content


def _sections(path: Path) -> dict[str, dict]:
    """把落盘的 MEMORY.md 解析成 {title: section}（按真实文件读，不信任内存态）。"""
    return {
        s["title"]: s
        for s in DeskMateMemoryManager(path)._parse_sections(
            (path / "MEMORY.md").read_text(encoding="utf-8")
        )[1]
    }


class TestCreationSectionRouting:
    """书稿记忆自动落 `§ 创作 #stable`：**换一本新书不再复发**。

    背景（2026-09-26 实证）：决定记忆会不会过期的是分节标签（`MEMORY_DECAY_TTL`），
    而自动路由能产出的标签只有 `preference/correction/work/note` 四个——`stable`
    是**自动路径到不了的孤岛**：

    - LLM 正常 → 分类器提示词写着 `work（项目/工作进展）` → 书稿进度必然判成
      `#work` → 30 天后归档；
    - LLM 不可用 / 超时 / 返回跑偏 → 关键词兜底落空 → 默认「笔记」`#note`
      → **7 天后直接删除且不落 vault**。

    更关键的是：§工作 里那 39 条书稿记忆**是小桔自己写的 `#work`**
    （「#work 国子监秘闻录进度 09-25…」）——她照工具说明办事。所以修复必须
    同时处理「内容判据」和「显式标签让位」两件事，否则显式标签会把每条内容
    提前带走，等于白改。

    ⚠ 已实测否掉「按书名一册一节」：真实语料里 39 条有 33 条压根没写书名，
    按《》出现次数取书名还会取到章节名（屯田策/忘川帖）。故退成单一 `§ 创作`，
    先把「不丢、不衰减」坐实，精确到哪本书留给丙。
    """

    def test_creation_note_gets_stable_section_and_never_decays(
        self, mgr, tmp_path, monkeypatch
    ):
        """书稿记忆 → §创作 #stable，且 500 天前的条目也不衰减。"""
        import deskmate.memory.manager as M

        monkeypatch.setattr(M, "llm_available", lambda: False)
        _write(tmp_path, f"§ 工作 #work\n- {_days_ago(1)}: 一条无关的工作进展\n")
        mgr.append_to_section(
            "memory", f"{_days_ago(500)}: 第五章《忘川帖》推进至第四节，武成川死于医官局"
        )

        sec = _sections(tmp_path)
        assert "创作" in sec, "书稿记忆必须落 §创作"
        assert "stable" in sec["创作"]["tags"], (
            "必须挂 #stable；不挂就会被 30 天 TTL 吃掉，等于没修"
        )
        assert "医官局" in sec["创作"]["body"]
        assert mgr.decay_memory()["decayed"] == 0, "500 天前的书稿记忆也不得被衰减"

    def test_explicit_work_tag_yields_to_creation(self, mgr):
        """⚠ 本次修复的核心回归：小桔亲手写的 `#work` 不得把书稿内容带走。

        真实的失败形态就长这样：`#work 国子监秘闻录进度 09-25（…）：第五章《忘川帖》
        正式完结`。若显式标签仍绝对优先，规则再好也救不了——每一条都会被
        30 天 TTL 收走。
        """
        entry = "#work 国子监秘闻录进度 09-25：第五章《忘川帖》正式完结"
        assert mgr._route_memory_section(entry) == "创作"

    def test_preference_and_correction_still_win(self, mgr):
        """「关于萧哥」的事实与用户明示的长期事实仍绝对优先，不被创作规则抢走。"""
        assert mgr._route_memory_section("#correction 用户纠正我：第五章笔误要逐条核对") == "correction"
        assert mgr._route_memory_section("#preference 用户喜欢在章节定稿后先睡一觉") == "preference"
        assert mgr._route_memory_section("#stable 人物设定核对规则") == "stable"

    def test_operation_notes_not_swept_into_creation(self, mgr, tmp_path, monkeypatch):
        """反例①：AI圈/写作营的运营流水账是真·一时工作进展，不得被创作规则收编。

        否则「运营流水账永久占据常驻记忆」——修一个坑挖另一个坑。
        """
        import deskmate.memory.manager as M

        monkeypatch.setattr(M, "llm_available", lambda: False)
        _write(tmp_path, f"§ 工作 #work\n- {_days_ago(1)}: 一条无关的工作进展\n")
        ops = [
            "#work 敲钟人踩坑：bell_submit 也占用每日 3 条广场发帖额度",
            "擂台 #2 辩题「唱反调是『另一面』还是『反张嘴』？」继续运行",
            "小桔定稿写作营09-25文章《第一章顺顺当当，第二章直接卡死？八成是视角没选对》",
        ]
        for e in ops:
            assert mgr._route_memory_section(e) != "创作", f"运营内容被误收编：{e[:40]}"
            mgr.append_to_section("memory", e)
        assert "创作" not in _sections(tmp_path)

    def test_reading_a_book_is_not_writing_a_book(self, mgr, tmp_path, monkeypatch):
        """反例②：读别人的书（读字贴着书名号）不得归进创作分节。

        串书禁令是最高优先级红线；把「读完《兰陵王》第18章」判成创作内容，
        等于给串书开一条自动通道。
        """
        import deskmate.memory.manager as M

        monkeypatch.setattr(M, "llm_available", lambda: False)
        _write(tmp_path, f"§ 工作 #work\n- {_days_ago(1)}: 一条无关的工作进展\n")
        mgr.append_to_section("memory", "读完《兰陵王》第18章，记风格观察：谶谣式死亡预告")

        assert "创作" not in _sections(tmp_path)
        assert mgr._is_creation_note("《兰陵王》读完18章") is False

    def test_creation_note_without_book_marks_still_counts(self, mgr):
        """创作侧的正常说法（无书名号）仍要认——这正是 33 条没写书名的现实。"""
        assert mgr._is_creation_note("读完第五章，开始写第六章") is True
        assert mgr._is_creation_note("第六章《投名状》分镜拆解完成") is True
        # 无任何创作信号 → 不认
        assert mgr._is_creation_note("托管站点 ai.xs91.com 的发布配置") is False

    def test_second_write_reuses_creation_section(self, mgr, tmp_path, monkeypatch):
        """第二次写入必须复用 §创作，不得新建重复分节。"""
        import deskmate.memory.manager as M

        monkeypatch.setattr(M, "llm_available", lambda: False)
        _write(tmp_path, f"§ 工作 #work\n- {_days_ago(1)}: 一条无关的工作进展\n")
        mgr.append_to_section("memory", f"{_days_ago(2)}: 第五章进展：章太聪革职被捕")
        mgr.append_to_section("memory", f"{_days_ago(1)}: 第六章人物设定已定")

        titles = [s["title"] for s in mgr._parse_sections(
            (tmp_path / "MEMORY.md").read_text(encoding="utf-8"))[1]]
        assert titles.count("创作") == 1, f"分节重复：{titles}"

    def test_creation_section_header_is_canonical(self, mgr):
        """`§ 创作 #stable` 的渲染必须带 #stable——那是它不被 TTL 吃掉的唯一凭据。

        同时锁死：`创作` 不得混进 `_TITLE_TO_TAG`。那个表是 `_TAG_TO_TITLE` 的
        反向唯一来源，一旦加进去，`_section_header("stable")` 会渲染成
        `§ 创作 #stable`，把「长期事实」这一节整个劫持掉。
        """
        import deskmate.memory.manager as M

        assert mgr._section_header("创作") == "§ 创作 #stable"
        assert mgr._section_header("stable") == "§ 长期事实 #stable"
        assert "创作" not in M._TITLE_TO_TAG

    def test_llm_classifier_can_actually_return_stable(self, mgr, monkeypatch):
        """分类器必须能吐出 stable——锁行为，不是锁源码文本。

        此前白名单只有 4 个标签，`stable` 是自动路径到不了的孤岛；
        这条用例注入 LLM 返回 "stable" 并要求路由接得住。
        """
        import deskmate.memory.manager as M

        monkeypatch.setattr(M, "chat", lambda *a, **k: "stable")
        assert mgr._llm_route_section("托管站点 ai.xs91.com 的发布配置") == "stable"
        monkeypatch.setattr(M, "chat", lambda *a, **k: "manuscript")
        assert mgr._llm_route_section("随便一条") is None, "未登记标签必须被拒，防越权分节"

    def test_fallback_section_never_decays(self, mgr):
        """兜底分节必须是不衰减的：旧兜底「笔记」= 7 天后删除且不落 vault。"""
        import deskmate.memory.manager as M

        ident = mgr._route_memory_section("今天空气不错")  # 无 LLM、无关键词命中
        assert ident == "长期事实"
        assert M._TITLE_TO_TAG[ident] not in M.MEMORY_DECAY_TTL

    def test_cleanup_keeps_creation_section(self, mgr, tmp_path):
        """分层净化不得把 `§ 创作 #stable` 迁进项目记忆（迁出即不可达）。"""
        _write(tmp_path,
            "§ 创作 #stable\n"
            f"- {_days_ago(3)}: 第五章《忘川帖》推进至第四节\n"
        )
        res = mgr.clean_global_memory()
        assert res["entries_migrated"] == 0
        assert "推进至第四节" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")

    def test_cleanup_keeps_serial_project_section(self, mgr, tmp_path):
        """「§ 连载项目」属授权+长期设定，含「叙事」二字但不得被判成书稿迁出。"""
        _write(tmp_path,
            "§ 连载项目 #我眼中的萧盛\n"
            "- 2026-09-14: 萧盛授权用长篇叙事文学手法写人物纪实，每天一篇\n"
        )
        assert mgr.clean_global_memory()["entries_migrated"] == 0
