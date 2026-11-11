"""USER.md 写入护栏与健康监测回归测试（2026-11-11 P0）。

背景：2026-11-11 17:41 一次整篇覆写把 USER.md 写成「画像」×4000 的退化文本。
原子写防不住「内容本身就是坏的」，这里锁住三道护栏：
- 退化重复（同一字符 200+ / 同一 2 字单元 50+）
- 分节结构丢失（旧有 ≥3 个 ##，新一个都没有）
- 长度突变（≤25% 骤降 / ≥4 倍骤增）
以及旁路监测 check_user_file_health。

全部用 tmp 目录，绝不碰真实 ~/.deskmate/USER.md。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deskmate.memory import user_profile as up

NORMAL = """# 用户画像

## 硬规矩
- 写入文件一律用 write_file。
- 取不到字节不算，不拿疑似当结论。

## 基本信息
- 萧盛，1977 年生，写作者。
- 《夜不收》正稿固定位置在桌面。

## 工作模式与AI协作
- 用户提想法，AI 负责实现。
- 写作时先用大白话讲机制，再补技术细节，不要营销腔。
- 交付前先给可观测证据（命中率、延迟、上下文占用），没有数字不算验收。
- 改代码只进本地 fork/base，不 push origin；先备份再动数据。
- 默认中文回复；术语保留原文，长句拆短，避免车轱辘话。
- 需要决策的点先报，不要自己改方案方向。
- 主稿正文与 entities.db 不碰；每轮收尾给回滚方式。
"""


@pytest.fixture
def base(tmp_path: Path) -> Path:
    (tmp_path / "USER.md").write_text(NORMAL, encoding="utf-8")
    return tmp_path


def _read(base: Path) -> str:
    return (base / "USER.md").read_text(encoding="utf-8")


def test_rejects_degenerate_repeat(base: Path):
    bad = "画像" * 4000 + "\n\n2026-11-11: 用户不希望再原样重复）---"
    assert up.save_user(base, bad) is False
    assert _read(base) == NORMAL  # 原文件不动
    alerts = (base / "alerts.log").read_text(encoding="utf-8")
    assert "拒绝写入" in alerts and "重复片段过长" in alerts
    rej = list((base / "backups" / "user-rejected").glob("USER.md.rejected-*.bak"))
    assert len(rej) == 1 and bad in rej[0].read_text(encoding="utf-8")


def test_rejects_single_char_run(base: Path):
    bad = "画" * 500
    assert up.save_user(base, bad) is False
    assert _read(base) == NORMAL


def test_rejects_structure_loss(base: Path):
    # 900 字符、无任何 ## 标题 → 结构丢失
    bad = "这是一段没有分节结构的文字。" * 60
    assert up.save_user(base, bad) is False
    assert _read(base) == NORMAL


def test_rejects_length_collapse(base: Path):
    assert up.save_user(base, "就剩一句话了") is False
    assert _read(base) == NORMAL


def test_rejects_length_spike(base: Path):
    bad = NORMAL + ("正常内容的额外补充说明。" * 800)
    assert up.save_user(base, bad) is False
    assert _read(base) == NORMAL


def test_allows_normal_append(base: Path):
    new = NORMAL + "\n## 偏好与事实（非常驻·待检索）\n- 念稿用御姐音。\n"
    assert up.save_user(base, new) is True
    assert "御姐音" in _read(base)
    # 写入前轮转备份了旧版本
    roll = list((base / "backups" / "user-rolling").glob("USER.md.*.bak"))
    assert roll and NORMAL in roll[0].read_text(encoding="utf-8")


def test_allows_normal_compression(base: Path):
    # 压缩到约 70%（方案允许「短至少 30%」），不应误伤
    shrunk = NORMAL.replace(
        "- 写作时先用大白话讲机制，再补技术细节，不要营销腔。\n", ""
    ).replace(
        "- 默认中文回复；术语保留原文，长句拆短，避免车轱辘话。\n", ""
    )
    assert len(shrunk) > len(NORMAL) * 0.5  # 约七成，同真实压缩
    assert up.save_user(base, shrunk) is True


def test_allows_first_write_without_old(tmp_path: Path):
    assert up.save_user(tmp_path, "萧盛，写作者。") is True


def test_reset_uses_force(base: Path):
    assert up.save_user(base, up._DEFAULT_USER, force=True) is True
    assert "用户画像" in _read(base)


def test_health_monitor_detects_bypass_write(base: Path):
    # 模拟绕过 save_user 的旁路写入（例如外部进程直接覆写）
    (base / "USER.md").write_text("画像" * 4000, encoding="utf-8")
    res = up.check_user_file_health(base, interval=0)
    assert res["checked"] and res["ok"] is False
    assert "重复片段过长" in res["reason"]
    assert "监测:" in (base / "alerts.log").read_text(encoding="utf-8")


def test_health_monitor_ok_on_normal(base: Path):
    res = up.check_user_file_health(base, interval=0)
    assert res["checked"] and res["ok"] is True


def test_append_path_blocks_incident_repro(base: Path, monkeypatch):
    """复现 17:41 事故链路：读取被替身劫持 + 真实目录写入 → 护栏必须拦住。

    事故时 append_to_section 的 current 来自被 monkeypatch 的 load_user
    （返回 "画像"×4000），save_user 再把它写进真实 USER.md。
    这里把 load_user 换成同样的替身，但 base 是临时目录，断言原文件不动。
    """
    from deskmate.memory.manager import DeskMateMemoryManager
    import deskmate.memory.manager as M

    monkeypatch.setattr(M, "load_user", lambda _b: "画像" * 4000)
    mgr = DeskMateMemoryManager(base_dir=base)
    msg = mgr.append_to_section("user", "2026-11-11: 用户不希望再原样重复）---")
    assert "被拒" in msg
    assert _read(base) == NORMAL


def test_write_memory_returns_refusal(base: Path):
    from deskmate.memory.manager import DeskMateMemoryManager

    mgr = DeskMateMemoryManager(base_dir=base)
    msg = mgr.write_memory("user", "画像" * 4000)
    assert "被拒" in msg
    assert _read(base) == NORMAL
