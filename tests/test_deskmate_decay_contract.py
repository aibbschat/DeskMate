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
