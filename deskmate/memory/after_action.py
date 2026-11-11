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
- **守干净原则**：圣经刷新只在「该书已建过 ``.deskmate``」时做增量；对从未建圣经的陌生目录
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
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from . import project_memory as _pm

logger = logging.getLogger(__name__)

def _default_base_dir() -> Path:
    """默认根目录——**调用时**解析 ``DESKMATE_DIR``。

    ⚠ 历史坑（2026-09-21）：这里曾是模块级常量
    ``Path(os.environ.get("DESKMATE_DIR", expanduser("~/.deskmate")))``。
    它在 import 期求值，而 conftest 是 import **之后**才 setenv DESKMATE_DIR
    —— 模块级求值等于没隔离，``_append_to_global_memory`` 的默认落点会指向
    开发者**真实 USER.md / MEMORY.md**。同族修复见 ``memory/persistence.py``。
    """
    from deskmate.paths import deskmate_base_dir

    return deskmate_base_dir()

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


def _safe_book(path: Path) -> bool:
    """该书是否已建圣经（可安全刷新 / 回流）。

    2026-09-24 萧哥拍板：自己的稿子（如《国子监秘闻录》）是写作助理的核心工作对象，
    读稿 / 自动刷新人物圣经 / 回流讨论都是红线之内、用户授权的正常行为，不再排除。
    此处只判断「该书是否有 ``.deskmate`` 圣经库」——未建过的陌生目录绝不擅自全量建
    （守的是「不在无关目录塞元数据」的干净原则，而非把稿子当禁区）。
    """
    return _is_book_with_bible(path)


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
    if _safe_book(root):
        _refresh_one_book(str(root))
        return
    # 疑似总目录：只刷新已建圣经的子书（零冗余、守红线）
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.name.startswith(".") and _safe_book(child):
            _refresh_one_book(str(child))


def _upsert_produced_to_bible(workspace: Optional[str], text: str) -> None:
    """把一段产出文本增量回流进 workspace 内各书的圣经（内容侧自动闭环）。

    与 ``refresh_bible_if_novel``（文件侧）互补：后者扫盘捡新文件，本函数直接吃
    对话产出 / 圆桌报告等未落盘文本。复用 ``novel_core.upsert_bible_from_text``，
    该书未建圣经时静默跳过（守红线）；神圣稿件经 ``_safe_book`` 硬隔离。任何异常
    都不阻断主流程（fail-open）。
    """
    if not workspace or not text or not text.strip():
        return
    from deskmate.novel_memory import core as novel_core

    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        return
    books: list[Path] = []
    if _safe_book(root):
        books.append(root)
    else:
        for child in sorted(root.iterdir()):
            if child.is_dir() and not child.name.startswith(".") and _safe_book(child):
                books.append(child)
    for b in books:
        try:
            novel_core.upsert_bible_from_text(str(b), text, source_label="对话产出")
        except Exception:
            logger.exception("after_action: 产出文本回流圣经失败：%s", b)


def _upsert_artifacts_to_bible(workspace: Optional[str], artifacts: Sequence[str]) -> None:
    """把本轮产物文件内容（若有）回流进圣经——精确触发，仅当本轮有真实产物。"""
    if not workspace or not artifacts:
        return
    chunks: list[str] = []
    for p in artifacts:
        try:
            t = Path(p).expanduser().read_text(encoding="utf-8", errors="replace")
            if t.strip():
                chunks.append(t)
        except OSError:
            continue
    if chunks:
        _upsert_produced_to_bible(workspace, "\n\n".join(chunks))


