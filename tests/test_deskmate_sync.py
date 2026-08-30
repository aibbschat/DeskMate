"""P2-7 云端记忆同步测试（本地优先、零 LLM、fail-open）。

覆盖：敏感分节过滤、可同步文件枚举（排除设备本地状态）、LocalDirBackend 往返一致、
冲突检测、NoopBackend、MemorySyncService 状态/配置/双向同步。
"""

import json
import tempfile
from pathlib import Path

from deskmate.memory.sync import (
    LocalDirBackend,
    MemorySyncService,
    NoopBackend,
    filter_no_sync_sections,
)


def _make_base(tmp: Path) -> Path:
    base = tmp / "deskmate"
    base.mkdir()
    # 知识记忆
    (base / "USER.md").write_text("# 用户\n爱写历史悬疑\n", encoding="utf-8")
    (base / "MEMORY.md").write_text(
        "前言\n\n§ 写作偏好 #preference\n每天写两千字\n\n"
        "§ 家庭情况 #private #no-sync\n我儿子7岁，家住杭州\n",
        encoding="utf-8",
    )
    (base / "style_profile.json").write_text(json.dumps({"summary": "x"}), encoding="utf-8")
    vault = base / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("vault 内容\n", encoding="utf-8")
    proj = base / "projects" / "book1"
    proj.mkdir(parents=True)
    (proj / "note.md").write_text("项目笔记\n", encoding="utf-8")
    # 设备本地状态——应被排除
    (base / "behavior_log.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    g = base / "growth"
    g.mkdir()
    (g / "growth_state.json").write_text("{}", encoding="utf-8")
    (base / "emotion_timeline.json").write_text("{}", encoding="utf-8")
    (base / "traces").mkdir()
    (base / "traces" / "x.jsonl").write_text("{}", encoding="utf-8")
    return base


def test_filter_no_sync_drops_private_section():
    md = "前言\n\n§ 公开 #preference\n普通偏好\n\n§ 隐私 #private #no-sync\n我儿子7岁\n"
    out = filter_no_sync_sections(md)
    assert "§ 公开" in out
    assert "普通偏好" in out
    assert "我儿子7岁" not in out
    assert "§ 隐私" not in out
    assert "前言" in out


def test_filter_no_sync_keeps_normal():
    md = "§ 写作偏好 #preference\n爱写历史\n"
    assert filter_no_sync_sections(md) == md


def test_iter_syncable_excludes_device_local(tmp_path):
    base = _make_base(tmp_path)
    files = {p.name for p in MemorySyncService(base)._collect()}
    # 知识记忆在
    assert "USER.md" in files
    assert "MEMORY.md" in files
    assert "style_profile.json" in files
    assert "a.md" in files
    assert "note.md" in files
    # 设备本地状态不在
    assert "behavior_log.jsonl" not in files
    assert "growth_state.json" not in files
    assert "emotion_timeline.json" not in files
    assert "x.jsonl" not in files


def test_localdir_push_pull_roundtrip(tmp_path):
    base = _make_base(tmp_path)
    sync_dir = tmp_path / "icloud"
    svc = MemorySyncService(base)
    svc.configure(True, str(sync_dir))
    res = svc.push()
    assert res["pushed"] >= 5
    assert res["errors"] == []
    # 隐私分节不应出现在远端
    remote_mem = (sync_dir / "openworker-memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert "我儿子7岁" not in remote_mem
    assert "§ 家庭情况" not in remote_mem
    assert "§ 写作偏好" in remote_mem


def test_localdir_pull_consistency(tmp_path):
    base = _make_base(tmp_path)
    sync_dir = tmp_path / "icloud"
    svc = MemorySyncService(base)
    svc.configure(True, str(sync_dir))
    svc.push()
    # 另一台设备：空 base，拉取应还原知识记忆
    base2 = tmp_path / "deskmate2"
    svc2 = MemorySyncService(base2)
    svc2.configure(True, str(sync_dir))
    pull = svc2.pull()
    assert pull["pulled"] >= 5
    assert (base2 / "USER.md").is_file()
    assert "爱写历史悬疑" in (base2 / "USER.md").read_text(encoding="utf-8")


def test_conflict_detected_on_diverged_remote(tmp_path):
    base = _make_base(tmp_path)
    sync_dir = tmp_path / "icloud"
    svc = MemorySyncService(base)
    svc.configure(True, str(sync_dir))
    svc.push()
    # 远端被第三方改动（mtime 更新、内容不同）
    remote_user = sync_dir / "openworker-memory" / "USER.md"
    import os
    remote_user.write_text("# 用户\n远端改过的内容\n", encoding="utf-8")
    # 远端 mtime 必须比本地「现在」更新，才能被判定为冲突
    os.utime(remote_user, (10**10, 10**10))
    res = svc.push()
    assert "USER.md" in res["conflicts"]


def test_noop_backend_is_safe():
    b = NoopBackend()
    b.write("x.md", "hi")
    assert b.read("x.md") is None
    assert b.exists("x.md") is False
    assert b.list_relpaths() == []
    assert b.mtime("x.md") == 0.0


def test_service_disabled_by_default(tmp_path):
    base = _make_base(tmp_path)
    status = MemorySyncService(base).status()
    assert status["enabled"] is False
    assert status["backend"] == "noop"
    # 默认不写远端（Noop），远端文件数为 0
    MemorySyncService(base).push()
    assert MemorySyncService(base).status()["remote_files"] == 0


def test_sync_bidirectional_counts(tmp_path):
    base = _make_base(tmp_path)
    sync_dir = tmp_path / "icloud"
    svc = MemorySyncService(base)
    svc.configure(True, str(sync_dir))
    res = svc.sync()
    assert res["pushed"] >= 5
    assert res["errors"] == []
    assert MemorySyncService(base).status()["last_sync"] is not None
