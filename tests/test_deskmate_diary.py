"""Tests for P1-3 桌宠日记产品化（deskmate/growth/diary.py）。

核心：把 growth 信号转成小桔第一人称日记 + 每周回忆；幂等、诚实（不编造）、
独立于 memory 层（写入 <base>/diary/，不碰 P0-6/P0-7）。
"""

from __future__ import annotations

import datetime
from pathlib import Path

from deskmate.growth.diary import DiaryWriter, _evt_date
from deskmate.growth.engine import GrowthEngine


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class TestDiaryGeneration:
    def test_diary_with_events(self, tmp_path):
        eng = GrowthEngine(base_dir=tmp_path)
        ts = _now_utc_iso()
        eng.state.recent_events.append({"ts": ts, "kind": "creation_approved", "note": "定稿"})
        eng.state.recent_events.append({"ts": ts, "kind": "suggestion_accepted"})
        eng.state.recent_events.append({"ts": ts, "kind": "memory_learned"})
        eng.state.total_chats = 42
        eng.save()
        w = DiaryWriter(tmp_path)
        path = w.ensure_today()
        content = Path(path).read_text(encoding="utf-8")
        assert "定稿" in content or "拍板" in content
        assert "采纳" in content
        assert "42" in content
        assert "没有的我不编" in content

    def test_diary_empty_is_quiet_not_fabricated(self, tmp_path):
        """无任何信号 → 只写安静的陪伴，不编造活动。"""
        w = DiaryWriter(tmp_path)
        content = Path(w.ensure_today()).read_text(encoding="utf-8")
        assert "安静" in content
        assert "没有的我不编" in content
        assert "聊了天" not in content  # 不能编造"聊天"

    def test_ensure_today_idempotent(self, tmp_path):
        w = DiaryWriter(tmp_path)
        p1 = w.ensure_today()
        Path(p1).write_text("手改后的内容", encoding="utf-8")
        p2 = w.ensure_today()
        assert p1 == p2
        assert Path(p1).read_text(encoding="utf-8") == "手改后的内容"  # 未重写

    def test_badge_unlocked_on_date_shown(self, tmp_path):
        eng = GrowthEngine(base_dir=tmp_path)
        eng.state.badges_unlocked["first_chat"] = _now_utc_iso()
        eng.save()
        w = DiaryWriter(tmp_path)
        content = Path(w.ensure_today()).read_text(encoding="utf-8")
        assert "初见" in content  # first_chat 徽章名「初见」

    def test_empathy_domain_shown(self, tmp_path):
        eng = GrowthEngine(base_dir=tmp_path)
        eng.state.empathy_domains["creation"] = 80.0
        eng.save()
        w = DiaryWriter(tmp_path)
        content = Path(w.ensure_today()).read_text(encoding="utf-8")
        assert "创作" in content  # 学会偏好：最投入「创作」

    def test_state_numbers_always_shown(self, tmp_path):
        eng = GrowthEngine(base_dir=tmp_path)
        eng.state.intimacy = 60.0
        eng.state.trust = 0.8
        eng.state.total_chats = 7
        eng.save()
        w = DiaryWriter(tmp_path)
        content = Path(w.ensure_today()).read_text(encoding="utf-8")
        assert "60" in content and "0.8" in content and "7" in content


class TestWeeklyRecap:
    def test_recap_sums_events_in_window(self, tmp_path):
        eng = GrowthEngine(base_dir=tmp_path)
        ts = _now_utc_iso()
        eng.state.recent_events.append({"ts": ts, "kind": "creation_approved"})
        eng.state.recent_events.append({"ts": ts, "kind": "suggestion_accepted"})
        eng.state.recent_events.append({"ts": ts, "kind": "suggestion_accepted"})
        eng.save()
        w = DiaryWriter(tmp_path)
        Path(w.ensure_today())  # 生成今日日记
        recap = w.weekly_recap(7)
        assert "回忆" in recap
        assert "定稿创作 1 份" in recap
        assert "采纳提醒 2 次" in recap

    def test_recap_no_diaries(self, tmp_path):
        w = DiaryWriter(tmp_path)
        recap = w.weekly_recap(7)
        assert "还没有日记" in recap

    def test_list_and_read(self, tmp_path):
        w = DiaryWriter(tmp_path)
        w.ensure_today()
        dates = w.list_diaries()
        assert len(dates) == 1
        today = datetime.date.today().isoformat()
        assert dates[0] == today
        assert w.read(today) is not None
        assert w.read("2099-01-01") is None


class TestTimeMatching:
    def test_evt_date_utc_to_local(self):
        # 取今天本地正午转成 UTC 串，转回本地日期仍应是今天。
        # 不写死日期 —— 原测试硬编码 2026-08-29，过一天就必然失败。
        noon_local = datetime.datetime.combine(
            datetime.date.today(), datetime.time(12, 0)
        ).astimezone()
        ts = noon_local.astimezone(datetime.timezone.utc).isoformat()
        assert _evt_date(ts) == datetime.date.today().isoformat()

    def test_diary_under_base_dir(self, tmp_path):
        """验证写的是 <base>/diary 而非全局 ~/.deskmate。"""
        w = DiaryWriter(tmp_path)
        p = w.ensure_today()
        assert str(Path(p)).startswith(str(tmp_path))
        assert "diary" in str(Path(p))