def _upsert_content_loop(workspace: Optional[str], last_asst: str, artifacts) -> None:
    """内容侧闭环的门槛收敛：仅当 workspace 本身就是一本已建圣经的书时，才把对话产出
    自动回流（避免总目录下对每本子书盲目跑抽取）；产物文件则精确触发。

    放在后台守护线程调用（见 ``reflect_and_persist`` 的 ``_bg``），LLM 抽取再慢也不阻塞
    响应；fail-open，任何异常不阻断主流程。
    """
    try:
        root = Path(workspace).expanduser().resolve() if workspace else None
        if root and root.is_dir() and _safe_book(root):
            if last_asst and len(last_asst.strip()) >= 300:
                _upsert_produced_to_bible(workspace, last_asst)
        if artifacts:
            _upsert_artifacts_to_bible(workspace, artifacts)
    except Exception:
        logger.exception("after_action: 产出回流圣经（内容侧）失败")


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
    # ⚠ 信号词表（2026-09-22 收紧）：此表是「本轮用户在纠正 AI 吗」的**唯一**
    # 判据，而判中的后果之一是 `GrowthEngine.record_correction()` 扣信任感
    # （trust -0.02，且是**自动、单向**的——正向反馈要用户显式点「接受/定稿」）。
    # 旧表含「不是 / 应该是 / 重新 / 改一下 / 错误」这类中文高频歧义词：
    #   · 「不是明天，是后天」——他在说自己的安排，不是 AI 出错
    #   · 「改一下标题」「重新生成一版」——普通指令，不是纠错
    # 结果信任感被误扣至归零（真实库实测 trust=0.0；behavior_log 里 correction
    # 触发 85 次，而 suggestion_accepted / creation_approved 为 0 次）。
    # 现在只保留**明确指向对方输出**的强信号词。
    _SIGNALS = (
        "不对", "错了", "搞错", "记错", "弄反", "搞反", "纠正",
        "你弄", "你搞", "你写错",
    )
    if not any(sig in last_user for sig in _SIGNALS):
        return
    # P0-3：明确纠正 → 信任/默契微调（best-effort，不阻断技能进化）
    try:
        from deskmate.growth.engine import GrowthEngine

        # 带上纠正原话：日记要写得出「他说的是哪一句」，而不是「他纠正了我一次」。
        GrowthEngine(base_dir=base_dir).record_correction(note=last_user)
    except Exception:
        pass
    try:
        from deskmate.skills.manager import SkillManager

        mgr = SkillManager(base_dir=base_dir)
        # 证据流：correction_text=用户纠正；user_message=纠正原文（匹配技能用）；
        # assistant_output=上一轮被纠正的助手输出（截断，供 LLM 判断「助手错在哪」）。
        updated = mgr.evolve_skill_on_correction(
            last_user, last_user, assistant_output=last_asst[:800]
        )
        if updated:
            logger.info(
                "after_action: 纠正驱动技能进化 -> %s",
                [u.get("name") for u in updated],
            )
    except Exception:
        logger.exception("after_action: 技能进化失败")


# ---------------------------------------------------------------------------
# 会话后后台记忆复盘（Hermes 记忆复盘半环：把「关于用户」蒸馏进 USER.md / MEMORY.md）
# ---------------------------------------------------------------------------

# 廉价前置信号：本轮文本涉及「关于人/偏好/约定」才值得跑记忆 LLM，否则跳过省 token。
_MEMORY_REVIEW_SIGNALS = (
    "喜欢", "习惯", "希望", "不要", "别", "总是", "从不", "偏好",
    "我要", "我以为", "记住", "以后", "默认", "风格", "格式",
    "你以后", "请", "必须", "不想", "宁可", "应该", "约定", "规则",
    "如何", "怎么写", "prefer", "always", "never", "remember",
    "want you to", "my style", "i like", "by default",
)


def _looks_like_memory_candidate(user_msg: str, asst_msg: str) -> bool:
    """零 LLM 调用的廉价预判：本轮是否可能暴露了用户画像 / 偏好 / 持久约定。"""
    if os.environ.get("DESKMATE_MEMORY_REVIEW_PREFILTER", "1") == "0":
        return True
    text = (user_msg + " " + asst_msg).lower()
    return any(sig in text for sig in _MEMORY_REVIEW_SIGNALS)


_MEMORY_REVIEW_PROMPT = """你是一段对话后的「记忆复盘器」。阅读以下最近一轮对话，判断是否有值得长期记住的「关于用户本人」的事实。\n\n只关注：\n1. 用户的身份、职业、背景、长期偏好（如写作领域、史料严谨要求、厌恶的格式/语气）。\n2. 用户对未来行为的期望（"以后默认用 X"、"别用 Y"、"记住 Z"）。\n3. 用户的工作流/协作约定（如"改了公众号要同步改网站"这类跨系统约定）。\n\n不要记：\n- 一次性任务的具体步骤（除非它反映了可复用的偏好）。\n- 会话内临时信息、闲聊。\n\n**去重（最重要的一条）**：下面会给你一份「已记录」清单。凡是意思相同或相近的——**换了说法也算**——一律不要输出。\n只有两种情形值得输出：\n1. **真正新增**的事实、偏好、约定；\n2. **数值 / 规则发生变化的修订**：这时**只输出新的说法**，并在句末附「（更新：旧值 X）」，\n   千万不要再单独输出一条"旧值保留备查"——旧值由系统自己留证据，不用你重写一遍。\n宁可少写一条，也不要重复一条：这些条目每条都会被注入下一次对话，重复即噪声。\n\n输出 JSON：\n{"updates": [{"target": "user", "text": "..."}, ...]}\ntarget 选 "user"（用户画像/偏好，写入 USER.md）或 "memory"（跨项目持久事实，写入 MEMORY.md）。\n若没有值得记的（包括"这些都已经记过了"），输出 {"updates": []}。\n只输出 JSON，不要解释。\n"""

