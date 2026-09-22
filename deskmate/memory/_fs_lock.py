"""跨进程安全的「加锁 + 原子写」工具。

为何存在
--------
记忆层的核心写路径（``MemoryManager._save_global_memory`` 写 MEMORY.md、
``save_user`` 写 USER.md、``_append_trace`` 截断写 trace 文件）历史上**没有**
任何互斥保护，而它们被三个独立入口调用：

1. 对话轮内的管理器方法（主线程 / 协程）；
2. ``after_action.reflect_and_persist`` 的 ``_bg()`` 后台守护线程；
3. 定时 ``run_dream`` 提炼任务。

三者重叠时进行「读-改-写」会互相覆盖 / 交错，正是「同一件事被重复写成两条」
（并发重入）这一已知症状的根因。项目约定「多入口写同一文件须 fcntl.flock」
（见 agency/outbound、growth/diary），但核心记忆写路径漏了这层锁——属漏修。

设计
----
- 锁文件与数据文件**同目录、同 stem**（``MEMORY.md`` → ``MEMORY.wlock``），
  因此 flock 与数据落在同一文件系统，跨进程有效。
- 锁文件与数据文件**分离**：临界区内先写临时文件再 ``os.replace`` 落盘，
  既满足「原子写（临时文件 + os.replace）」红线，又不受 replace 改 inode
  导致锁失效的影响（replace 只换数据 inode，不动锁文件）。
- 无 fcntl（Windows）退化为进程内线程锁，至少挡住同进程的两个入口。
- 全程 fail-open：建锁目录 / 加锁失败一律降级为无锁，绝不让记忆写不出去。
"""
from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path
from typing import Iterator

try:  # POSIX 跨进程文件锁；Windows 无 fcntl，退化为进程内线程锁
    import fcntl
except ImportError:  # pragma: no cover - 仅 Windows
    fcntl = None  # type: ignore[assignment]


_thread_locks: dict[str, "threading.Lock"] = {}
_thread_locks_guard = threading.Lock()


def _thread_lock(key: str) -> "threading.Lock":
    with _thread_locks_guard:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _thread_locks[key] = lock
        return lock


@contextlib.contextmanager
def file_write_lock(path: Path) -> Iterator[None]:
    """对 ``path`` 的「数据文件」加跨进程互斥锁。

    锁文件为 ``path`` 同目录、同 stem 的 ``.wlock`` 文件；临界区内可安全地进行
    读-改-写 / 临时文件 + os.replace。fail-open：任何异常都退化为无锁。
    """
    path = Path(path)
    lock_path = path.with_suffix(".wlock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield  # 建不了目录（只读挂载等）——退化无锁
        return
    with open(lock_path, "a+", encoding="utf-8") as fh:
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except OSError:
                yield  # 加锁失败——退化无锁
                return
            try:
                yield
            finally:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        else:  # pragma: no cover - 仅 Windows
            _thread_lock(str(lock_path)).acquire()
            try:
                yield
            finally:
                _thread_lock(str(lock_path)).release()


def atomic_write_locked(path: Path, content: str) -> None:
    """加锁 + 原子写（临时文件 + os.replace）到 ``path``。"""
    path = Path(path)
    with file_write_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
