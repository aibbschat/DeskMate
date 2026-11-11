"""并发写 MEMORY.md / USER.md / trace 的安全性（漏修的并发重入防护）。

复现并锁死萧哥报过的「同一件事被重复写成两条」症状：三个入口（对话轮管理器
方法 / 后台守护线程 reflect_and_persist / 定时 run_dream）并发做「读-改-写」，
未加锁时会交错或互相覆盖。加锁后验证：
- 文件始终是某一次写的**完整**内容（无字节交错、无半截行）；
- 最终只保留一个写入者的标记（无丢失、无重复叠加）；
- trace 行数不丢不重，每行合法 JSON。
"""
from __future__ import annotations

import json
import threading

from deskmate.memory.manager import DeskMateMemoryManager as MemoryManager
from deskmate.memory.user_profile import load_user, save_user


def _spawn(n, target):
    threads = [threading.Thread(target=target, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_concurrent_save_user_no_corruption(tmp_path):
    n = 24
    marker = "USER_MARKER_{:03d}"  # 零填充：避免 "002" 被 "023" 包含导致的子串误判

    def worker(i):
        save_user(tmp_path, f"# 用户画像\n- {marker.format(i)}\n")

    _spawn(n, worker)
    content = load_user(tmp_path)  # load 成功即证明是合法 utf-8（无半截字节）
    hits = [marker.format(i) for i in range(n) if marker.format(i) in content]
    assert len(hits) == 1, f"并发写 USER.md 出现交错/丢失：命中 {len(hits)} 个 marker"


def test_concurrent_save_global_memory(tmp_path):
    mgr = MemoryManager(base_dir=tmp_path)
    n = 24
    marker = "MEM_MARKER_{:03d}"

    def worker(i):
        mgr._save_global_memory(f"# 全局记忆\n- {marker.format(i)}\n")

    _spawn(n, worker)
    content = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    hits = [marker.format(i) for i in range(n) if marker.format(i) in content]
    assert len(hits) == 1, f"并发写 MEMORY.md 出现交错/丢失：命中 {len(hits)} 个 marker"


def test_concurrent_append_trace_no_loss(tmp_path):
    mgr = MemoryManager(base_dir=tmp_path)
    n = 60

    def worker(i):
        mgr._append_trace("inject.jsonl", {"i": i})

    _spawn(n, worker)
    lines = (mgr._trace_dir() / "inject.jsonl").read_text(encoding="utf-8").splitlines()
    # 60 行远低于截断阈值（TRACE_MAX_LINES=2000），不应有任何丢失
    assert len(lines) == n, f"并发写 trace 丢失：期望 {n} 行，实际 {len(lines)} 行"
    for ln in lines:  # 无交错损坏：每行都是合法 JSON
        json.loads(ln)


def test_concurrent_mixed_memory_writes(tmp_path):
    """USER.md 与 MEMORY.md 同时被多线程写，互不影响、各自完整。"""
    mgr = MemoryManager(base_dir=tmp_path)
    n = 16

    def worker(i):
        save_user(tmp_path, f"# 用户画像\n- U{i:03d}\n")
        mgr._save_global_memory(f"# 全局记忆\n- M{i:03d}\n")

    _spawn(n, worker)
    u = load_user(tmp_path)
    m = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    u_hits = sum(1 for i in range(n) if f"U{i:03d}" in u)
    m_hits = sum(1 for i in range(n) if f"M{i:03d}" in m)
    assert u_hits == 1, f"USER.md 并发写异常：命中 {u_hits} 个"
    assert m_hits == 1, f"MEMORY.md 并发写异常：命中 {m_hits} 个"


def test_concurrent_journal_append_no_loss(tmp_path):
    """24 线程并发追加 journal 复盘条目，一条不丢。

    修前：固定 ``.tmp`` 文件名 + 无锁，后写者覆盖先写者，实测丢 17~18/24，
    且无异常路径假成功返回 added>0（上层还记「journal +1」成功日志）。
    """
    from datetime import date

    from deskmate.memory import journal_log

    n = 24

    def worker(i):
        journal_log.append_review(tmp_path, [f"并发复盘条目 {i:02d}"])

    _spawn(n, worker)
    content = journal_log._month_path(tmp_path, date.today()).read_text(encoding="utf-8")
    hits = sum(1 for i in range(n) if f"并发复盘条目 {i:02d}" in content)
    assert hits == n, f"并发写 journal 丢失：期望 {n} 条，实际 {hits} 条"