# 现有记忆喂给复盘器的字符预算（每文件）。给多了抬成本，给少了还会重复，
# 2000 字符 ≈ 手写画像条目 + 最近的复盘条目。
_REVIEW_DIGEST_BUDGET = 2000


def _memory_digest_for_review(base_dir: Path, filename: str,
                              budget: int = _REVIEW_DIGEST_BUDGET) -> str:
    """抽出某记忆文件里"已经记了什么"，喂给复盘器做去重（**不是喂全文**）。

    为什么需要（2026-09-21 定因）：原实现的 prompt 只有本轮对话，
    复盘器**看不到已经记过什么**，于是每天的复述都当新事实再写一遍——
    实测同一条「写作营格式要求」在三天里被写了三遍（措辞各不相同）。
    与其事后合并，不如从源头不产生。

    顺序：先给**手写分节的条目**（画像本体、约束最强），再给**复盘条目**（新→旧）。
    越靠前越不会被预算截掉。
    """
    path = base_dir / filename
    try:
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    except OSError:
        return ""
    if not text:
        return ""
    # 1) 手写分节（非复盘）的条目行，按文件顺序
    handwritten = text
    for start, end in reversed(_recap_spans(text)):
        handwritten = handwritten[:start] + handwritten[end:]
    bullets: list[str] = [
        ls for ls in (l.strip() for l in handwritten.splitlines())
        if ls.startswith(("- ", "* "))
    ]
    # 2) 复盘条目，从新到旧
    for _date, entry in sorted(
        _recap_entries(text), key=lambda e: e[0], reverse=True
    ):
        bullets.append("- " + entry)
    out: list[str] = []
    used = 0
    for b in bullets:
        if used + len(b) + 1 > budget:
            break
        out.append(b)
        used += len(b) + 1
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 复盘节的「一天一节 + 近重复合并」工具（2026-09-21）
#
# 设计约束（都是踩出来的）：
# 1. **手写分节必须逐字节不动**：``## 基本信息`` / ``## 查资料铁律`` 等是画像本体，
#    任何重排都是事故。所以重建时只切"复盘节所在的连续区间"，
#    区间之外的 prefix / suffix **原样拷贝**。
# 2. **不许用标题当 dict key**：重复节头（正是本 bug 的产物）会互相覆盖，
#    自校验就会漏检。一律按下标逐块处理。
# 3. **相似度只在"剥掉标记"后算**：``（已被更新覆盖，旧值保留备查）`` 这类
#    尾巴是**同一句模板**，留着算会让两条毫不相干的事实在它上面"撞高分"
#    （实测：伪造相似度 0.60，真近重复反而只有 0.30）。
# ---------------------------------------------------------------------------

# ``## 自动复盘 · 2026-09-21``（也容忍 ``## 自动复盘：2026-09-21`` 等写法）
_RECAP_HEAD_RE = re.compile(r"(?m)^##(?!#)[^\n]*自动复盘[^\n]*")
_RECAP_DATE_RE = re.compile(r"(\d{4}-\d{1,2}-\d{1,2})")
_RECAP_HEADING = "自动复盘"
# 「（已被更新覆盖，旧值保留备查）」等各种变体：标记，不是内容
_RECAP_MARK_RE = re.compile(r"[（(][^（()）]*被更新覆盖[^（()）]*[)）]")
# 相似度阈值：2-gram Jaccard。实测真近重复落在 0.30–0.60，
# 不同事实（哪怕同为"用户的写作要求"）落在 0.25 以下。
_RECAP_DUP_SIM = 0.30
# 相似度到这个高度 = "就是同一句话的两种写法"，长短无所谓，直接顶替。
_RECAP_DUP_SIM_HIGH = 0.50
# 中间地带（0.30–0.50）加一道"新说法不许更短"的闸：
# 实测 0.31 一档里混着"同题材但携带不同操作细节"的对子
# （如「每日发布任务 + 完整工作流」被「运营文化观察员角色」这种概括句顶掉，
# 48 小时新闻窗口这类规则就没了）。**丢一条规则不可逆，留一条重复只是不整齐。**
_RECAP_SUPERSEDE_MIN_RATIO = 0.6
# 归一化后短于这个长度就不做"近重复"判定，只认完全相同。
# 理由：2-gram 在短文本上噪声极大 —— "旧的一条" vs "新的一条" 能算出 0.5，
# 而它们毫无关系。真实条目都远长于这个长度，这条护栏不会误伤正常合并。
_RECAP_MIN_DUP_CHARS = 12
_MAX = 20_000


