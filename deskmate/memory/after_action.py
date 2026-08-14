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

记忆分层（对齐 Hermes 记忆漏斗，根治「日志越积越胀 / 全量注入」债）：

- **journal.md = 情景滚动缓冲**：保留原始「发生了什么」流水，硬上限 2 万字符，
  超出截断最旧。完整日志留在磁盘，经 ``manager._search_project_memory`` 按需检索，
  **不每次全量注入**。
- **longterm.md = 已蒸馏长期记忆（有界热记忆）**：把每轮 ``learned`` 习得要点去重固化，
  上限 6 千字符、超出从最旧裁剪（LRU）。**完整常驻注入**，是跨会话不丢的真相源。
- **注入窗口**：``format_project_memory`` 只注入 longterm（全量）+ journal 最近 N 条，
  整体有总字符上限兜底，prompt 成本不随历史线性膨胀。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
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
# 长期记忆（已蒸馏）上限：有界热记忆，超出从最旧裁剪（LRU）。对齐 Hermes L1 强制压缩。
_LONGTERM_MAX_CHARS = 6_000
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
# 闭环后主动抽取技能（对齐 Hermes learning loop）
# ---------------------------------------------------------------------------
def _maybe_extract_skill(base_dir: Path, last_user: str, last_asst: str) -> None:
    """本轮闭环后，主动把可复用的工作流沉淀为技能（Hermes 的「学完即用」核心）。

    由 ``extract_skill_from_session`` 经 LLM 判断是否值得保存为新技能，
    命中则写入技能库（默认 general 分类），供日后同类型任务匹配复用。
    仅在「本轮有实质内容」时才会被调用（见 ``reflect_and_persist`` 的门槛），
    避免对客套轮次空耗 LLM。任何异常都不阻断主流程。
    """
    try:
        from deskmate.skills.manager import SkillManager

        mgr = SkillManager(base_dir=base_dir)
        created = mgr.extract_skill_from_session(last_user, last_asst)
        if created:
            logger.info(
                "after_action: 主动沉淀技能 %s -> %s",
                created.get("name"), created.get("status"),
            )
    except Exception:
        logger.exception("after_action: 技能抽取失败")


# ---------------------------------------------------------------------------
# 闭环后主动识别关联未竟（对齐 Hermes 主动行为环 Task2）
# ---------------------------------------------------------------------------

# 零 LLM 成本的廉价预判信号词：本轮文本涉及任一信号，才值得跑完整调查链，
# 否则直接跳过（省每轮 3~4 次工具调查 + 1 次决策的开销）。宁可错杀也不浪费 token；
# 设 DESKMATE_CLOSE_LOOP_PREFILTER=0 可关闭此门控做全量调查。
_CLOSE_LOOP_SIGNALS = (
    "同步", "更新", "还没", "没有", "忘记", "漏", "遗漏", "版本", "配置",
    "部署", "网站", "公众号", "一致", "不一致", "改了", "改完", "修改",
    "改一下", "两处", "副本", "链接", "发布", "推送", "提交", "未提交",
    "还没做", "没做", "待办", "todo", "doc", "文档", "文件", "代码",
    "sync", "update", "deploy", "publish", "consistent", "config", "not done",
)


def _looks_like_close_loop_candidate(user_msg: str, asst_msg: str) -> bool:
    """零 LLM 调用的廉价预判：本轮是否可能涉及关联/未竟动作。"""
    if os.environ.get("DESKMATE_CLOSE_LOOP_PREFILTER", "1") == "0":
        return True  # 显式关闭前置门控，做全量调查
    text = (user_msg + " " + asst_msg).lower()
    return any(sig in text for sig in _CLOSE_LOOP_SIGNALS)


def _maybe_proactive_close_loop(
    base_dir: Path, workspace: Optional[str], last_user: str, last_asst: str
) -> None:
    """本轮闭环后，主动调查真实环境并判断是否有关联未竟应闭环（对齐 Hermes 主动行为环）。

    v2 改进：
    1. 加载记忆传给决策器（用户偏好/纠正历史参与判断）。
    2. 决策器内部会先用只读工具调查环境（read_file/search_files/git_status/read_memory），
       再基于调查结果做判断--不再是只看对话文本猜。

    高门槛（高置信度 + 可逆 + 低风险）才自动执行，否则降级为提案等待用户确认--
    绝不静默替用户做高风险动作。任何异常都不阻断主流程。
    """
    try:
        from deskmate.proactivity.close_loop import decide_close_loop
        from deskmate.proactivity import executor

        # 廉价前置门控：本轮文本无「关联/未竟」信号时，跳过完整调查链（省 LLM 成本）
        if not _looks_like_close_loop_candidate(last_user or "", last_asst or ""):
            return

        # 加载相关记忆：全局 MEMORY.md + 当前项目 longterm.md
        memory_context = _load_close_loop_memory(base_dir, workspace)

        decision = decide_close_loop(
            last_user or "",
            last_asst or "",
            memory_context=memory_context,
            workspace=workspace,
            base_dir=str(base_dir),
        )
        if not decision.should_close_loop:
            return
        # 记一笔识别日志（透明可审计，含调查发现）
        project = _project_name_for(workspace)
        investigation = decision.investigation_notes or "（无调查）"
        _append_journal(
            base_dir,
            project,
            f"**主动闭环识别**：{decision.action_summary}\n"
            f"- 理由：{decision.rationale}\n"
            f"- 置信度：{decision.confidence:.0%}｜可逆：{decision.reversible}｜风险：{decision.risk_level}\n"
            f"- 调查发现：{investigation[:500]}",
        )
        result = executor.try_execute_close_loop(decision, workspace)
        logger.info(
            "after_action: 主动闭环 -> executed=%s proposed=%s skipped=%s (%s)",
            result.executed, result.proposed, result.skipped, result.reason,
        )
    except Exception:
        logger.exception("after_action: 主动闭环识别失败")


