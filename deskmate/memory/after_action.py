"""全局 after-action 记忆钩子。

把「做完事顺手记一句」做成全局机制，取代「发现一处补一处」的打补丁方式。

任何任务 / 对话轮结束后调用 ``reflect_and_persist``，它会：

1. **记一笔项目日志**：append 到 ``<base_dir>/projects/<project>/journal.md``。
   ``base_dir`` 默认 ``~/.deskmate``，**绝不写入用户书稿目录**。
2. **刷新小说圣经**：若 workspace 是已登记 / 疑似小说目录，逐本增量刷新
   ``<书>/.deskmate`` 圣经。**只在各书目录内建 ``.deskmate``，绝不在总目录根建**，
   守住书稿目录纯净（如 ``/Users/xiaocheng/Documents/已出版`` 只放书稿）。

设计要点：

- **零额外 LLM 成本**：项目日志用朴素文本摘要（时间 + 用户意图 + 助手做了啥 +
  已抽取的习得要点），不再调一次 LLM；圣经刷新靠文件 mtime 短路，无改动秒回。
- **防刷屏**：本轮 assistant 输出过短且无产物 / 习得时，跳过项目日志，但仍刷新圣经
  （读稿这种无"对话输出"的动作也要能更新记忆）。
- **守红线**：圣经刷新只在「该书已建过 ``.deskmate``」时做增量；对从未建圣经的陌生目录
  不擅自全量建（避免慢 + 避免往无关目录塞元数据）。

调用方（app.py 报告流 / 对话钩子、manager.py 任务收尾）均已接好。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from . import project_memory as _pm

logger = logging.getLogger(__name__)

_DEFAULT_BASE_DIR = Path(
    os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
)

# 防刷屏门槛：用户 / 助手任意一方有效长度低于此（字符）且无产物 / 习得，视为纯客套，不写日志。
# 阈值偏低（偏向「记」）：误记只是日志多几行（有裁剪），漏记才是违背「全局记忆习惯」的 bug。
_MIN_EFFECTIVE_CHARS = 4
# 项目日志最大保留长度，超出裁剪最旧内容
_JOURNAL_MAX_CHARS = 20_000
_BIBLE_DIR = ".deskmate"


# ---------------------------------------------------------------------------
# 项目名派生
# ---------------------------------------------------------------------------
def _project_name_for(workspace: Optional[str]) -> str:
    """从 workspace 路径派生项目名（用于项目笔记目录）。

    用最后一级目录名；含中文也没关系（``list_projects`` 不限制 ascii）。
    若 workspace 为空则落到 ``default``。
    """
    if not workspace:
        return "default"
    p = Path(workspace).expanduser()
    return p.name or "default"


# ---------------------------------------------------------------------------
# 圣经刷新（守红线：绝不在总目录根建 .deskmate）
# ---------------------------------------------------------------------------
def _is_book_with_bible(path: Path) -> bool:
    """判断 path 是否『一本书』（已有 .deskmate 圣经库）。"""
    return (path / _BIBLE_DIR).is_dir()


def _refresh_one_book(folder: str) -> None:
    """增量刷新一本书的圣经；返回 None（未建过）时静默跳过，不擅自全量建。"""
    from deskmate.novel_memory import core as novel_core

    try:
        result = novel_core.incremental_bible_update(folder)
        if result is None:
            logger.debug("圣经未建，跳过增量刷新：%s", folder)
        else:
            logger.info("圣经增量刷新完成：%s", folder)
    except Exception:
        logger.exception("圣经增量刷新失败：%s", folder)


def refresh_bible_if_novel(workspace: Optional[str]) -> None:
    """若 workspace 是小说目录，刷新圣经。

    守红线：绝不在总目录根建 ``.deskmate``。

    - workspace 本身是书（含 ``.deskmate``）→ 直接增量刷新该书。
    - workspace 是总目录（如 ``已出版``，自身无 ``.deskmate``）→ 遍历直接子目录，
      **只刷新已建过圣经的书**（``<书>/.deskmate`` 已存在），不在总目录根建任何东西。
    - 既不是书也不是总目录 → 不动。
    """
    if not workspace:
        return
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        return
    if _is_book_with_bible(root):
        _refresh_one_book(str(root))
        return
    # 疑似总目录：只刷新已建圣经的子书（零冗余、守红线）
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.name.startswith(".") and _is_book_with_bible(child):
            _refresh_one_book(str(child))


# ---------------------------------------------------------------------------
# 项目日志（记一笔）
# ---------------------------------------------------------------------------
def _append_journal(base_dir: Path, project: str, entry: str) -> None:
    """滚动追加项目日志，带日期分隔；超长裁剪最旧。"""
    fname = "journal.md"
    existing = _pm.read_project_file(base_dir, project, fname)
    sep = f"\n\n---\n\n## {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    if existing:
        new_content = existing + sep + entry
    else:
        new_content = f"# 项目日志：{project}\n" + sep + entry
    if len(new_content) > _JOURNAL_MAX_CHARS:
        tail = new_content[-_JOURNAL_MAX_CHARS:]
        new_content = new_content[:200] + "\n\n…（早期日志已截断）…\n\n" + tail
    _pm.write_project_file(base_dir, project, fname, new_content)


def _summarize_entry(
    user_msg: str, asst_msg: str, learned: Sequence[str]
) -> str:
    """生成一条朴素的项目日志条目（不含额外 LLM 调用，零成本）。"""
    parts: list[str] = []
    u = (user_msg or "").strip().replace("\n", " ")
    a = (asst_msg or "").strip().replace("\n", " ")
    if u:
        parts.append(f"**用户**：{u[:200]}")
    if a:
        parts.append(f"**助手**：{a[:400]}")
    if learned:
        facts = "\n".join(f"- {f}" for f in learned[:10])
        parts.append(f"**习得要点**：\n{facts}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def reflect_and_persist(
    workspace: Optional[str],
    last_user: str,
    last_asst: str,
    *,
    active_project: Optional[str] = None,
    learned: Sequence[str] | None = None,
    artifacts: Sequence[str] | None = None,
    base_dir: Optional[Path] = None,
) -> None:
    """全局 after-action 记忆钩子：记一笔项目日志 + 刷新小说圣经。

    Args:
        workspace: 当前任务 / 会话的工作目录（书稿目录或项目目录）。可为空。
        last_user / last_asst: 本轮用户 / 助手消息（用于日志摘要）。
        active_project: 优先用作项目名（来自 DeskMateMemoryManager.active_project）。
        learned: 由 post_session_summary 等 LLM 抽取得到的习得要点。
        artifacts: 本轮产物文件路径列表（有产物视为有实质产出）。
        base_dir: 记忆根目录，默认 ~/.deskmate。
    """
    base_dir = base_dir or _DEFAULT_BASE_DIR

    # ---- 1. 项目日志（记一笔）----
    effective_len = max(
        len((last_user or "").strip()), len((last_asst or "").strip())
    )
    has_artifacts = bool(artifacts)
    # 防刷屏：纯客套（双方都极短）且无产物/习得则不写日志，但仍刷新下面的圣经
    if effective_len >= _MIN_EFFECTIVE_CHARS or has_artifacts or learned:
        project = active_project or _project_name_for(workspace)
        entry = _summarize_entry(last_user, last_asst, learned or [])
        try:
            _append_journal(base_dir, project, entry)
        except Exception:
            logger.exception("after_action: 写项目日志失败")

    # ---- 2. 刷新圣经（修读稿不更新记忆）----
    try:
        refresh_bible_if_novel(workspace)
    except Exception:
        logger.exception("after_action: 刷新圣经失败")


async def reflect_and_persist_async(
    workspace: Optional[str],
    last_user: str,
    last_asst: str,
    **kwargs,
) -> None:
    """异步包装：把同步 I/O（圣经刷新可能扫大量文件）放到线程，避免阻塞事件循环。"""
    await asyncio.to_thread(
        reflect_and_persist, workspace, last_user, last_asst, **kwargs
    )