def _recap_norm(text: str) -> str:
    """相似度用归一化：去列表前缀、去「旧值保留备查」标记、去空白与标点。"""
    s = re.sub(r"^[-*]\s+", "", (text or "").strip())
    s = _RECAP_MARK_RE.sub("", s)
    return re.sub(r"[\s，。、；：:;,.\-—～~（）()「」『』“”\"'\[\]【】<>《》…！？!?/|·]+", "", s)


def _recap_sim(a: str, b: str) -> float:
    """离线相似度：去掉标点后的 **中文 2-gram Jaccard**。

    为什么不用 ``manager._entry_sim``（单字 Jaccard）：中文单字重合度天然虚高，
    「同一件事换种说法」和「两件不同的事」拉不开差距（实测都在 0.3–0.7）。
    2-gram 抓的是"词的搭配"，同一事实换个说法仍然共享大量搭配。
    """
    na, nb = _recap_norm(a), _recap_norm(b)
    if len(na) < _RECAP_MIN_DUP_CHARS or len(nb) < _RECAP_MIN_DUP_CHARS:
        return 1.0 if na == nb and na else 0.0
    if len(na) < 2 or len(nb) < 2:
        return 0.0
    ga = {na[i: i + 2] for i in range(len(na) - 1)}
    gb = {nb[i: i + 2] for i in range(len(nb) - 1)}
    return len(ga & gb) / len(ga | gb)


def _recap_supersedes(old: str, new: str) -> bool:
    """``new``（较新的说法）能否把 ``old`` 顶掉。

    三道条件，全部为真才算"同一件事的更新说法"：

    1. 相似度 ≥ ``_RECAP_DUP_SIM``（否则是两件事）；
    2. 若相似度已达 ``_RECAP_DUP_SIM_HIGH`` → 同一句话的两种写法，直接顶；
    3. 中间地带要求 **新说法不更短**（归一化长度 ≥ 旧 × ``_RECAP_SUPERSEDE_MIN_RATIO``）
       —— 挡住"一句概括吃掉一条带细节的规则"。丢一条规则不可逆，留一条重复不整齐。
    """
    sim = _recap_sim(old, new)
    if sim < _RECAP_DUP_SIM:
        return False
    if sim >= _RECAP_DUP_SIM_HIGH:
        return True
    return len(_recap_norm(new)) >= _RECAP_SUPERSEDE_MIN_RATIO * len(_recap_norm(old))


def _recap_spans(text: str) -> list[tuple[int, int]]:
    """所有复盘节的 ``[start, end)`` 区间（按出现顺序）。

    ``end`` 取下一个任意 ``^##`` 标题的起点，或文末。
    """
    heads = list(re.finditer(r"(?m)^##(?!#)[^\n]*", text))
    spans: list[tuple[int, int]] = []
    for i, m in enumerate(heads):
        if _RECAP_HEADING not in m.group(0):
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        spans.append((m.start(), end))
    return spans


def _recap_entries(text: str) -> list[tuple[str, str]]:
    """按文件顺序取出 ``(日期, 条目原文)``；日期取节头里的第一个 ISO 日期。"""
    out: list[tuple[str, str]] = []
    for start, end in _recap_spans(text):
        block = text[start:end]
        head, _, body = block.partition("\n")
        m = _RECAP_DATE_RE.search(head)
        date = m.group(1) if m else ""
        for line in body.splitlines():
            ls = line.strip()
            if not ls:
                continue
            out.append((date, re.sub(r"^[-*]\s+", "", ls).strip()))
    return out


def _render_recap(groups: dict[str, list[str]]) -> str:
    """把 ``{日期: [条目]}`` 渲染回 ``## 自动复盘 · 日期`` 分节（日期升序）。"""
    blocks: list[str] = []
    for date in sorted(groups):
        entries = groups[date]
        if not entries:
            continue
        blocks.append(
            "## %s · %s\n" % (_RECAP_HEADING, date or "未知日期")
            + "\n".join("- %s" % e for e in entries)
        )
    return "\n\n".join(blocks)


def _rebuild_recap(text: str, groups: dict[str, list[str]]) -> str:
    """用 ``groups`` 替换文本里的全部复盘节；其余部分**原样拷贝**。

    逐块走 ``^##`` 下标（不用标题当 key——重复节头正是本 bug 的产物，
    拿标题建 dict 会互相覆盖、漏检）。第一个复盘节的位置渲染一次新内容，
    后面的复盘节整块跳过；非复盘节连同其间的空行逐字节照搬。
    """
    heads = list(re.finditer(r"(?m)^##(?!#)[^\n]*", text))
    if not any(_RECAP_HEADING in m.group(0) for m in heads):
        return text
    out: list[str] = []
    cursor = 0
    rendered = False
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        if _RECAP_HEADING in m.group(0):
            if not rendered:
                out.append(text[cursor: m.start()])
                out.append(_render_recap(groups) + "\n\n")
                rendered = True
        else:
            out.append(text[cursor:end])
        cursor = end
    out.append(text[cursor:])
    return "".join(out).rstrip() + "\n"


