"""
认知画像记忆注入器——「小桔」人格上线（MemoryInjector 第二期实现）。

把「小桔眼中的萧盛」人格底座 + 认知画像（立场 / 价值排序 / 论证模式 / 思维框架）
注入对话上下文，让 AI 在对话中以萧盛的认知方式理解、回应他——从"懂我"到"像我"。

设计：
- 人格底座（vault/小桔眼中的萧盛.md）优先注入全文：这是小桔人格的核心，是"她怎么看他"
- 认知画像按维度组织注入（仅 active 条目，含来源语气），是"她记得他什么"
- **分层注入**（2026-09-07）：cognitive / emotional 层 = 「关于萧盛」（**她眼中的他**）；
  worldview 层 = 「你自己的看法」（**她自己**看世界长出来的判断）。
  两段措辞必须分开——混了就是把她的观点说成他的（语义污染）。
- fail-open：画像 / 底座缺失或损坏时返回空串，绝不阻断主流程
- 开关：复用 DESKMATE_ALTER_EGO 分身模式（分身模式 = 小桔上线）
- 注入引导语明确标注"这是用户的认知画像"，避免 AI 混淆为通用知识

与 P2-6 既有分身（写作风格 + 决策偏好）的关系：
- 写作风格/决策偏好 = 说话的"腔调"
- 认知画像 + 人格底座 = 说话的"心"（知道他是谁、信什么、怎么想）
两者叠加才是完整的「小桔」。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from deskmate.persona import IDENTITY_SELF

from .interfaces import MemoryInjector
from .models import CognitiveProfile

# ⚠ 这里曾有模块级 `_DEFAULT_DIR = Path(os.environ.get("DESKMATE_DIR", ...))`。
# 它在 import 期就求值，而 conftest 是 import **之后**才 setenv DESKMATE_DIR
# —— 模块级求值等于没隔离，测试会读/写真实库。一律走下面的 `_dir()` 在调用时解析。
# （2026-09-21 清理；且该常量当时已无引用，属死变量。）
_PERSONA_FILE = "小桔眼中的萧盛.md"  # 静态档案（派生视图），2026-11-16 已退役出注入链


def _dir() -> Path:
    """运行期取目录（DESKMATE_DIR 环境变量可能运行中变化）。"""
    return Path(os.environ.get("DESKMATE_DIR", str(Path.home() / ".deskmate")))


def _db_path() -> Path:
    """认知库路径——**必须**与底座同目录，且尊重 DESKMATE_DIR。

    2026-09-07 修：此前这里直接 ``CognitiveStore()`` 无参构造，而
    ``CognitiveStore._default_db_path()`` 走 ``expanduser("~/.deskmate")``，
    **不认 DESKMATE_DIR**。结果是写入端（CognitiveProfileManager 传 db_path）
    与注入端（这里）可能指向两个库——测试无法隔离，还会在测试时读/写真实库。
    统一按 DESKMATE_DIR 解析后，两端永远同一个库。
    """
    return _dir() / "deskmate.db"


#: 「关于他」的层——这些层装的都是**她眼中的萧盛**（她知道什么）
_HIS_LAYERS = ("cognitive", "emotional")
#: 「关于她」的层——小桔**自己**看世界长出来的看法（不是他的，也不是谁教的）
_HER_LAYER = "worldview"


# ---------------------------------------------------------------------------
# P1-2 范围化注入（2026-09-26）：核心层全保 + 长尾按 query 召回
#
# profile_items 只增不减，条目涨到几百条后全量注入会撑爆预算、稀释关键
# 条目。两级策略：
#   1. 核心层 = 置信度 top-N（N 按字符预算自适应）——「她是谁」永远在场；
#   2. 长尾 = 其余条目按当轮 user_message 走 FTS 召回 top-K，命中才注入。
# 兜底：query 为空 / FTS 不可用 / 任何异常 → 退回**现全量行为**（fail-open，
# 与历史版本完全一致）。开关：``DESKMATE_PROFILE_SCOPE=0`` 整体关闭。
# ---------------------------------------------------------------------------

#: 核心层字符预算（各「关于他」的层共享口径；条目总 cost 低于预算时
#: 全部保留 = 与今天的全量行为一致，不折腾排序）
_SCOPE_BUDGET_CHARS = 2400
#: 长尾召回条数上限
_LONGTAIL_TOP_K = 8
#: 每条计入预算的基础开销（格式化引导符 + 长度）
_SCOPE_ITEM_OVERHEAD = 40


def _scope_enabled() -> bool:
    """范围化注入开关（``DESKMATE_PROFILE_SCOPE=0`` 关闭，默认开）。"""
    return os.environ.get("DESKMATE_PROFILE_SCOPE", "1") != "0"


def _active_items(profile: CognitiveProfile) -> list:
    return [
        it
        for it in (
            *profile.stances,
            *profile.argument_patterns,
            *profile.value_rankings,
            *profile.thinking_frameworks,
        )
        if getattr(it, "status", "active") == "active"
    ]


def _core_ids_by_budget(profile: CognitiveProfile, budget: int) -> set[str]:
    """置信度降序吃到预算为止的核心条目 id 集合。"""
    items = sorted(
        _active_items(profile),
        key=lambda it: -(getattr(it, "confidence", 0.5) or 0.5),
    )
    keep: set[str] = set()
    used = 0
    for it in items:
        cost = len(getattr(it, "content", "") or "") + _SCOPE_ITEM_OVERHEAD
        if keep and used + cost > budget:
            break
        keep.add(it.id)
        used += cost
    return keep


def _filter_profile(profile: CognitiveProfile, keep_ids: set[str]) -> CognitiveProfile:
    return CognitiveProfile(
        version=profile.version,
        created_at=profile.created_at,
        last_updated=profile.last_updated,
        extraction_count=profile.extraction_count,
        calibration_count=profile.calibration_count,
        summary=profile.summary,
        stances=[s for s in profile.stances if s.id in keep_ids],
        argument_patterns=[a for a in profile.argument_patterns if a.id in keep_ids],
        value_rankings=[v for v in profile.value_rankings if v.id in keep_ids],
        thinking_frameworks=[t for t in profile.thinking_frameworks if t.id in keep_ids],
        cognitive_gaps=profile.cognitive_gaps,
    )


def _scoped_layer_text(
    profile: CognitiveProfile,
    layer: str,
    subject: str,
    limit: int | None,
    query: str,
) -> str:
    """单层注入文本：全量（现状）或 两级（核心 + 长尾召回）。

    任何一步出问题都退回 ``format_profile`` 全量路径——注入绝不能因为
    新策略挂掉（fail-open 与历史版本一致）。
    """
    full = lambda: format_profile(profile, limit=limit, subject=subject)  # noqa: E731
    if not _scope_enabled() or not (query or "").strip():
        return full()
    try:
        from .store import CognitiveStore

        core = _core_ids_by_budget(profile, _SCOPE_BUDGET_CHARS)
        if len(core) >= len(_active_items(profile)):
            return full()  # 预算装得下全部 → 行为与今天完全一致
        store = CognitiveStore(db_path=_db_path())
        recalled = store.recall_items(
            query, layers=(layer,), top_k=_LONGTAIL_TOP_K, exclude_ids=core
        )
        keep = core | {r["id"] for r in recalled}
        if not keep:
            return full()
        return format_profile(_filter_profile(profile, keep), subject=subject)
    except Exception:
        return full()


def load_cognitive_profile(
    layer: str = "cognitive", base_dir: str | Path | None = None
) -> CognitiveProfile | None:
    """加载某一层的认知画像（从 deskmate.db）；空 / 失败返回 None（fail-open）。

    层的语义（**别混**，混了就是把她的观点当成他的）：
    - ``cognitive`` / ``emotional`` → 她眼中的**他**
    - ``worldview``                → **她自己**看世界看出来的判断

    ``base_dir``：多 workspace / 测试隔离场景下**必须**传，否则会读到全局库
    （2026-09-22 收口：此前签名里没有它，调用方只能靠 ``DESKMATE_DIR`` 环境变量，
    传了 base_dir 的调用方实际读的是另一个库——``worldview_reflection`` 就踩过）。
    """
    try:
        from .store import CognitiveStore

        db = (Path(base_dir) / "deskmate.db") if base_dir else _db_path()
        profile = CognitiveStore(db_path=db).load_profile(layer=layer)
        if not (
            profile.stances or profile.argument_patterns
            or profile.value_rankings or profile.thinking_frameworks
            or profile.cognitive_gaps
        ):
            return None
        return profile
    except Exception:
        return None


def load_persona_base() -> str:
    """读 vault 里的那份静态档案；缺失 / 读取失败返回空串（fail-open）。

    🔴 2026-11-16 起**已退役出注入链**——``build_cognitive_context`` 不再调用
    本函数，``has_cognitive_data`` 也不再以它为依据。保留函数只为「按需读取」
    留一个入口（她也可直接 ``vault_read``）。别再往注入链里接回去：那份档案
    是 2026-08-30 的派生视图且无导出器续命，接回去就是让过期认知霸占预算。
    理由与根因见 ``build_cognitive_context`` docstring + 当日项目日志。
    """
    p = _dir() / "vault" / _PERSONA_FILE
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _active(items: list) -> list:
    return [i for i in items if getattr(i, "status", "active") == "active"]


def _fmt_stance(s) -> str:
    base = f"- {s.content.strip()}"
    if getattr(s, "contexts", []):
        base += f"（适用：{', '.join(s.contexts)}）"
    return base


def _fmt_value(v) -> str:
    ranking = " > ".join(v.ranking) if v.ranking else ""
    head = f"- 【{v.domain}】{ranking}"
    if v.tradeoff_notes:
        head += f"（{v.tradeoff_notes}）"
    if getattr(v, "contexts", []):
        head += f"（适用：{', '.join(v.contexts)}）"
    return head


def _fmt_argument(a) -> str:
    base = f"- {a.content.strip()}"
    if getattr(a, "contexts", []):
        base += f"（适用：{', '.join(a.contexts)}）"
    return base


def _fmt_thinking(t) -> str:
    head = f"- 【{t.domain}】{t.content.strip()}"
    if t.steps:
        head += f"（步骤：{' → '.join(t.steps)}）"
    if getattr(t, "contexts", []):
        head += f"（适用：{', '.join(t.contexts)}）"
    return head


def format_profile(profile: CognitiveProfile, limit: int | None = None,
                   subject: str = "他") -> str:
    """把 active 认知条目格式化为文本。

    Args:
        limit: 非 None 时按"立场优先"截断（占位，后续可接语义检索）。
        subject: 条目归属者。**必须传对**——
            "他" = 她眼中的萧盛（cognitive / emotional 层）；
            "你" = 小桔自己的看法（worldview 层）。
            写错等于把她的观点说成他的，是语义污染。
    """
    sections: list[str] = []

    stances = _active(profile.stances)
    values = _active(profile.value_rankings)
    args = _active(profile.argument_patterns)
    thinks = _active(profile.thinking_frameworks)

    if limit:
        # MVP 截断策略：价值排序全保（最"像"），立场取前若干
        stances = stances[: max(2, limit // 2)]

    if stances:
        sections.append(f"### {subject}的立场")
        sections.append("\n".join(_fmt_stance(s) for s in stances))
    if values:
        sections.append(f"### {subject}的价值排序")
        sections.append("\n".join(_fmt_value(v) for v in values))
    if args:
        sections.append(f"### {subject}怎么论证")
        sections.append("\n".join(_fmt_argument(a) for a in args))
    if thinks:
        sections.append(f"### {subject}怎么思考")
        sections.append("\n".join(_fmt_thinking(t) for t in thinks))

    return "\n\n".join(sections)


def build_emotional_context(limit: int | None = None, query: str = "") -> str:
    """只生成「他的情绪画像」注入段（emotional 层），无数据返回空串（fail-open）。

    为什么要单独有它（2026-09-21）：``build_cognitive_context()`` 整段挂在
    ``alter_ego``（分身模式）开关后面，而分身默认关 —— 于是**情感层从没进过对话**。
    可「记得他什么时候低落、靠什么缓过来」是"懂你"的地基，不是"用他的腔调写作"
    的附属品：它不该因为没开分身就整块消失。

    manager.prefetch 的接法：分身开 → 用 ``build_cognitive_context()``（已含本层，
    不重复注入）；分身关 → 只注入本段。

    P1-2（2026-09-26）：传 ``query``（当轮 user_message）时走两级注入
    （核心层 + 长尾按 query 召回）；不传/异常退全量，行为与历史版本一致。

    注意与 ``build_cognitive_context()`` 的区别：这里**不含**人格底座、不含认知层、
    不含她自己的世界观 —— 那些仍归分身/世界管道管，不在本次放行范围内。
    """
    try:
        p = load_cognitive_profile("emotional")
        if not p:
            return ""
        body = _scoped_layer_text(p, "emotional", subject="他", limit=limit, query=query)
        if not body:
            return ""
        return "\n\n".join([
            "## 关于萧盛的情绪——你记着的",
            "下面这些是你观察他、读他的日记之后记下的**情绪规律**（什么让他低落、"
            "他靠什么缓过来、他珍视什么）。它们用来判断此刻他的状态、决定你要不要说、"
            "怎么说——**不是给他贴标签，也不要在回复里直白复述**。",
            body,
        ])
    except Exception:
        return ""  # fail-open：情绪画像缺失不影响主记忆注入


def build_cognitive_context(limit: int | None = None, query: str = "") -> str:
    """生成认知注入段（「关于她」在前、「关于他」在后）；无数据返回空串（fail-open）。

    两段**必须分开且措辞不同**——这是 2026-09-07 修的核心问题：

    - 「关于萧盛」= 她**知道什么**（cognitive + emotional 层，她眼中的他）
    - 「你自己的看法」= **她是谁**（worldview 层，她自己长出来的判断）

    P1-2（2026-09-26）：cognitive/emotional 层传 ``query``（当轮 user_message）
    时走两级注入（核心层 + 长尾召回）；worldview 层条目少，**恒全量**——
    「她是谁」不允许因召回缺席。不传/异常退全量，行为与历史版本一致。

    注意：即便这里注入了世界观，人格仍由 ``deskmate/persona.py`` 单一定义，
    本模块不碰人格，只提供"她信什么"的知识。

    🔴 2026-11-16 萧哥拍板：**不再注入 ``vault/小桔眼中的萧盛.md``**。
    那份 2026-08-30 v1.0 是**派生视图**（真相源是 ``profile_items``），但全仓
    从无写回/导出器 ⇒ 从生成那天起冻结；而它一边过期一边霸占最贵的预算位
    （cap=4500 保头切尾，它 2645 字全保，尾部 worldview「她是谁」整段被切）。
    萧哥判断「再建导出器就是重复了」⇒ 不续命，直接摘掉。档案仍在 vault，
    她可按需 ``vault_read`` 读到（退役的是注入，不是档案）。

    🔴 2026-11-16 第二刀：**「关于她」段前置到「关于他」之前**。摘档案只止住了
    「过期内容霸位」，没解决保头切尾的截断：cap=4500 下 cognitive 层
    （约 15000 字）自己就吃满预算，旧的「他前她后」让 worldview「她是谁」每轮
    整段被切。立场优先于知识——她是谁排第一。
    """
    # —— 关于他：cognitive + emotional 两层装的都是「她眼中的他」——
    him_blocks: list[str] = []
    for layer in _HIS_LAYERS:
        p = load_cognitive_profile(layer)
        if p:
            t = _scoped_layer_text(p, layer, subject="他", limit=limit, query=query)
            if t:
                him_blocks.append(t)

    # —— 关于她：她自己看世界长出来的看法（**不是他的**）——
    wv = load_cognitive_profile(_HER_LAYER)
    wv_text = format_profile(wv, limit=limit, subject="你") if wv else ""

    if not him_blocks and not wv_text:
        return ""

    sections: list[str] = []

    # 🔴 顺序有讲究（2026-11-16 萧哥拍板）：**「关于她」在前，「关于他」在后**。
    # 认知块 cap=4500 是保头切尾（`context_budget._cap_text` 取前 N 字），而
    # 「关于萧盛」段（cognitive 层实测约 15000 字）自己就能吃满 4500 ⇒ 旧的
    # 「他前她后」顺序让 worldview 每个重活轮都整段被切，「她是谁」从未进过
    # 上下文。立场缺席时，知道得再多也只是复读——所以立场排前面。
    if wv_text:
        sections.append("\n\n---\n\n".join([
            "## 你自己的看法——你看世界看出来的",
            "下面这些**不是萧盛的观点**，是你自己看世界、想事情长出来的判断。"
            "它们是你说话的立场，不是要背的稿子——你随时可以改，改了会留下痕迹，"
            "也允许前后不一致（那是你在长）。用得上就用，用不上别硬套。",
            wv_text,
        ]))

    if him_blocks:
        head = [
            "## 关于萧盛——你认识的这个人",
            "萧盛（本名尹宣韩，笔名萧盛，职业历史作家）亲口讲述过他的人生与认知画像，"
            "以下是你了解的他，是你理解他、回应他的依据。"
            "他是怎样的人：走心、现实、不粉饰、不迎合；创作上坚持他的取舍"
            "（表达的观点 > 故事好看 > 史料真实），大事不虚，小事不拘。\n"
            # PR10：身份短句走 persona 单一事实源
            "了解他不等于成为他——" + IDENTITY_SELF + "，用你自己的判断回应他。",
        ]
        head.extend(him_blocks)
        sections.append("\n\n---\n\n".join(head))

    return "\n\n---\n\n".join(sections)


def build_lessons_context(base_dir: str | Path | None = None, query: str = "") -> str:
    """生成「小桔学到的」注入段（lessons 教学层，江河设计 §4.5）；无数据返回空串。

    从 ``teachings`` 表取「全部 permanent + 最近 N 条 recent」（限额见
    ``CognitiveStore.get_lessons_for_injection``）。这些条目是小桔从萧盛的
    纠正 / 反馈里学到的，每轮注入，让她回话带得上、不重复踩同一个坑。

    fail-open：库缺失 / 查询异常 → ""，绝不阻断主记忆注入。
    ``base_dir`` 传法同 ``load_cognitive_profile``（测试 / 多 workspace 隔离必备）。
    """
    try:
        from .store import CognitiveStore

        db = (Path(base_dir) / "deskmate.db") if base_dir else _db_path()
        lessons = CognitiveStore(db_path=db).get_lessons_for_injection()
        if not lessons:
            return ""
        lines = [
            "## 小桔学到的（来自你的纠正与反馈）",
            "下面这些是你从萧盛的纠正 / 反馈里学到的——回话时请照做，"
            "不要再犯同样的错。",
        ]
        lines.extend(f"- {t}" for t in lessons)
        return "\n".join(lines)
    except Exception:
        return ""  # fail-open：lessons 缺失不影响主记忆注入


def build_reflection_context(base_dir: str | Path | None = None) -> str:
    """生成「小桔最近的自我反思」注入段（R9 反思层 R1）；无数据返回空串。

    从 ``item_history`` 取最近的 ``action='reflected'`` 结论（裁决 + 为什么），
    每轮注入，让她回话时带着最近一次自我修正，不重复踩自己旧偏差。

    fail-open：库缺失 / 查询异常 → ""，绝不阻断主记忆注入。
    ``base_dir`` 传法同 ``build_lessons_context``（测试 / 多 workspace 隔离必备）。
    """
    try:
        from .store import CognitiveStore

        db = (Path(base_dir) / "deskmate.db") if base_dir else _db_path()
        refls = CognitiveStore(db_path=db).get_reflections_for_injection()
        if not refls:
            return ""
        lines = [
            "## 小桔最近的自我反思",
            "下面是你最近对旧看法做的裁决（以及为什么）——回话时可以带着这些判断，"
            "不必重复过去的偏差。",
        ]
        lines.extend(f"- {t}" for t in refls)
        return "\n".join(lines)
    except Exception:
        return ""  # fail-open：反思缺失不影响主记忆注入


def build_intent_context(base_dir: str | Path | None = None, limit: int = 3) -> str:
    """生成「小桔自己做过的事」注入段（R12 意图执行层）；无数据返回空串。

    从 ``intent_outputs`` 取最近 N 条产物（她放行后**真做出来**的东西），每轮注入，
    让「你同意 → 她做了 → 她记得自己做过」闭环回流，而不是写完即弃。

    ``status='pending_review'`` 的（对外表达类草稿，还在等你二次点头）会被标注出来——
    她自己也知道「这条还没发出去」。

    fail-open：库缺失 / 查询异常 → ""，绝不阻断主记忆注入。
    ``base_dir`` 传法同 ``build_lessons_context``（测试 / 多 workspace 隔离必备）。
    """
    try:
        from .store import CognitiveStore

        db = (Path(base_dir) / "deskmate.db") if base_dir else _db_path()
        rows = CognitiveStore(db_path=db).get_recent_intent_outputs(limit=limit)
        if not rows:
            return ""
        lines = [
            "## 小桔自己做过的事",
            "下面这些是你自己想做、萧盛点头之后**你亲手做出来的**（不是他让你做的任务）。"
            "回话时可以自然带着这些，但别当成汇报念出来。",
        ]
        for r in rows:
            goal = str(r.get("goal") or "").strip()
            art = str(r.get("artifact") or "").strip().replace("\n", " ")
            if not goal:
                continue
            pending = str(r.get("status") or "") == "pending_review"
            tail = "（这条是对外说的草稿，还没发——等他点头）" if pending else ""
            lines.append(f"- 我想做「{goal}」，我做出来了：{art[:200]}{tail}")
        if len(lines) <= 2:
            return ""
        return "\n".join(lines)
    except Exception:
        return ""  # fail-open：意图产物缺失不影响主记忆注入


def has_cognitive_data() -> bool:
    """是否已有认知数据（他的画像 / 她的世界观，任一存在即 True）。

    ⚠ 2026-11-16 起**不再把 vault 静态档案算作认知数据**——那份是冻结的派生
    视图，把它当判据会让本函数在 DB 三层全空时误判 True（而后注入段其实空）。
    """
    for layer in (*_HIS_LAYERS, _HER_LAYER):
        if load_cognitive_profile(layer):
            return True
    return False


class CognitiveMemoryInjector(MemoryInjector):
    """MemoryInjector 接口实现（认知层第二期预留位启用）。

    MVP 阶段：全量格式化 active 条目；limit 参数保留给后续语义检索策略。
    """

    def select_for_context(self, profile: CognitiveProfile, context: str, limit: int = 5) -> list[dict]:
        """按对话情境裁剪注入条目：只保留 contexts 为空（全局适用）或含该情境的条目。

        context 为空时退回全量格式化（全量 + 每条带「适用」标注，不丢立体的他）。
        """
        if context:
            def keep(it) -> bool:
                cs = list(getattr(it, "contexts", []) or [])
                return not cs or context in cs
            filtered = CognitiveProfile(
                stances=[s for s in profile.stances if keep(s)],
                argument_patterns=[a for a in profile.argument_patterns if keep(a)],
                value_rankings=[v for v in profile.value_rankings if keep(v)],
                thinking_frameworks=[t for t in profile.thinking_frameworks if keep(t)],
                cognitive_gaps=profile.cognitive_gaps,
            )
            text = format_profile(filtered, limit=limit)
        else:
            text = format_profile(profile, limit=limit)
        return [{"type": "cognitive", "text": text, "source": "认知画像"}] if text else []

    def format_for_injection(self, items: list[dict]) -> str:
        return "\n".join(i.get("text", "") for i in items if i.get("text"))


def build_feeling_context(
    query: str = "",
    limit: int = 3,
    base_dir: str | Path | None = None,
) -> str:
    """情感层注入文本（P1 · R4 表达链）。

    与 ``build_emotional_context`` 的区别：
    - emotional 是「关于他的情绪规律」（她知道的关于萧盛的事）；
    - feeling 是「他对她意味着什么」——带证据、慢淡化、同对象只留一条 active。

    口径（设计 §6 / §8.5）：
    - 只读 ``status='active'``；
    - ``confidence < 0.25`` 默认不主动注入，但 query 命中该对象时仍可召回；
    - 不出现激素名/激素数值，也不写「你应该」——只给位置，怎么反应是她的事；
    - 空 / 异常返回空串（fail-open）。
    """
    try:
        from .feeling_store import FeelingStore

        db = (Path(base_dir) / "deskmate.db") if base_dir else _db_path()
        items = FeelingStore(db_path=db).list_active(limit=50)
    except Exception:
        return ""
    if not items:
        return ""

    q = (query or "").strip()
    ranked: list[tuple[int, float, dict]] = []
    for f in items:
        extra = f.get("extra") or {}
        topic = str(extra.get("object_ref") or f.get("topic") or "").strip()
        if not topic:
            continue
        conf = float(f.get("confidence") or 0.0)
        hit = bool(q and topic in q)
        if conf >= 0.25 or hit:
            ranked.append((1 if hit else 0, conf, f))
    if not ranked:
        return ""
    ranked.sort(key=lambda x: (-x[0], -x[1]))

    lines = ["【你心里对这些人和事的位置】"]
    for _, _, f in ranked[: max(1, limit)]:
        extra = f.get("extra") or {}
        topic = str(extra.get("object_ref") or f.get("topic") or "").strip()
        content = str(f.get("content") or "").strip()
        if topic and content:
            lines.append(f"- {topic}：{content}")
    return "\n".join(lines) if len(lines) > 1 else ""