def _load_close_loop_memory(
    base_dir: Path, workspace: Optional[str]
) -> str:
    """加载闭环决策用的记忆上下文：全局 MEMORY.md + 当前项目 longterm.md。

    对齐 Hermes 的记忆注入：不是全量灌入，而是按相关性截取。
    """
    parts: list[str] = []

    # 1. 全局记忆（MEMORY.md）--用户偏好/纠正历史
    memory_path = base_dir / "MEMORY.md"
    if memory_path.is_file():
        try:
            text = memory_path.read_text(encoding="utf-8", errors="replace")
            if text.strip():
                parts.append("## 全局记忆\n" + text[:2000])
        except OSError:
            pass

    # 2. 用户画像（USER.md）
    user_path = base_dir / "USER.md"
    if user_path.is_file():
        try:
            user_text = user_path.read_text(encoding="utf-8", errors="replace")
            if user_text.strip():
                parts.append("## 用户画像\n" + user_text[:1000])
        except OSError:
            pass

    # 3. 当前项目的长期记忆（longterm.md）
    if workspace:
        project = _project_name_for(workspace)
        longterm = _pm.read_project_file(base_dir, project, "longterm.md")
        if longterm.strip():
            parts.append(f"## 项目记忆（{project}）\n" + longterm[:1500])

    return "\n\n---\n\n".join(parts) if parts else ""


def _maybe_evolve_on_correction(
    base_dir: Path, last_user: str, last_asst: str
) -> None:
    """本轮用户疑似纠正了上一轮助手输出时，驱动相关技能自我进化（Hermes learning loop）。

    启发式前置：仅当本轮 user 含明显纠正信号词时才触发 LLM，避免每次收尾都消耗 LLM。
    内部 match_skills 命中不到技能时也会短路返回（零 LLM 成本）。任何异常不阻断主流程。
    """
    if not (last_user and last_asst):
        return
    _SIGNALS = (
        "不对", "错了", "错误", "纠正", "应该是", "不是", "搞错",
        "重新", "修正", "改一下", "你弄", "你搞", "记错", "弄反", "搞反",
    )
    if not any(sig in last_user for sig in _SIGNALS):
        return
    try:
        from deskmate.skills.manager import SkillManager

        mgr = SkillManager(base_dir=base_dir)
        updated = mgr.evolve_skill_on_correction(last_user, last_user)
        if updated:
            logger.info(
                "after_action: 纠正驱动技能进化 -> %s",
                [u.get("name") for u in updated],
            )
    except Exception:
        logger.exception("after_action: 技能进化失败")


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


def _norm(s: str) -> str:
    """归一化要点文本，用于去重比较（折叠空白 + 去首尾空格 + 小写）。"""
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _distill_to_longterm(
    base_dir: Path, project: str, learned: Sequence[str]
) -> None:
    """把本轮习得要点蒸馏进长期记忆（longterm.md）：去重 + 有界裁剪。

    对应 Hermes 的「结晶 / 蒸馏」：把高信噪比的习得事实从情景日志里固化出来，
    持久化且常驻注入；journal.md 仍保留原始情景流水（滚动缓冲），但不再是
    唯一真相源。longterm 超上限时从最旧要点裁剪（LRU），保证有界热记忆。

    零额外 LLM 成本——``learned`` 已是上游（post_session_summary）抽取好的结构化事实，
    这里只做去重固化，不再调一次 LLM。
    """
    if not learned:
        return
    existing = _pm.read_project_file(base_dir, project, "longterm.md")
    bullets: list[str] = []
    seen: set[str] = set()
    for line in existing.splitlines():
        s = line.strip()
        if s.startswith("- "):
            body = s[2:].strip()
            bullets.append(body)
            seen.add(_norm(body))
    added: list[str] = []
    for f in learned:
        fn = _norm(f)
        if fn and fn not in seen:
            seen.add(fn)
            added.append(f.strip())
    if not added:
        return
    new_bullets = bullets + added
    text = "# 长期记忆（已蒸馏）\n" + "\n".join(f"- {b}" for b in new_bullets)
    # 有界：超出从最旧裁剪（LRU）
    if len(text) > _LONGTERM_MAX_CHARS:
        over = len(text) - _LONGTERM_MAX_CHARS
        drop = 0
        while over > 0 and drop < len(new_bullets):
            over -= len(new_bullets[drop]) + 1
            drop += 1
        new_bullets = new_bullets[drop:]
        text = "# 长期记忆（已蒸馏）\n" + "\n".join(f"- {b}" for b in new_bullets)
    _pm.write_project_file(base_dir, project, "longterm.md", text)


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
        # 蒸馏习得要点到长期记忆（有界热记忆，跨会话不丢）
        try:
            _distill_to_longterm(base_dir, project, learned or [])
        except Exception:
            logger.exception("after_action: 蒸馏长期记忆失败")
        # 闭环后主动抽取可复用技能 + 主动闭环识别 + 技能进化 —— 全部放到后台 daemon 线程
        # ⚠️ 绝对不能等它们跑完才能返回响应，必须立刻回给前端，否则界面卡住
        from threading import Thread
        def _bg():
            _maybe_extract_skill(base_dir, last_user, last_asst)
            _maybe_proactive_close_loop(base_dir, workspace, last_user, last_asst)
            _maybe_evolve_on_correction(base_dir, last_user, last_asst)
        Thread(target=_bg, daemon=True).start()

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