def _compact_recap(
    text: str, *, new_lines: Sequence[str] = (), date: str = "",
) -> tuple[str, dict[str, int]]:
    """复盘节归一：**一天一个节 + 跨日语义近重复合并（新者胜）**。

    返回 ``(新文本, 统计)``；文本里没有复盘节、且没有新行时原样返回。

    合并在"整篇复盘"范围内做，条目按 (日期, 原顺序) 排列后**从新到旧**扫描，
    新条目先占位；旧条目若与任何已占位条目近重复，就整条丢弃——它要表达的
    东西已经被更新的说法覆盖了（``（已被更新覆盖，旧值保留备查）`` 那种
    "留旧值备查"恰恰是缺了这一步的现场）。
    """
    entries = _recap_entries(text)
    entries.extend((date, t.strip()) for t in new_lines if str(t or "").strip())
    if not entries:
        return text, {}

    # 从新到旧占位（稳定排序 → 同日期内保持原文件顺序）
    order = sorted(range(len(entries)), key=lambda i: (entries[i][0], i), reverse=True)
    kept_idx: list[int] = []
    merged = 0
    for i in order:
        text_i = entries[i][1]
        dup = False
        for j in kept_idx:
            # 已占位的 j 比 i 新；问"新的能不能顶掉这条旧的"
            if _recap_supersedes(text_i, entries[j][1]):
                dup = True
                break
        if dup:
            merged += 1
            continue
        kept_idx.append(i)
    kept_idx.sort()

    groups: dict[str, list[str]] = {}
    for i in kept_idx:
        d, t = entries[i]
        groups.setdefault(d, []).append(t)

    new_text = _rebuild_recap(text, groups)
    stats = {
        "sections_before": len(_recap_spans(text)),
        "sections_after": len([d for d, v in groups.items() if v]),
        "entries_before": len(entries) - len(new_lines),
        "entries_after": len(kept_idx),
        "merged": merged,
    }
    return new_text, stats


def _trim_global_memory(text: str, limit: int = _MAX, filename: str = "") -> str:
    """超限时的**分节感知**裁剪：先删最旧的复盘条目，绝不切手写正文。

    原实现是 ``new_content[-_MAX:]``——按字符从**头部**无差别切，可能切在句中。
    而 USER.md 的头部正是 ``## 基本信息 / ## 写作观 / ## 查资料铁律``：
    **最先被切掉的恰是最该留的**（现状 8 089 字符未触发，属潜伏事故）。

    现在的顺序：① 删最旧的复盘条目（机器生成、价值随时间递减）
    → ② 仍超限就记 warning 并**保留全文**（宁可超一点，也不能吃掉画像本体）。
    """
    if len(text) <= limit:
        return text
    entries = _recap_entries(text)
    if not entries:
        logger.warning(
            "after_action: %s 超限（%d > %d）且无复盘节可删——保留全文，不切画像",
            filename or "全局记忆", len(text), limit,
        )
        return text
    dropped = 0
    while len(text) > limit and entries:
        # 每轮删一条**最旧**的（日期最小；同日期删最靠前的）
        i = min(range(len(entries)), key=lambda k: (entries[k][0], k))
        d, t = entries.pop(i)
        groups: dict[str, list[str]] = {}
        for dd, tt in entries:
            groups.setdefault(dd, []).append(tt)
        text = _rebuild_recap(text, groups)
        dropped += 1
        if not _recap_spans(text):
            break
    logger.info(
        "after_action: %s 超限，裁掉最旧复盘条目 %d 条（现 %d 字符）",
        filename or "全局记忆", dropped, len(text),
    )
    if len(text) > limit:
        logger.warning(
            "after_action: %s 裁完复盘仍超限（%d > %d）——保留全文，不切画像",
            filename or "全局记忆", len(text), limit,
        )
    return text


