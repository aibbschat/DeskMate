"""神圣稿件路径守卫 —— **单一事实源**（红线）。

为什么单独成模块（2026-09-23 巡检后重构）：
在此之前，同样的守卫逻辑散落在 ``deskmate/ingest/local_intake.py`` 与
``coworker/history/private_store.py`` 各有一份副本，且**两份都已出现漏防**——

1. **ingest 侧零守卫**：``_is_sacred`` 只在 scan 的遍历函数里被调用，而
   ``build_facts`` / ``ingest_files`` 直接吃外部传入的 path 就读内容，端点还
   允许任意 ``paths`` → 直传神圣路径就被灌进记忆库（实测可复现）。
2. **symlink 绕过**：判定只看**字面路径**的 parts；配置目录里放一个
   ``dirlink -> ~/国子监秘闻录``，遍历时 ``entry.name`` 是 "dirlink"（不含敏感词），
   解开并未 resolve 就被 follow 进神圣目录（实测可复现）。

两份副本必然再次漂移，因此收敛到这里：所有「要读用户文件」的入口都必须过本模块。

威胁模型与对策：
- 直传任意路径  → :func:`guarded_file` 强制 ``resolve()`` 后再判；
- symlink / 硬构造路径 → 判定基于 **resolve 后的真实路径**，且字面路径也一并查；
- 非文本文件被当文本读 → 调用方必须传 ``allowed_exts`` 白名单。

设计取舍：**允许指向非神圣位置的合法 symlink**（用户拿软链整理笔记是正当用法），
只把「解析后落在神圣路径内」这一条钉死成一票否决。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# 神圣稿件标识：任何路径片段含这些字符串 → 跳过（不依赖具体路径，稳健）
SACRED_FRAGMENTS: tuple[str, ...] = ("国子监秘闻录",)


def _candidate_paths(path: Path) -> list[Path]:
    """字面路径 + 解析后的真实路径（尽力，失败不影响后续判定）。"""
    out = [path]
    try:
        rp = path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        # 解析失败（如权限、循环软链）→ 只能判字面路径
        return out
    if rp != path:
        out.append(rp)
    return out


def is_sacred(path: str | Path) -> bool:
    """该路径（或其解析后的真实路径）是否触碰神圣稿件。

    同时检查 ``parts`` 与完整路径字符串——前者覆盖分段命名，后者兜住
    「片段被拼在某一段内部」等边缘情况。
    """
    try:
        base = Path(str(path))
    except (TypeError, ValueError):
        return True  # 判不了就当危险，fail-closed
    for cand in _candidate_paths(base):
        for frag in SACRED_FRAGMENTS:
            if frag in str(cand) or any(frag in part for part in cand.parts):
                return True
    return False


def guarded_file(
    path: str | Path,
    allowed_exts: Iterable[str] | None = None,
) -> Path | None:
    """外部传入的路径 → 可安全读取的**解析后**路径；不可读则返回 ``None``。

    依次把关：
    1. 神圣稿件（字面 + resolve 后）→ 拒；
    2. ``resolve()`` 失败 / 解析后不是普通文件 → 拒；
    3. ``allowed_exts`` 给了则校验扩展名白名单 → 不在名单拒。

    Returns:
        解析后的 :class:`Path`；被拒时 ``None``（调用方按 skip 处理）。
    """
    if path is None:
        return None
    try:
        base = Path(str(path)).expanduser()
    except (TypeError, ValueError):
        return None

    if is_sacred(base):
        logger.warning("已拦截神圣稿件路径（不读取内容）: %s", base)
        return None

    try:
        real = base.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if real != base and is_sacred(real):
        logger.warning("已拦截经符号链接指向神圣稿件的路径: %s -> %s", base, real)
        return None
    try:
        if not real.is_file():
            return None
    except OSError:
        return None

    if allowed_exts is not None:
        wanted = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in allowed_exts}
        if real.suffix.lower() not in wanted:
            return None
    return real


def guarded_dir_entry(path: str | Path) -> bool:
    """遍历（scan）时判断某个目录/文件条目是否可继续深入。

    与 :func:`guarded_file` 的区别：这里允许目录通过，只做神圣判定——
    因为遍历时需要对目录递归、对文件再走 :func:`guarded_file` 的扩展名校验。
    """
    try:
        base = Path(str(path))
    except (TypeError, ValueError):
        return False
    if is_sacred(base):
        logger.warning("已跳过神圣稿件路径: %s", base)
        return False
    try:
        real = base.resolve()
    except (OSError, RuntimeError, ValueError):
        return True  # 解析不了（断了链的软链）→ 后续 is_file 会自然淘汰
    if real != base and is_sacred(real):
        logger.warning("已跳过符号链接指向的神圣稿件路径: %s -> %s", base, real)
        return False
    return True
