"""跨进程写锁回归测试（P1-6 遗留缺口的修复验证）。

验证 _BibleWriteLock：
1. 进程内可重入（build_bible 临界区里再调 upsert_records 不会自锁死锁）；
2. 真正跨进程串行化——sidecar 主进程与 novel_memory MCP server 两个独立进程
   同时写同一本 entities.db 时，各自在锁内的写块不会被对方插断（杜绝
   「增量抽完 → build 清库 → 增量写进空库」的逻辑交错）。

不烧 LLM、不碰 ~/.deskmate（用 tmp 锁文件），fail-open。
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from deskmate.novel_memory.entities import _BibleWriteLock

_REPO_ROOT = Path(__file__).resolve().parents[1]

# 子进程临界区脚本：在锁内对共享文件写一段「START pid / sleep / END pid」，
# 用以检测两个进程是否在各自写块中间被对方插断（交错即证明跨进程锁失效）。
_XPROC_SCRIPT = r'''
import sys, time, os
from pathlib import Path
repo = sys.argv[1]
sys.path.insert(0, repo)
from deskmate.novel_memory.entities import _BibleWriteLock
lock = _BibleWriteLock(Path(sys.argv[2]))
out = Path(sys.argv[3])
pid = os.getpid()
with lock:
    with open(out, "a") as f:
        f.write("START %d\n" % pid)
        f.flush()
        time.sleep(0.3)
        f.write("END %d\n" % pid)
        f.flush()
'''


def test_inprocess_reentrant_no_deadlock(tmp_path):
    """进程内嵌套 with 不应自锁；释放后另一线程应能获取。"""
    lock = _BibleWriteLock(tmp_path / "w.lock")

    # 嵌套进入（build_bible 临界区里再调 upsert_records 的场景）
    with lock:
        with lock:
            pass

    acquired = []

    def worker():
        with lock:
            acquired.append(True)

    th = threading.Thread(target=worker)
    th.start()
    th.join(timeout=3)
    assert acquired == [True], "进程内锁未能被另一线程获取（可能自锁）"
    # 锁文件应在首次进入后被创建
    assert (tmp_path / "w.lock").exists()


def test_crossprocess_serializes_writes(tmp_path):
    """两个独立进程各在锁内写一段带 sleep 的块，断言两段不交错。"""
    lock_file = tmp_path / "write.lock"
    out_file = tmp_path / "out.txt"
    if out_file.exists():
        out_file.unlink()

    # 并发启动两个子进程；若 flock 真正跨进程生效，A 的整段（START+END）必在
    # B 的整段之前或之后，不会出现 START_A / START_B / END_A / END_B 交错。
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _XPROC_SCRIPT, str(_REPO_ROOT),
             str(lock_file), str(out_file)],
            cwd=str(_REPO_ROOT),
        )
        for _ in range(2)
    ]
    for p in procs:
        assert p.wait(timeout=15) == 0, "子进程异常退出（可能锁获取失败）"

    lines = out_file.read_text(encoding="utf-8").splitlines()
    # 逐块校验：每个 START 之后必须紧跟自己的 END，中间不能插入对方的 START。
    i = 0
    while i < len(lines):
        assert lines[i].startswith("START "), f"意外的行：{lines[i]!r}"
        pid = lines[i].split()[1]
        i += 1
        assert i < len(lines) and lines[i] == f"END {pid}", (
            f"写块交错！期望 'END {pid}' 但读到 {lines[i]!r} —— 跨进程锁失效"
        )
        i += 1
    assert i == 4, f"应正好 4 行（2 进程 × START/END），实际 {lines!r}"