def _append_to_global_memory(base_dir: Path, filename: str, new_lines: list[str]) -> int:
    """向全局记忆文件（USER.md / MEMORY.md）追加去重后的要点，原子写 + 有界裁剪。

    返回实际新增条数。重复要点（归一化后相同）不重复追加；超长从最旧裁剪（保留尾部新增）。

    2026-09-21 重写（原实现有两个病）：

    1. **每次都新建节头**。原来无条件 ``existing + "\\n\\n## 自动复盘 · 今天\\n" + 行``，
       同一天多轮复盘就留下多个同名节 —— 实测 3 天攒出 **17 个节**（09-19×6 /
       09-20×6 / 09-21×4），只有 1 行的节比比皆是。
    2. **只做逐字去重**，抓不住"同一件事换个说法"—— ``_norm`` 是字符级归一化，
       措辞一变就当成新事实。于是同一批规则在三天里被写了三遍。

    现在改成：**按日期 upsert（一天一个节）+ 语义近重复合并（新者胜）**。
    合并发生在"整篇复盘节"范围内，跨日期生效——这正是"信念修订"该有的样子：
    新的说法覆盖旧的说法，而不是两条并排留着互相打架。
    """
    if not new_lines:
        return 0
    path = base_dir / filename
    existing = ""
    try:
        if path.is_file():
            existing = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        existing = ""
    seen = set()
    for l in existing.splitlines():
        ls = l.strip()
        if not ls:
            continue
        ls = re.sub(r"^[-*]\s+", "", ls).strip()  # 去子弹前缀再归一化
        seen.add(_norm(ls))
    to_add: list[str] = []
    for t in new_lines:
        ts = t.strip()
        if not ts or _norm(ts) in seen:
            continue
        seen.add(_norm(ts))
        to_add.append(ts)
    if not to_add:
        return 0
    stamp = datetime.now().strftime("%Y-%m-%d")
    if _RECAP_HEAD_RE.search(existing):
        # 已有复盘节：按日期 upsert + 语义近重复合并（一天只留一个节头）
        new_content, stats = _compact_recap(existing, new_lines=to_add, date=stamp)
        if stats.get("merged"):
            logger.info(
                "after_action: 复盘节合并 %d 条近重复（保留较新说法）", stats["merged"]
            )
    else:
        block = "\n\n## 自动复盘 · %s\n" % stamp + "\n".join("- %s" % t for t in to_add)
        new_content = existing + block
    new_content = _trim_global_memory(new_content, filename=filename)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(new_content, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.exception("after_action: 写全局记忆失败 %s", filename)
        return 0
    return len(to_add)


def _maybe_review_memory(base_dir: Path, last_user: str, last_asst: str) -> None:
    """会话后后台记忆复盘：把用户画像/偏好/持久约定蒸馏进 USER.md / MEMORY.md。

    对标 Hermes background_review 的「记忆」半环。零前置 LLM 成本（信号门控）；
    记忆 provider 不可用时跳过；任何异常都不阻断主流程（fail-open）。
    """
    try:
        from deskmate.memory.llm import memory_llm_available, chat_memory

        if not memory_llm_available():
            return
        if not _looks_like_memory_candidate(last_user or "", last_asst or ""):
            return

        digest_parts = [
            d for d in (
                _memory_digest_for_review(base_dir, "USER.md"),
                _memory_digest_for_review(base_dir, "MEMORY.md"),
            ) if d
        ]
        existing_block = (
            "\n已记录（**这些不要重复输出**，换种说法也算重复）：\n"
            + "\n".join(digest_parts) + "\n"
        ) if digest_parts else ""
        prompt = (
            _MEMORY_REVIEW_PROMPT
            + existing_block
            + f"\n对话：\n用户：{last_user[:1500]}\n\n助手：{last_asst[:1500]}"
        )
        try:
            resp = chat_memory(
                "你是记忆复盘器。只输出 JSON，不要解释。",
                prompt,
                temperature=0.2,
                max_tokens=1000,
            )
        except Exception as e:
            logger.debug("after_action: 记忆复盘 LLM 失败: %s", e)
            return
        m = re.search(r"\{.*\}", resp, re.DOTALL)
        if not m:
            return
        try:
            data = json.loads(m.group(0))
        except (json.JSONDecodeError, TypeError):
            return
        updates = data.get("updates") or []
        if not isinstance(updates, list):
            return
        user_lines: list[str] = []
        mem_lines: list[str] = []
        for u in updates:
            if not isinstance(u, dict):
                continue
            text = (u.get("text") or "").strip()
            if not text:
                continue
            tgt = str(u.get("target") or "user").lower()
            if tgt == "memory":
                mem_lines.append(text)
            else:
                user_lines.append(text)
        # 2026-11-11 一段止血：复盘流水改存独立 journal（不再写 USER.md）。
        # USER.md 里的流水在注入时被 strip_auto_review 整段剥掉，等于写了不读；
        # journal 则会在 prefetch 里以「近期复盘」块真正进入上下文。
        from deskmate.memory import journal_log

        added_user = journal_log.append_review(base_dir, user_lines)
        added_mem = _append_to_global_memory(base_dir, "MEMORY.md", mem_lines)
        if added_user or added_mem:
            logger.info(
                "after_action: 记忆复盘 -> journal +%d, MEMORY.md +%d",
                added_user, added_mem,
            )
    except Exception:
        logger.exception("after_action: 记忆复盘失败")


def _maybe_curate_skills(base_dir: Path) -> None:
    """后台定期巡检技能库（curator 自整理）。默认非破坏性：只出报告。

    自动归档是否开启由 curator.auto_archive_enabled() 控制（env
    DESKMATE_CURATOR_AUTO_ARCHIVE），开启后仅归档「从未使用 + 陈旧 + 高重叠」
    的严格低价值技能，且支持 skill_restore 一键还原。
    距上次巡检超过间隔才运行（见 curator._should_run），避免每轮都扫。
    任何异常都不阻断主流程（fail-open）。
    """
    try:
        from deskmate.skills import curator as _curator

        if not _curator._should_run(base_dir):
            return
        result = _curator.curate_skills(base_dir, auto_archive=_curator.auto_archive_enabled())
        logger.info(
            "after_action: 技能巡检 -> 扫描 %d, 合并候选 %d, 归档候选 %d, 报告 %s",
            result.get("total", 0),
            len(result.get("merge_candidates") or []),
            len(result.get("archive_candidates") or []),
            result.get("report_path", ""),
        )
    except Exception:
        logger.exception("after_action: 技能巡检失败")


def _maybe_dream(base_dir: Path) -> None:
    """后台空闲时对「你的记忆」做静默提炼（P2-1 对标 Dreaming）。

    只动 USER.md / MEMORY.md（不碰书稿记忆），零 LLM、幂等、fail-open。
    距上次提炼超过间隔才运行（decay/curator 同款间隔守卫）。
    """
    try:
        from deskmate.memory import dream as _dream

        if not _dream._should_run(base_dir):
            return
        result = _dream.run_dream(base_dir)
        logger.info(
            "after_action: 记忆提炼 -> 去重 %d, 矛盾消解 %d, 超龄迁vault %d, 文件 %s",
            result.get("merged", 0),
            result.get("contradictions_resolved", 0),
            result.get("stale_superseded", 0),
            result.get("files_touched", []),
        )
    except Exception:
        logger.exception("after_action: 记忆提炼失败")


# ---------------------------------------------------------------------------
# P1-5 收尾：写作风格信号每轮自动抽取（零 LLM、纯统计）
# ---------------------------------------------------------------------------

# 保守阈值：过短的聊天消息不抽。每次抽取都会让 metrics.n +1 并参与加权平均，
# 若把「好的 / 继续」这类口语也算一个样本，会稀释真实文风画像（反而害了 P1-5）。
_STYLE_MIN_CHARS_DEFAULT = 200


def _maybe_update_style_profile(last_user: str) -> None:
    """P1-5 收尾：从**用户消息**增量更新写作风格画像（零 LLM、fail-open）。

    ⚠️ 只抽取用户自己的话——助手输出是 AI 的文风，抽了就学反了
    （「越写越懂你的文风」要懂的是**用户**的文风，不是 AI 的）。

    环境变量：
    - ``DESKMATE_STYLE_AUTO=0``       关闭自动抽取（默认开）
    - ``DESKMATE_STYLE_MIN_CHARS=N``  调整最小抽取长度（默认 200 字）
    """
    try:
        if os.environ.get("DESKMATE_STYLE_AUTO", "1") == "0":
            return
        text = (last_user or "").strip()
        try:
            min_chars = int(
                os.environ.get("DESKMATE_STYLE_MIN_CHARS") or _STYLE_MIN_CHARS_DEFAULT
            )
        except ValueError:
            min_chars = _STYLE_MIN_CHARS_DEFAULT
        if len(text) < min_chars:
            return
        from deskmate.style_profile_store import update_style_profile

        # 聊天消息只进「对话桶」：写作侧文风指纹只认书稿桶，避免聊天语感冒充文风
        update_style_profile(
            text,
            source=f"对话-{datetime.now().strftime('%Y-%m-%d')}",
            bucket="chat",
        )
    except Exception:
        logger.debug("after_action: 风格画像增量更新跳过", exc_info=True)


def _maybe_decay_memory(base_dir: str, every_n: int = 25) -> None:
    """P2-11：周期性触发记忆衰减，让「遗忘可见」(traces/forgot.jsonl) 真实发生。

    decay_memory 只在被显式调用时跑（MCP 工具 / cleanup_redundancy），从未进入每轮
    对话流程，导致 traces/forgot.jsonl 从未生成、遗忘对用户不可见。这里按轮数去抖，
    每 every_n 轮跑一次 cleanup_redundancy（其第一步即 decay_memory）。

    2026-11-11 一段止血：原计数器存在函数属性里，进程一重启就归零，开发期频繁
    重启会永远到不了 25 轮——这是 forgot.jsonl 至今为空的直接原因。现在再加一道
    「日期守卫」：同一进程内每 25 轮一次；跨进程每天至少一次。

    环境变量 ``DESKMATE_DECAY_DRY_RUN=1`` 时只返回「将要忘什么」，不落盘。
    零 LLM、fail-open。
    """
    try:
        counter = getattr(_maybe_decay_memory, "_counter", 0) + 1
        _maybe_decay_memory._counter = counter  # type: ignore[attr-defined]
    except Exception:
        counter = 0

    base = Path(base_dir or str(deskmate_base_dir()))
    guard = base / ".decay_guard"
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        last_run = guard.read_text(encoding="utf-8").strip()
    except OSError:
        last_run = ""

    # 轮数去抖与日期守卫任一满足即执行：修掉「重启就永远不跑」。
    if counter % every_n != 0 and last_run == today:
        return

    dry_run = os.environ.get("DESKMATE_DECAY_DRY_RUN", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    try:
        from deskmate.memory.manager import DeskMateMemoryManager

        manager = DeskMateMemoryManager(base)
        if dry_run:
            result = manager.decay_memory(dry_run=True)
            logger.info(
                "after_action: 记忆衰减 dry-run：将归档 %s、删除 %s",
                result.get("archived", 0), result.get("deleted", 0),
            )
        else:
            result = manager.cleanup_redundancy()
            logger.info("after_action: 记忆衰减已执行：%s", result.get("decay", result))
        # 只有真正跑成功才记日期，失败留待下一轮重试；dry-run 不记，
        # 否则当天真实衰减会被日期守卫误跳过。
        if not dry_run:
            try:
                guard.write_text(today, encoding="utf-8")
            except OSError:
                pass
    except Exception:
        logger.warning("after_action: 记忆衰减失败", exc_info=True)


def _maybe_update_decision_profile(last_user: str) -> None:
    """P2-6 收尾：从**用户消息**提取决策偏好（零 LLM、fail-open）。

    与写作风格画像并列——写作风格学"用户怎么写"，决策偏好
    学"用户怎么选"（保守/激进、先A后B、重视X超过Y等）。

    环境变量：
    - ``DESKMATE_DECISION_AUTO=0``  关闭自动提取（默认开）
    - ``DESKMATE_DECISION_MIN_CHARS=N``  最小长度（默认 8 字，短决策句也能捕获）
    """
    try:
        if os.environ.get("DESKMATE_DECISION_AUTO", "1") == "0":
            return
        text = (last_user or "").strip()
        try:
            min_chars = int(os.environ.get("DESKMATE_DECISION_MIN_CHARS") or 8)
        except ValueError:
            min_chars = 8
        if len(text) < min_chars:
            return
        from deskmate.alter_ego.decision_profile import extract_and_update

        extract_and_update(text)
    except Exception:
        logger.debug("after_action: 决策偏好提取跳过", exc_info=True)


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
    base_dir = base_dir or _default_base_dir()

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
            _maybe_review_memory(base_dir, last_user, last_asst)
            _maybe_curate_skills(base_dir)
            _maybe_dream(base_dir)
            # P1-5 收尾：从用户消息学文风（零 LLM 纯统计，快）。
            # 只传 last_user —— 抽助手输出会学到 AI 的文风，正好学反。
            _maybe_update_style_profile(last_user)
            # P2-6 收尾：从用户消息学决策偏好（零 LLM 纯规则，快）。
            # 与文风并列——文风是"怎么写"，决策偏好是"怎么选"。
            _maybe_update_decision_profile(last_user)
            # A：内容侧闭环（产出文本 / 产物文件回流圣经），LLM 抽取较慢，放后台不阻塞响应
            _upsert_content_loop(workspace, last_asst, artifacts)
            # P2-11：定期记忆衰减 → 让「遗忘可见」(traces/forgot.jsonl) 真实产生。
            # decay_memory 本身只在被显式调用时跑，此前从未进入每轮流程，导致遗忘 trace 永远为空。
            # 每 N 轮（含本轮日志写入）跑一次 cleanup_redundancy（内部先调 decay_memory），
            # 零 LLM、fail-open，漏跑不影响主流程。
            _maybe_decay_memory(base_dir)
        Thread(target=_bg, daemon=True).start()

    # ---- 2. 刷新圣经（文件侧，快、同步）----
    try:
        refresh_bible_if_novel(workspace)
    except Exception:
        logger.exception("after_action: 刷新圣经（文件侧）失败")


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
