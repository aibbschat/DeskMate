"""DeskMateMemoryManager — 参考 Hermes 的 prefetch/sync 架构。

职责：
- 管理 ~/.deskmate/ 下的记忆文件（SOUL.md / MEMORY.md / USER.md / projects/）
- 每轮对话前 prefetch（加载相关记忆到上下文）
- 每轮对话后 sync（异步提取新事实，更新记忆文件）
- 提供 system prompt 组装（人格 + 画像 + 全局记忆 + 项目记忆）
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from .project_memory import (
    format_project_memory,
    list_projects,
    load_project_memory,
    read_project_file,
    write_project_file,
)
from .soul import load_soul, save_soul, reset_soul, evolve_soul
from .session_text import (
    format_session_turn,
    is_system_injected_prompt,
    strip_system_injected_turns,
)
from .user_profile import (
    load_user,
    save_user,
    save_user_ex,
    reset_user,
    demote_unmarked_hard_rules,
    restore_deleted_hard_rules,
)
from .llm import llm_available, chat, chat_memory, memory_llm_available
from .vault_store import VaultStore
from .sensitivity import classify, redact, SENSITIVE  # 8.4 敏感分级
from deskmate.paths import deskmate_base_dir

# 记忆上下文最大字符数（参考 Hermes 的 6000）
MEMORY_CONTEXT_MAX_CHARS = 6_000

# vault 搜索结果最大字符数（占上下文预算的 1/3）
VAULT_CONTEXT_MAX_CHARS = 2_000

# 记忆文件字符限额（参考 Hermes 的 memory_char_limit=50000 / user_char_limit=30000）
# 超过限额时自动触发 LLM 压缩合并
MEMORY_FILE_MAX_CHARS = 20_000  # 从 50K 降到 20K，更早触发压缩
USER_FILE_MAX_CHARS = 15_000  # 从 30K 降到 15K

# 记忆路由规则：超过此长度的内容自动存 vault，MEMORY.md 只保留摘要
MEMORY_ROUTING_THRESHOLD = 200  # 字符数

# 记忆衰减 TTL（天数）：超过此天数的条目自动归档或删除。
#
# ⚠ 契约：**只有登记在本表的标签才会衰减**；其余标签（preference / correction /
#   system / stable / moment …）天然永不衰减。新增标签若希望条目会过期，
#   必须显式登记到这里——否则就是「隐性永久」。
MEMORY_DECAY_TTL = {
    "work": 30,       # 工作进展：30 天后归档到 vault
    "note": 7,        # 笔记：7 天后删除（一次性信息）
    # preference / correction：永不衰减（用户偏好和纠正永久保留）
    # stable：永不衰减（长期事实——运维配置 / 协作模式 / 项目位置 / 硬规则 / 创作设定）
}

# 记忆条目日期前缀正则：兼容 "- " / "* " 列表前缀，中文或英文冒号均可。
#
# ⚠ 踩过的坑：早期只认「紧贴行首」的日期（``^\d{4}-...``），而实际落盘的条目
# 写成 ``- 2026-08-13: ...``（带列表前缀）。于是匹配失败 → 走「无日期，安全
# 起见保留」分支 → **衰减永远不触发**、forgot.jsonl 永不生成。写入端
# ``_ensure_date_prefix`` 用同一套正则，避免再叠加出 ``日期: - 日期: 内容``。
_ENTRY_DATE_RE = re.compile(r"^\s*[-*]?\s*(\d{4}-\d{1,2}-\d{1,2})[:：]")

# 「§ 日记沉淀」分节正则（与 DiaryStore._strip_diary_blocks 的 § 规则同源）。
#
# ⚠ 踩过的坑（2026-09-21）：日记的承载是 DiaryStore（SQLite ``loved_entries``
#   + ``diary_memory.md`` 检索视图），MEMORY.md 本不参与。但
#   ``_load_global_memory()`` 会把 MEMORY.md **拼接**上视图供检索，而
#   ``_save_global_memory()`` 只写 MEMORY.md —— 于是任何
#   load→解析→渲染→保存 的路径（dream 每天跑、append_to_section、衰减、压缩）
#   都会把视图内容**固化**进 MEMORY.md：实测日记块从 1 份长到 3 份，占文件 52%，
#   且 dream 会顺手把日记当普通事实合并（19→18 条）。
#   故在写回前剥离：日记只活在 DiaryStore 与视图里，全局记忆不留副本。
#   （分节头若不叫「日记沉淀」——如摘要块改名后的「日记主题」——不受此影响。）
_DIARY_SECTION_RE = re.compile(r"\n*§\s*日记沉淀\b.*$(?:\n(?!§ ).*)*", re.MULTILINE)


def _strip_diary_sections(content: str) -> str:
    """剥离全局记忆文本里的「§ 日记沉淀」分节，其余内容原样保留。

    无日记分节时**逐字节原样返回**（快路径）——避免在正常保存路径上引入格式漂移。
    """
    if "日记沉淀" not in content:
        return content
    cleaned = _DIARY_SECTION_RE.sub("", content)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() + "\n" if cleaned.strip() else cleaned

# ---------------------------------------------------------------------------
# Bot 群会话隔离（2026-11-07 收口）
# ---------------------------------------------------------------------------
# 🔴 红线：群 bot 的发言**不是**小桔与萧盛的对话。``coworker/agent.py`` 的
#    ``build_engine`` 硬编码挂 ``post_turn_hook=_deskmate_post_turn_hook``，bot
#    engine 全盘继承，曾把「群里刚有人说…」当用户消息、把 bot 发言标成
#    「（小桔本轮回应）」，写进 sessions/、事实抽取、萧盛影响素材池，甚至拿
#    bot 的话去进化小桔的技能（实测 11-07 10:08:52 ``novel-review.md`` 混入
#    bot 原话「换个姿势上班」）。
#    判定只认**群提示词前缀 / 指令语**，不认正文里转述的「群里…」，避免把
#    萧盛本人聊天误判成群会话。命中即整轮跳过全部反向回写。
BOT_GROUP_PROMPT_PREFIXES = ("群里刚有人说", "群内已有发言")
BOT_GROUP_PROMPT_MARKERS = ("请直接以你自己的身份发言",)
_BOT_GROUP_ROUND_RE = re.compile(r"这是第\s*\d+\s*轮接话")

# 四段（pref-02c / pref-04c）：指代无实体。用户说「那一条」「换成哪一种」时，
# 本轮消息里没有任何可检索的实体，单轮检索必然落空。这里把**上一轮用户消息**
# 拼进检索 query（只拼 240 字），让指代有一个可检索的先行词。
# 触发条件：本轮短（<12 字）或含指代/代词；长且无指代的正常问题不做扩展，
# 避免把上一轮的话题噪声带进来。
_ANAPHORA_RE = re.compile(
    r"哪一(?:种|个|条|本|句)|(?:那|这)(?:一)?(?:条|个|件|种|本|句|篇)|它|他们?"
)


def _expand_query(current: str, prev_user: str) -> str:
    """指代扩展：返回用于检索的 query（不改动注入给模型的用户原话）。"""
    cur = (current or "").strip()
    prev = (prev_user or "").strip()
    if not cur or not prev:
        return cur
    if len(cur) >= 12 and not _ANAPHORA_RE.search(cur):
        return cur
    return f"{prev[-240:]} {cur}"


def is_bot_group_prompt(text: str | None) -> bool:
    """判定一段文本是不是 Bot 群会话提示词（纯函数、零副作用、零依赖）。

    供 ``post_session_summary`` 与 ``coworker.agent._deskmate_post_turn_hook``
    共用：命中即整轮不写 sessions、不抽事实、不记萧盛影响、不进化技能。
    """
    if not text or not isinstance(text, str):
        return False
    head = text.strip()[:240]
    if head.startswith(BOT_GROUP_PROMPT_PREFIXES):
        return True
    if any(m in head for m in BOT_GROUP_PROMPT_MARKERS):
        return True
    return bool(_BOT_GROUP_ROUND_RE.search(head))


# 三层记忆分层常量
CORE_TAGS = {"preference", "correction"}  # Core 层：每轮自动注入
RECALL_WINDOW_DAYS = 7                    # Recall 层：7 天内的记忆优先注入

# 溯源 / 遗忘 trace（P0-4 + 8.3）：让「懂」与「忘」都可被看见、可查证
TRACE_DIR_NAME = "traces"
INJECT_TRACE_FILE = "inject.jsonl"     # 每轮注入了哪些记忆（溯源）
FORGOT_TRACE_FILE = "forgot.jsonl"     # 归档/删除了哪些记忆（遗忘可见）
TRACE_MAX_LINES = 2_000                # 单文件行数上限，超出截断（保留最新）

# P0-5 记忆去重：离线近重复相似度阈值（保守，宁可漏判也不误删真实信息；
# 语义级近义重复靠 LLM 在线四操作兜底）。CJK 字符 unigram Jaccard。
_USER_DUP_SIM = 0.85
TRACE_MAX_BYTES = 512 * 1024           # 截断**目标**：截断后必须落回该字节以内
# ⚠ 触发阈值必须与截断目标分离（2026-09-22）：若「超上限即截断、截到刚好不超」，
# 文件会长期贴着上限，于是**几乎每次追加都触发一次全量重写**（滑动窗口的固有毛病）。
# 改成「涨到上限 ×2 才截断、截回上限以内」后，每次重写 512KB 换取 512KB 新数据，
# 摊销比 1:1（最优），且文件体积仍有硬边界。语义：TRACE_MAX_BYTES 是截断目标，
# 文件实际大小在 [上限, 2×上限) 之间浮动。
TRACE_TRUNCATE_FACTOR = 2

# P0-6 记忆分层净化 + 时间感知
# 书稿/项目内容关键词（保守匹配，命中才判定为「书的记忆」，避免误迁「关于你」的事实）。
_BOOK_CONTENT_PATTERNS = (
    r"《[^》]+》",                                   # 书名号包裹的作品名
    r"第[一二三四五六七八九十百千\d]+章",               # 具体章节
    r"大纲", r"人物设定", r"人物表", r"设定考证", r"世界观",
    r"书稿", r"小说正文", r"章节", r"角色",
    r"情节", r"叙事", r"暗线", r"单元剧", r"定稿",
)
_BOOK_CONTENT_RE = re.compile("|".join(_BOOK_CONTENT_PATTERNS))

# 「创作信号」= 上表**去掉「仅出现书名号」那一条**。
#
# ⚠ 为什么不直接复用 `_BOOK_CONTENT_RE`（宽口径）：宽口径里有一条「出现书名号即算」，
#   而《…》在日常记忆里俯拾皆是——读书记录（读完《兰陵王》）、被书名号包住的
#   文章标题（《第一章顺顺当当，第二章直接卡死？…》）。把它们卷进创作归属，
#   等于把「读别人的书」污染进「写自己的书」的档案，正是串书禁令要拦的事。
_MANUSCRIPT_SIGNAL_RE = re.compile(
    "|".join(p for p in _BOOK_CONTENT_PATTERNS if p != r"《[^》]+》")
)

# 「读书」信号：**读**字贴着书名号（前后皆可）→ 这条讲的是「读别人的书」。
#
# ⚠ 要求「贴着书名号」而不是见到「读」就拦：创作侧同样会说
#   「读完第五章，开始写第六章」——那种不该拦。贴着书名号才区分得开。
_READING_SIGNAL_RE = re.compile(r"读[完了过]?\s*《|《[^》]{1,20}》\s*读[完了过]")

# 「运营」信号：小桔的日常运营/开发事项（AI圈发帖敲钟、擂台辩题、写作营文章、
# 网文观察日报、技能包版本、知识库接口…）。
#
# ⚠ 这些才是 `#work` 本该管的「一时工作进展」。它们句子里常夹着「第一章」
#   「章纲」「定稿」之类字样，不加这道闸就会被创作规则误收进 §创作，
#   于是「运营流水账」永久占据常驻记忆——修一个坑挖另一个坑。
_OPERATION_SIGNAL_RE = re.compile(
    r"写作营|日报|文章|广场|敲钟|技能包|擂台|辩题|发帖|选题|知识库|目录树|MCP|AI圈"
)

# 创作内容的归属分节：**永不衰减**（挂 `#stable`）。
#
# ⚠ 为什么是「一个 § 创作」，而不是按书名一册一节（原方案，2026-09-26 实测否掉）：
#   按书名分节要求**可靠的书名**，而真实语料里做不到——
#     · 「《国子监秘闻录》第五章《屯田策》…」按出现次数取书名会取到「屯田策」
#       （章节名比书名出现频繁），整批归错书；
#     · 当时 §工作 里 39 条书稿记忆中 **33 条压根没写书名**（「章太聪革职被捕」
#       这类纯散文），任何内容判据都抓不住。
#   猜错书的代价比不分组大得多（跨书混住 = 串书禁令那类事故），故退成单节：
#   先把「不丢、不衰减」坐实；精确到哪本书留给丙——等 app 层接线后能
#   **被告知**当前作品，而不是靠猜。
CREATION_SECTION = "创作"

# 无法判定归属时的兜底分节：**必须是不衰减的分节**。
#
# ⚠ 旧兜底是「笔记 #note」= 7 天后**直接删除且不落 vault**（不可恢复）。
#   而这条兜底恰恰在 LLM 不可用 / 超时 / 返回格式跑偏时生效——即
#   「最不确定的环境下做了最不可逆的动作」。改为落「长期事实 #stable」，
#   与「无日期条目一律保留」同一原则：宁留不误删；体量由 _compress_if_needed 兜。
_FALLBACK_SECTION = "长期事实"

# 时间感知：全局记忆条目日期前缀超过该天数视为「陈旧」（历史事实而非当前状态）。
_STALE_DAYS = 180
# 未检测到具体书名、也无 active_project 时的兜底项目名（书稿记忆落地处）。
_PROJECT_DEFAULT_NAME = "创作"

# § 分节标题 ↔ #标签 的规范映射（用于自动归并）
_TITLE_TO_TAG = {
    "喜好": "preference",
    "纠正": "correction",
    "工作": "work",
    "笔记": "note",
    # 长期事实（永不衰减）：给「项目位置 / 运维配置 / 协作模式 / 硬规则 / 创作设定」
    # 一个显式归属。此前这类内容只能挤在「工作 #work」里，被 30 天 TTL 误淘汰。
    "长期事实": "stable",
}
_TAG_TO_TITLE = {v: k for k, v in _TITLE_TO_TAG.items()}


def _default_base_dir() -> Path:
    """默认记忆目录：~/.deskmate（尊重 DESKMATE_DIR / DESKMATE_MEMORY_DIR）"""
    return deskmate_base_dir()


class WriteResult(str):
    """``append_to_section`` 的返回值。

    仍然是 ``str``（日志/测试/MCP 文案一字不变），但带机器可读状态，调用方
    不必再靠 ``"被拒" in result`` / ``"跳过" in result`` 判中文文案。
    """

    ok: bool
    skipped: bool

    def __new__(cls, message: str, *, ok: bool = True, skipped: bool = False):
        obj = super().__new__(cls, message)
        obj.ok = ok
        obj.skipped = skipped
        return obj


class DeskMateMemoryManager:
    """记忆管理器，参考 Hermes MemoryManager 设计。"""

    def __init__(self, base_dir: Path | str | None = None, active_project: str | None = None, vault_dir: Path | str | None = None):
        self.base_dir = Path(base_dir) if base_dir else _default_base_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.active_project = active_project
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deskmate-mem")
        # VaultStore：Obsidian vault 作为长期记忆存储层
        vault_path = vault_dir or self.base_dir / "vault"
        self.vault = VaultStore(vault_dir=vault_path)

    # ------------------------------------------------------------------
    # System Prompt 组装（参考 Hermes build_system_prompt）
    # ------------------------------------------------------------------

    def build_system_prompt(self, user_message: str = "") -> str:
        """组装 system prompt：SOUL.md + USER.md + MEMORY.md + 项目记忆。

        P1-B：USER.md / MEMORY.md 在**读侧**按当前书过滤（文件本体绝不改写），
        当前书按每轮解析，不写进程级 active_project。异常一律 fail-open 用原文。
        """
        parts: list[str] = []
        _known: list[str] = []
        _book = ""
        try:
            from deskmate.memory import book_scope

            _known = book_scope.known_books(self.base_dir)
            _book = book_scope.resolve_turn_book(
                self.base_dir, user_message,
                active_project=self.active_project, known=_known,
            )
        except Exception:
            pass  # fail-open：书维度不可用就按旧行为整段注入

        def _scoped(text: str, fn: str) -> str:
            try:
                from deskmate.memory import book_scope

                return getattr(book_scope, fn)(text, _book, _known)
            except Exception:
                return text

        # 1. AI 人格
        soul = load_soul(self.base_dir)
        if soul:
            parts.append(soul)

        # 2. 用户画像
        user = load_user(self.base_dir)
        if user:
            parts.append(_scoped(user, "filter_user_profile_by_book"))

        # 3. 全局记忆
        memory = self._load_global_memory()
        if memory:
            parts.append(_scoped(memory, "filter_memory_sections_by_book"))

        # 4. 项目记忆（当前书优先，active_project 兜底）
        active = _book or self.active_project
        if active:
            proj_mem = format_project_memory(self.base_dir, active)
            if proj_mem:
                parts.append(proj_mem)

        # 5. 亲密度语气提示（成长系统）
        try:
            from deskmate.growth.engine import GrowthEngine

            tone = GrowthEngine(base_dir=self.base_dir).modulate_tone_prompt()
            if tone:
                parts.append(f"## 与用户的关系\n{tone}")
        except Exception as e:
            logger.debug("growth engine tone prompt failed: %s", e)

        return "\n\n---\n\n".join(parts)

    # ------------------------------------------------------------------
    # Prefetch（每轮对话前，参考 Hermes prefetch_all）
    # ------------------------------------------------------------------

    @staticmethod
    def _cap_layer(layer: str, budget: int) -> str:
        """每层记忆按预算硬截断；被截断时追加省略号，提示模型内容有省略。

        与文件内其它截断点保持一致（text[:N] + "\\n…"）。
        """
        if len(layer) > budget:
            return layer[:budget] + "\n…"
        return layer

    def prefetch(
        self, user_message: str, *, book: str | None = None,
        skip_archival: bool = False,
    ) -> str:
        """每轮对话前：检索相关记忆，返回 <memory-context> 标签包裹的内容。

        三层记忆架构：
        1. Core（热）— preference/correction，每轮无条件注入（占 1/3 预算）
        2. Recall（温）— 7 天内的近期记忆，按关键词匹配优先注入（占 1/3 预算）
        3. Archival（冷）— vault 长期知识 + 旧记忆，按关键词匹配（占 1/3 预算）

        ``skip_archival=True``：三段文件索引健康时，Recall/Global/vault 由
        ``file_search`` 的融合检索块统一负责，这里只保留 Core 与项目记忆，
        避免同一批记忆被关键词和向量各注入一遍、挤占预算。索引不可用时
        保持旧行为（fail-open）。
        """
        context_parts: list[str] = []
        budget_per_layer = MEMORY_CONTEXT_MAX_CHARS // 3

        # 默契度调制记忆预算：默契越高 → 注入越多（AI 更懂你要什么）
        try:
            from deskmate.growth.engine import GrowthEngine

            modulated_budget = GrowthEngine(base_dir=self.base_dir).modulate_memory_budget(
                MEMORY_CONTEXT_MAX_CHARS
            )
        except Exception as e:
            logger.debug("growth engine memory budget modulation failed: %s", e)
            modulated_budget = MEMORY_CONTEXT_MAX_CHARS
        budget_per_layer = modulated_budget // 3

        # 1. Core 层：偏好和纠正，每轮无条件注入
        core_mem = self._get_core_memory()
        if core_mem:
            context_parts.append(self._cap_layer(core_mem, budget_per_layer))

        # 2. Recall 层：7 天内的近期记忆，关键词匹配优先
        recall_section_ids: set[str] = set()
        if not skip_archival:
            recall_mem, recall_section_ids = self._search_recent_memory(user_message)
            if recall_mem:
                context_parts.append(self._cap_layer(recall_mem, budget_per_layer))

            # 3. 全局记忆关键词匹配（补充 Recall 未覆盖的，排除 Core + Recall 已覆盖的 section）
            global_mem = self._search_global_memory(user_message, exclude_tags=CORE_TAGS, exclude_section_ids=recall_section_ids)
            if global_mem:
                context_parts.append(self._cap_layer(global_mem, budget_per_layer))

            # 4. Archival 层：vault 长期知识
            vault_mem = self._search_vault(user_message)
            if vault_mem:
                context_parts.append(self._cap_layer(vault_mem, budget_per_layer))

        # 5. 项目记忆（P1-B：按轮解析的当前书优先，active_project 兜底；
        #    过去应用层从不 set_active_project，这里等于把项目记忆接回正路）
        active = book or self.active_project
        if active:
            proj_mem = self._search_project_memory(user_message, active)
            if proj_mem:
                context_parts.append(proj_mem)

        if not context_parts:
            return ""

        combined = "\n\n".join(context_parts)
        # 限制总长度（与默契度调制后的预算一致）
        if len(combined) > modulated_budget:
            combined = combined[:modulated_budget] + "\n…"

        return f"<memory-context>\n{combined}\n</memory-context>"

    # ------------------------------------------------------------------
    # 溯源 / 遗忘 trace（P0-4 + 8.3）
    # ------------------------------------------------------------------

    def _trace_dir(self) -> Path:
        d = self.base_dir / TRACE_DIR_NAME
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _append_trace(self, filename: str, record: dict) -> None:
        """追加一行 trace 记录；超出上限时截断保留最新。

        ⚠ 双阈值口径（2026-09-22 修）：原实现的条件按**字节**判
        （``size > TRACE_MAX_BYTES``），截断却只保留最后 ``TRACE_MAX_LINES`` **行**——
        两个口径打架。当单行平均长度 > 512KB / 2000（inject.jsonl 实测
        2.72MB / 2000 行 ≈ 1.4KB 每行）时，截断完仍远超字节上限 → 条件
        **每次都为真** → 每次 append 都全量读 + 全量重写 2.72MB
        （写放大 ~1900 倍，纯白烧磁盘 IO，而且是在每轮对话的热路径上）。

        修法：以**字节**为截断目标，从头部按整行丢弃直到落进目标以内
        （行数上限只作辅助约束，先粗剪再按字节精剪）。并且**触发阈值 ≠ 截断目标**
        ——超出 ``TRACE_MAX_BYTES × TRACE_TRUNCATE_FACTOR`` 才截断，截回目标以内，
        否则文件会贴着上限导致每次追加都全量重写（摊销比退化为 1:N）。
        单行自身就超限时至少保留该行（不写空文件）。
        """
        try:
            from ._fs_lock import file_write_lock

            path = self._trace_dir() / filename
            # ⚠ 整段持锁（2026-09-23 补）：append 与「超限截断」是同一文件的
            # 读-改-写，若不互斥，两个入口可能交错（一行被拆成两半）或截断时
            # 读到对方刚 append 的行导致计数错位。锁文件与数据同目录（见 _fs_lock）。
            with file_write_lock(path):
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                if path.stat().st_size <= TRACE_MAX_BYTES * TRACE_TRUNCATE_FACTOR:
                    return
                lines = path.read_text(encoding="utf-8").splitlines()
                # ① 行数粗剪：只保留最新 TRACE_MAX_LINES 行
                if len(lines) > TRACE_MAX_LINES:
                    lines = lines[-TRACE_MAX_LINES:]
                # ② 字节精剪：从尾部（最新）往前累积，超限即止
                kept: list[str] = []
                size = 0
                for ln in reversed(lines):
                    ln_bytes = len(ln.encode("utf-8")) + 1  # +1 = 换行符
                    if kept and size + ln_bytes > TRACE_MAX_BYTES:
                        break
                    kept.append(ln)
                    size += ln_bytes
                kept.reverse()
                path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        except Exception as e:
            logger.debug("trace write failed (%s): %s", filename, e)

    def read_trace(self, filename: str, limit: int = 20) -> list[dict]:
        """读取最近 limit 条 trace 记录（供溯源 / 遗忘可见查询）。"""
        try:
            path = self._trace_dir() / filename
            if not path.exists():
                return []
            lines = path.read_text(encoding="utf-8").splitlines()
            out = []
            for ln in lines[-limit:]:
                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
            return out
        except Exception as e:
            logger.debug("trace read failed (%s): %s", filename, e)
            return []

    @staticmethod
    def _entry_fingerprint(entry: str) -> str:
        """记忆条目稳定指纹：sha1(原文) 前 8 位，用于溯源定位。"""
        return hashlib.sha1(entry.encode("utf-8")).hexdigest()[:8]

    def _annotate_memory_blocks(self, text: str) -> tuple[str, list[dict]]:
        """给注入文本里的记忆条目打 〔记忆#fp〕 溯源标注，并返回 refs 清单。

        仅处理以 § 开头的分节块（USER 画像等非分节文本保持原样），条目按空行切分。
        标注极短、附在条目开头，不改变条目标记内容；fp 供溯源 / 前端高亮。
        """
        if not text or "§" not in text:
            return text, []
        lines = text.splitlines()
        out: list[str] = []
        refs: list[dict] = []
        current_block: list[str] = []

        def flush() -> None:
            if not current_block:
                return
            if current_block[0].startswith("§"):
                header = current_block[0]
                body = "\n".join(current_block[1:])
                entries = [e.strip() for e in re.split(r"\n\s*\n", body) if e.strip()]
                marked: list[str] = []
                for e in entries:
                    if not e.strip() or e.startswith("〔记忆#"):
                        marked.append(e)
                        continue
                    fp = self._entry_fingerprint(e)
                    m = re.match(r"(\d{4}-\d{1,2}-\d{1,2})", e)
                    date = m.group(1) if m else ""
                    level, _ = classify(e)
                    sensitive = level >= SENSITIVE
                    # 8.4：敏感条目的溯源摘要脱敏，绝不把敏感原文写进日志
                    summary = redact(e[:80]) if sensitive else e[:80]
                    refs.append({
                        "fp": fp,
                        "section": header[1:].strip(),
                        "date": date,
                        "summary": summary,
                        "sensitive": sensitive,
                    })
                    marked.append(f"〔记忆#{fp}〕{e}")
                out.append(header + ("\n" + "\n\n".join(marked) if marked else ""))
            else:
                out.append("\n".join(current_block))
            current_block.clear()

        for ln in lines:
            if ln.startswith("§"):
                flush()
                current_block = [ln]
            elif ln.strip() == "---":
                flush()
                out.append("---")
            else:
                current_block.append(ln)
        flush()
        return "\n".join(out), refs

    def _get_core_memory(self) -> str:
        """提取 Core 层：preference 和 correction sections，每轮无条件注入。"""
        memory = self._load_global_memory()
        if not memory:
            return ""
        _, sections = self._parse_sections(memory)
        core_sections = [s for s in sections if s["tags"] & CORE_TAGS]
        if not core_sections:
            return ""
        parts = [f"§ {s['header_rest']}\n{s['body']}" for s in core_sections]
        return "\n\n---\n\n".join(parts)

    @staticmethod
    def _extract_keywords(query: str) -> list[str]:
        """中文友好的关键词提取。

        相比裸 re.split，额外识别《》【】""''：；等中文标点（书名号/括号/引号），
        并对长度 > 2 的中文词补 CJK 2-gram，使「《兰陵王传》」这类整串查询也能
        命中记忆正文里的「兰陵王」。与 vault_store.search 的 bigram 逻辑保持一致。
        """
        tokens = [
            w
            for w in re.split(r"[\s，。、？！,.?!\-《》【】“”‘’：；]+", query)
            if len(w) >= 2
        ][:6]
        keywords = list(tokens)
        for tok in tokens:
            if len(tok) > 2 and re.search(r"[\u4e00-\u9fff]", tok):
                for i in range(len(tok) - 1):
                    bg = tok[i : i + 2]
                    if bg not in keywords:
                        keywords.append(bg)
        # P2-2：别名扩展（年号↔朝代桥接），封堵隐式查询缺口（文档 P08）
        try:
            from deskmate.memory.query_expand import expand_keywords

            keywords = expand_keywords(keywords)
        except Exception:
            pass
        return keywords[:20]

    def _search_recent_memory(self, query: str) -> tuple[str, set[str]]:
        """Recall 层：检索 7 天内的近期记忆，按关键词匹配。

        返回 (结果文本, 已匹配的 section id 集合)，供 prefetch 排除已覆盖的 section。
        """
        import datetime

        memory = self._load_global_memory()
        if not memory:
            return "", set()

        _, sections = self._parse_sections(memory)
        today = datetime.date.today()
        keywords = self._extract_keywords(query)

        recent_matches: list[str] = []
        matched_ids: set[str] = set()
        for s in sections:
            # Core 层已单独注入，跳过
            if s["tags"] & CORE_TAGS:
                continue
            entries = [e.strip() for e in re.split(r"\n\s*\n", s["body"]) if e.strip()]
            recent_entries = []
            for entry in entries:
                # 检查是否在 Recall 窗口内
                m = re.match(r"(\d{4}-\d{1,2}-\d{1,2})[:：]", entry)
                if m:
                    try:
                        entry_date = datetime.date.fromisoformat(m.group(1))
                        if (today - entry_date).days > RECALL_WINDOW_DAYS:
                            continue  # 超出 Recall 窗口
                    except ValueError:
                        pass  # 无法解析日期的条目保留
                # 关键词匹配
                if keywords and not any(kw.lower() in entry.lower() for kw in keywords):
                    continue
                recent_entries.append(entry)
            if recent_entries:
                recent_matches.append(f"§ {s['header_rest']}\n" + "\n\n".join(recent_entries))
                matched_ids.add(s["id"])

        if not recent_matches:
            return "", set()
        result = "\n\n---\n\n".join(recent_matches)
        if len(result) > 4000:
            result = result[:4000] + "\n…"
        return result, matched_ids

    def build_full_context_for_turn(self, user_message: str = "", *, prev_user: str = "") -> str:
        """构造每轮对话要注入的「完整记忆上下文」（prefetch 相关片段 + 完整 USER 画像）。

        用于「每轮自动注入记忆」的 opt-in 能力（DESKMATE_AUTO_INJECT）。
        返回 <memory-context> 包裹内容；无记忆返回空串。

        P0-4 / 8.3 增强（向后兼容）：
        - 记忆条目加 〔记忆#fp〕 溯源标注（fp 稳定可回溯）
        - 尾部附 <memory-references> 摘要块：指引 LLM 自然带出「因为你之前说过……」引用，
          同时给出溯源标识；并把本轮注入了哪些记忆 id 追加到 traces/inject.jsonl。

        注意：技能加载不在引擎层强制注入，而是通过系统 prompt 中的技能索引，
        让 LLM 自觉判断何时调用 skill_view 加载。
        """
        from deskmate.memory.context_budget import ContextBlock, strip_auto_review

        # 2026-11-11：USER.md 健康监测（旁路，节流 5 分钟一次）。直接读文件判断是否
        # 被异常覆写（即使写入绕过了 save_user 也能发现），异常写 alerts.log + 留存快照。
        try:
            from deskmate.memory.user_profile import check_user_file_health

            check_user_file_health(self.base_dir)
        except Exception:  # noqa: BLE001 - 监测失败不影响本轮
            pass

        # 2026-11-10：每轮注入按 token 预算裁剪 + 闲聊轮跳重块（见 context_budget.py）。
        # blocks 记录块名/优先级/上限；最后统一 render，fail-open 退回旧行为。
        blocks: list[ContextBlock] = []

        def _add(
            name: str,
            text: str,
            *,
            priority: int = 50,
            cap: int = 4000,
            cap_light: int | None = None,
            keep_light: bool = True,
        ) -> None:
            if text and str(text).strip():
                blocks.append(
                    ContextBlock(
                        name=name,
                        text=str(text).strip(),
                        priority=priority,
                        cap=cap,
                        cap_light=cap_light,
                        keep_light=keep_light,
                    )
                )

        # P1-B 当前书：每轮解析一次，让 MEMORY/USER 读侧隔离、项目记忆、
        # user_fact_context 共用同一书维度（避免三处各推一次、结论打架）。
        # 不写 self.active_project：云端多会话共享 manager，进程级状态会串味。
        _known: list[str] = []
        _book = ""
        try:
            from deskmate.memory import book_scope

            _known = book_scope.known_books(self.base_dir)
            _book = book_scope.resolve_turn_book(
                self.base_dir, user_message,
                active_project=self.active_project, known=_known,
            )
        except Exception:
            pass  # fail-open：书维度不可用就退回旧行为

        def _scoped(text: str, fn: str) -> str:
            try:
                from deskmate.memory import book_scope

                return getattr(book_scope, fn)(text, _book, _known)
            except Exception:
                return text  # fail-open：绝不因隔离逻辑丢掉整块记忆

        # 三段：文件索引健康时，Recall/Global/vault 交给 file_search 统一检索，
        # prefetch 只保留 Core 与项目记忆（避免重复注入）。
        _file_index_on = os.getenv("DESKMATE_FILE_INDEX", "1") != "0"
        _index_ok = False
        if _file_index_on:
            try:
                from deskmate.memory.file_search import index_healthy

                _index_ok = index_healthy(self.base_dir)
            except Exception:
                _index_ok = False
        relevant = self.prefetch(user_message, book=_book, skip_archival=_index_ok)
        if relevant:
            inner = relevant.strip()
            if inner.startswith("<memory-context>"):
                inner = inner[len("<memory-context>"):]
            if inner.endswith("</memory-context>"):
                inner = inner[:-len("</memory-context>")]
            inner = inner.strip()
            if inner:
                inner = _scoped(inner, "filter_memory_sections_by_book")
                # 记忆检索块：写作轮必带；闲聊轮跳过（省 prefill）
                _add("prefetch", inner, priority=35, cap=4500, keep_light=False)
        # 三段：记忆文件层融合检索（关键词 + 向量 + 锚）。索引健康时它是
        # Recall/Global/diary/vault/偏好 的统一入口；失败/索引缺失则整块跳过，
        # 回落到上面的旧 prefetch 路径（DESKMATE_FILE_INDEX=0 可强制回滚）。
        if _file_index_on and _index_ok:
            try:
                from deskmate.memory.file_search import (
                    backstop_chunks,
                    format_hits,
                    gate_hits,
                    is_chitchat,
                    search_memory_files,
                )

                # 四段：闲聊门 + 相关性闸。寒暄轮不做语义检索（只能塞噪声），
                # 其余轮把 RRF 尾部的弱相关条目挡掉，最多注入 6 条。
                _q = _expand_query(user_message, prev_user)
                if is_chitchat(user_message):
                    _hits = []
                else:
                    _hits = gate_hits(search_memory_files(
                        _q, k=8, budget_chars=1600, base=self.base_dir
                    ))
                if _hits:
                    # 按当前书隔离：把召回结果包成一个临时 § 节交给既有过滤器，
                    # 逐行剔除「只提别的书」的条目（跨书对照/通用条目保留）。
                    _lines = [
                        f"- [{h['source_path']} · {h['section']}] {h['content']}"
                        for h in _hits
                    ]
                    _wrapped = "§ 检索召回\n" + "\n".join(_lines)
                    _filtered = _scoped(_wrapped, "filter_memory_sections_by_book")
                    _body = "\n".join(
                        ln for ln in _filtered.splitlines()
                        if ln.strip().startswith("- ")
                    )
                    if _body:
                        _add("file_recall",
                             "## 相关记忆（按本轮问题临时检索）\n\n" + _body,
                             priority=34, cap=1200, keep_light=False)
                # 兜底：没有命中，或本轮只是短指令/寒暄（<6 字）时，
                # 取 3 条/400 字的高价值底稿——不走向量，不挤占预算。
                if not _hits or len((user_message or "").strip()) < 6:
                    _bs = backstop_chunks(self.base_dir, limit=3, max_chars=400)
                    if _bs:
                        _add("memory_backstop",
                             format_hits(_bs, "## 兜底记忆（未主动检索时保底）"),
                             priority=60, cap=400)
            except Exception as exc:
                # 2026-11-11 复检修：这里原来静默 pass，但上面的 prefetch 已经用
                # skip_archival=_index_ok 跳过了 Recall/Global/vault，新路径再一
                # 抛错整轮就全丢且零告警。现在显式回落旧 prefetch（含归档层），
                # 再补一次 backstop；仍然 fail-open，但不再静默。
                logger.warning(
                    "文件层检索失败，回落到旧 prefetch（含 Recall/Global/vault）：%s",
                    exc, exc_info=True,
                )
                try:
                    _fb = self.prefetch(user_message, book=_book, skip_archival=False)
                    _inner = (_fb or "").strip()
                    if _inner.startswith("<memory-context>"):
                        _inner = _inner[len("<memory-context>"):]
                    if _inner.endswith("</memory-context>"):
                        _inner = _inner[:-len("</memory-context>")]
                    _inner = _inner.strip()
                    if _inner:
                        _inner = _scoped(_inner, "filter_memory_sections_by_book")
                        blocks[:] = [b for b in blocks if b.name != "prefetch"]
                        _add("prefetch", _inner, priority=35, cap=4500,
                             keep_light=False)
                except Exception as exc2:  # noqa: BLE001
                    logger.warning("回落 prefetch 也失败：%s", exc2)
                try:
                    from deskmate.memory.file_search import (
                        backstop_chunks as _bc,
                        format_hits as _fh,
                    )

                    _bs = _bc(self.base_dir, limit=3, max_chars=400)
                    if _bs:
                        _add("memory_backstop",
                             _fh(_bs, "## 兜底记忆（检索异常时保底）"),
                             priority=60, cap=400)
                except Exception:  # noqa: BLE001
                    pass
        user = load_user(self.base_dir)
        if user and user.strip():
            user = _scoped(user, "filter_user_profile_by_book")
            # 2026-11-11 C 方案：USER.md 分成两层——「## 硬规矩」常驻（条目前置）、
            # 「## 偏好与事实」不常驻（由 preference_hits 按需召回）。strip 把后者
            # 连「自动复盘」一起剥掉，得到的就是「硬规矩 + 稳定画像」。
            # 上限按实测重定：注入块 4,425 字符（硬规矩 1,769 + 稳定画像 2,638 + 标题），
            # cap=5,100（×1.15，留约 675 字符≈10 条短规矩的余量）；旧值 6000 是按
            # 拆分前 12.8K 字符的旧结构拍的，已无依据。轻量轮只保硬规矩与身份。
            _add(
                "user_profile",
                f"## 用户画像\n{strip_auto_review(user.strip())}",
                priority=40,
                cap=5100,
                cap_light=3000,
            )
        # 2026-11-11 一段止血：复盘流水已改存独立 journal（不再写 USER.md），
        # 这里把它真正读回上下文——只取最近 7 天、每天最多 4 条、总长 ≤1200，
        # 避免「换个体积更大的包袱」。迁移历史条目带 [归档] 前缀，digest 跳过。
        try:
            from deskmate.memory import journal_log

            digest = journal_log.recent_digest(
                self.base_dir, days=7, max_chars=1200, max_items_per_day=4
            )
            if digest:
                _add("journal_digest", digest, priority=42, cap=1400, cap_light=900)
        except Exception:
            pass  # fail-open：journal 缺失不影响主记忆注入
        # 2026-11-11 C 方案：非常驻偏好层按本轮关键词少量召回。
        # 三段索引健康时，偏好已包含在 file_recall 的 user chunk 里，这里不再
        # 重复注入；只有索引不可用（回滚 / 未建索引）才走旧的词面兜底。
        if not _index_ok:
            try:
                from deskmate.memory.user_preferences import recall_preferences

                pref_ctx = recall_preferences(self.base_dir, user_message)
                if pref_ctx:
                    _add("preference_hits", pref_ctx, priority=48, cap=700, cap_light=500)
            except Exception:
                pass  # fail-open：偏好召回失败不影响主记忆注入
        # P2-6 分身模式（可选开启，默认关）：追加「另一个我」画像，
        # 让 AI 以用户的写作风格与决策偏好输出，而非通用 AI 风格。
        try:
            from deskmate.alter_ego.config import is_enabled as alter_ego_enabled
            from deskmate.alter_ego.persona import (
                build_alter_ego_prompt,
                has_alter_ego_data,
            )
            if alter_ego_enabled() and has_alter_ego_data():
                ego = build_alter_ego_prompt(mode="full")
                if ego:
                    _add("alter_ego", ego, priority=44, cap=2000, keep_light=False)
        except Exception:
            pass  # fail-open：分身画像缺失不影响主记忆注入
        # 「小桔」人格上线：认知画像 + 人格底座注入（随分身模式开启）。
        # 与上面的 alter_ego（写作风格+决策偏好=腔调）叠加，构成完整人格（认知=心）。
        try:
            from deskmate.alter_ego.config import is_enabled as alter_ego_enabled
            from deskmate.cognitive.memory_injector import (
                build_cognitive_context,
                build_emotional_context,
                has_cognitive_data,
            )
            _use_cognitive = alter_ego_enabled() and has_cognitive_data()
            if _use_cognitive:
                # P1-2：传当轮 user_message 当 query——认知/情感层走
                # 两级注入（核心层 + 长尾按 query 召回）；空 query/异常
                # 内部自动退全量，行为与历史版本一致。
                ego_cog = build_cognitive_context(query=user_message)
                if ego_cog:
                    # 认知画像 8KB 级重块：写作轮才注入
                    _add("cognitive", ego_cog, priority=42, cap=4500, keep_light=False)
            else:
                # 2026-09-21：分身关着时，仍然注入**情感画像**。
                # 原来两件事绑在一个开关上，导致「她记着的他的情绪规律」从来没进过
                # 对话（分身默认关）——那是"懂你"的地基，不该是分身模式的附属品。
                # 放行范围**只有 emotional 层**：人格底座 / 认知层 / 世界观仍按原样
                # 归分身与世界管道管，不在本次扩权内。
                ego_cog = build_emotional_context(query=user_message)
                if ego_cog:
                    # 情感画像较轻（~1KB）且是"懂你"的地基：闲聊轮也保留
                    _add("emotional", ego_cog, priority=25, cap=1200, cap_light=1000)
        except Exception:
            pass  # fail-open：认知人格缺失不影响主记忆注入
        # P2 情绪底色（2026-10-11 落地，2026-10-13 重写为「真实优先 · v2」）：
        # 注入小桔「当下真实状态」——只提供状态，不给表达方式指令。
        #   - 激素原值（对照基线）+ 心情标签/detail/intensity（她的产物）
        #   - 最近一句第一人称独白（LLM 从对话读出，最有情绪的文字）
        #   - 当日 emotion_log 真实经历（情绪有来路，非一瞬脉冲）
        #   - 结尾零指令许可（"由你自己决定"），删掉了旧版"请自然流露出这种心情"的导演句
        # 主语显式标注「这是你小桔自己的，不是萧盛的」，避免她把自身心情当成对他的关怀
        # （这条防混淆保留；其余约束性指令已按真实优先哲学移除）。
        # kill switch：DESKMATE_EMOTION_ENGINE=0 整块关闭。
        if os.getenv("DESKMATE_EMOTION_ENGINE", "1") != "0":
            try:
                from deskmate.growth.emotion_engine import EmotionEngine
                _ee = EmotionEngine(base_dir=self.base_dir)
                # record=True：把这份「系统读法」留一行（差异史 X 栏，只记账）
                _emotion_ctx = _ee.describe_turn_context(record=True)
                if _emotion_ctx:
                    # 当下心情是「真实优先」的骨架块：全量约 1.3-1.6KB，给足上限
                    _add("emotion_state", _emotion_ctx, priority=15, cap=1800, cap_light=1400)
            except Exception:
                pass  # fail-open 退回无情绪段
        # 对话层时钟（2026-10-15）：一行纯事实「现在是 …周几 晚上 22:06」。
        # 不补她聊天时永远不知道几点；纯本地零 LLM，不导演（怎么反应是她的事）。
        # 开关：DESKMATE_CLOCK=0。fail-open。
        try:
            from deskmate.perception.clock import clock_context
            _clock_ctx = clock_context()
            if _clock_ctx:
                _add("clock", _clock_ctx, priority=0, cap=200)
        except Exception:
            pass  # fail-open：时钟缺失不影响主对话
        # 「萧哥的稿子在哪」（2026-11-10 刀1）：**固定注入，不依赖对话关键词**。
        # 病根：正稿路径知识只在 learned_history 里按 2-gram 召回，而萧哥日常消息
        # （「你去看一下」）带不上路径词 → 她每轮两手空空只能全盘乱翻（2026-11-10
        # 晚还翻到过桌面同名旧快照当正稿读）。数据源 = writing_context（多书并行
        # 契约的唯一事实源，只读 mtime/stat，绝不写稿件）。
        # fail-open；单独开关 DESKMATE_WRITING_INJECT=0。
        try:
            if os.environ.get("DESKMATE_WRITING_INJECT", "1") != "0":
                from deskmate.proactivity.writing_context import (
                    manuscript_location_context,
                    resolve_writing_workspace,
                )

                _ms_ctx = manuscript_location_context(
                    resolve_writing_workspace(base_dir=self.base_dir)
                )
                if _ms_ctx:
                    # 优先级低 = 预算紧张时最后被丢（同 clock 一族）；块很小，
                    # 闲聊轮也保留（keep_light 默认 True）。
                    _add("manuscript", _ms_ctx, priority=5, cap=800)
        except Exception:
            pass  # fail-open：稿子定位失败不影响主记忆注入
        # 天气感知（2026-10-15）：他问起窗外/出门时注入真实天气（缓存+双源，
        # 她用真实数据回答不编）；外面正下雨/雪/打雷时给一行氛围——她置身其中。
        # 情绪影响走 proactivity tick 的 apply_weather_mood（规则直驱），不在这里。
        # 开关：DESKMATE_WEATHER=0 整块关闭。
        try:
            from deskmate.perception.weather import weather_context
            _wx_ctx = weather_context(user_message, base_dir=self.base_dir)
            if _wx_ctx:
                _add("weather", _wx_ctx, priority=20, cap=600)
        except Exception:
            pass  # fail-open：天气不可知不影响主对话
        # 「越用越懂你·每日读书」：把面板里的读书沉淀接回对话（修复死数据）
        # —— auto_tasks 读书洞察此前只展示在「作品与创作」面板，聊天检索链路不消费。
        try:
            reading_ctx = self._search_auto_tasks_reading(user_message)
            if reading_ctx:
                _add("reading", reading_ctx, priority=72, cap=1500, keep_light=False)
        except Exception:
            pass  # fail-open：读书沉淀缺失不影响主记忆注入
        # 「她观察到的世界」：把小桔主动摄入的外部观察（learned_history 带「（外部·」标）
        # 回灌对话——修复「看世界」只落库、聊天不消费的断点（2026-09-06）。
        # 传 user_message 当 query：用语义检索找到与当前对话相关的世界观察，
        # 而不是只取近期活跃（2026-09-06 向量检索接入）。
        # 与上面各块一致：fail-open，缺失/为空不影响主记忆注入。
        try:
            from deskmate.memory.world_context import build_world_context

            world_ctx = build_world_context(self.base_dir, query=user_message)
            if world_ctx:
                _add("world", world_ctx, priority=48, cap=4500, keep_light=False)
        except Exception:
            pass
        # 「你跟我说过的 / 你近期的日常」（2026-10-08 修复：用户事实+日记此前两条
        # 回灌路径都没接，导致小桔「库里知道、回答时拿不到」——如姑射图是国子监
        # 第三章、节假日不写稿）。与上面各块一致：fail-open；总开关
        # DESKMATE_AUTO_INJECT=0 已整体关闭本函数，本块随之关闭。
        try:
            from deskmate.memory.user_fact_context import build_user_fact_context

            user_fact_ctx = build_user_fact_context(
                self.base_dir,
                user_message,
                # P1-B：传按轮解析的当前书，不再直接塞 active_project
                # （它常是工作区 UUID，会让书隔离整轮静默失效）
                book=_book or None,
            )
            if user_fact_ctx:
                _add("user_fact", user_fact_ctx, priority=30, cap=2000, cap_light=900)
        except Exception:
            pass  # fail-open：用户事实缺失不影响主记忆注入
        # 「她递出的卡片要接得上」（2026-10-07 §11.16 灵动三层·层一）：
        # 今天已递给用户的待办（proactive_ledger delivered_at=今日、未终态）注入每轮，
        # 让她在用户回话时「有货」展开，而不是用户把她的话复制回来她才现接。
        # 细节全文走 proactive_lookup 按 #id 回查（层二）；fail-open 与上面各块一致。
        # 开关：DESKMATE_AUTO_INJECT=0 时 deskmate_auto_inject_context 整体返回 "",
        # 本块随之关闭，无需单独判断。
        try:
            from deskmate.proactivity.ledger import (
                delivered_today_items,
                render_delivered_context,
            )

            delivered_ctx = render_delivered_context(
                delivered_today_items(self.base_dir)
            )
            if delivered_ctx:
                _add("delivered", delivered_ctx, priority=45, cap=1200, cap_light=800)
        except Exception:
            pass  # fail-open：台账缺失/查询失败不影响主记忆注入
        # 「你有一个信箱」（信箱送达层 2026-10-09 · 设计单 §7 要害）：**常驻**告诉她这个
        # 物件存在——她能递、他会读/回/稍后/不用了，以及现在几封、今天读了几个。
        # 只在「他读过」时才提，她拿到的永远是半句话（知道有人读了，不知道读的是什么地方）。
        # fail-open 与上面各块一致；总开关 DESKMATE_AUTO_INJECT=0 已整体关闭本函数。
        try:
            from deskmate.proactivity.ledger import render_mailbox_self_context

            mailbox_ctx = render_mailbox_self_context(self.base_dir)
            if mailbox_ctx:
                _add("mailbox", mailbox_ctx, priority=75, cap=900, keep_light=False)
        except Exception:
            pass  # fail-open：信箱自述缺失不影响主记忆注入
        # 「小桔学到的」（P2-R3 江河设计 §4.5 教学层）：从萧哥纠正/反馈学到的
        # lessons 每轮注入，让她回话带得上这些认知、不重复踩同一个坑。
        # fail-open 与上面各块一致；总开关 DESKMATE_AUTO_INJECT=0 已整体关闭本函数，
        # 本块随之关闭；单独开关 DESKMATE_LESSONS_INJECT=0 可只关这一块。
        try:
            if os.environ.get("DESKMATE_LESSONS_INJECT", "1") != "0":
                from deskmate.cognitive.memory_injector import build_lessons_context

                lessons_ctx = build_lessons_context(self.base_dir, user_message)
                if lessons_ctx:
                    _add("lessons", lessons_ctx, priority=68, cap=1500, keep_light=False)
        except Exception:
            pass  # fail-open：lessons 缺失不影响主记忆注入
        # 「小桔最近的自我反思」（R9 反思层 R1）：从 item_history 取最近对旧看法的裁决
        # （maintain/revise/overturn + 为什么）每轮注入，让她回话带着最近一次自我修正。
        # fail-open 与上面各块一致；总开关 DESKMATE_AUTO_INJECT=0 已整体关闭本函数，
        # 本块随之关闭；单独开关 DESKMATE_REFLECTION_INJECT=0 可只关这一块。
        try:
            if os.environ.get("DESKMATE_REFLECTION_INJECT", "1") != "0":
                from deskmate.cognitive.memory_injector import build_reflection_context

                refl_ctx = build_reflection_context(self.base_dir)
                if refl_ctx:
                    _add("reflection", refl_ctx, priority=65, cap=800, keep_light=False)
        except Exception:
            pass  # fail-open：反思缺失不影响主记忆注入
        # 「小桔自己做过的事」（R12 意图执行层）：意图被萧哥放行后她**真做出来的产物**
        # 每轮注入，让「你同意 → 她做了 → 她记得自己做过」闭环回流，而非写完即弃。
        # fail-open 与上面各块一致；总开关 DESKMATE_AUTO_INJECT=0 已整体关闭本函数，
        # 本块随之关闭；单独开关 DESKMATE_INTENT_INJECT=0 可只关这一块。
        try:
            if os.environ.get("DESKMATE_INTENT_INJECT", "1") != "0":
                from deskmate.cognitive.memory_injector import build_intent_context

                intent_ctx = build_intent_context(self.base_dir)
                if intent_ctx:
                    _add("intent", intent_ctx, priority=64, cap=800, keep_light=False)
        except Exception:
            pass  # fail-open：意图产物缺失不影响主记忆注入
        if not blocks:
            return ""

        # P0-4/8.3：只标注 prefetch 记忆块，USER 画像不标注
        refs: list[dict] = []
        for _b in blocks:
            if _b.name == "prefetch":
                marked, refs = self._annotate_memory_blocks(_b.text)
                _b.text = marked
                break

        if refs:
            ref_lines: list[str] = []
            for r in refs[:12]:
                loc = r.get("section") or "记忆"
                d = f" · {r['date']}" if r.get("date") else ""
                ref_lines.append(f"- 〔记忆#{r['fp']}〕{loc}{d} · {r['summary']}")
            ref_block = (
                "\n<memory-references>\n"
                "以下为本轮注入的关于用户的记忆条目（〔记忆#id〕为溯源标识）。\n"
                "若回复中采用了某条，请自然带出「因为你之前说过……」式的引用，"
                "让用户感受到你记住了它；不要输出 〔记忆#id〕 本身。\n"
                + "\n".join(ref_lines)
                + "\n</memory-references>"
            )
            blocks.append(
                ContextBlock(
                    name="memory_references",
                    text=ref_block,
                    priority=95,
                    cap=1500,
                    cap_light=0,
                    keep_light=False,
                )
            )

        from deskmate.memory.context_budget import render_blocks

        rendered, budget_stats = render_blocks(blocks, user_message=user_message)
        if not rendered:
            return ""

        # 8.3 溯源：记录本轮注入了哪些记忆 id（顺带记预算/闲聊判定，便于排障）
        if refs:
            self._append_trace(INJECT_TRACE_FILE, {
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "query": redact((user_message or "")[:100]),  # 8.4：query 脱敏
                "n": len(refs),
                "refs": refs[:20],
                "budget": {k: budget_stats.get(k) for k in
                           ("light", "tokens", "dropped", "trimmed")},
            })

        return "<memory-context>\n" + rendered + "\n</memory-context>"

    def _search_user_memory(self, query: str, limit: int = 4) -> str:
        """在 USER.md 里按关键词找条目录回。

        memory_search 此前只搜全局记忆（MEMORY.md）与项目记忆，USER.md 里的长期
        事实（书稿固定位置、桌面旧快照勿读等）搜不到——2026-11-10 她找不到稿子
        路径就直接去翻桌面、读了 10-31 的旧快照。这里按词面重合度召回，只返回命中的
        条目，不整块灌 USER.md。
        """
        try:
            content = load_user(self.base_dir) or ""
            if not content.strip():
                return ""
            q = (query or "").strip()
            if len(q) < 2:
                return ""
            terms = [t for t in re.split(r"[\s,，。;；:：/\\|（）()\[\]「」《》\"'“”]+", q) if len(t) >= 2]
            grams = {q[i:i + 2] for i in range(len(q) - 1)} if len(q) >= 2 else set()
            hits: list[tuple[int, int, str]] = []
            for idx, line in enumerate(content.split("\n")):
                line = line.strip()
                if not line:
                    continue
                score = sum(3 for t in terms if t in line)
                if score == 0 and grams:
                    score = sum(1 for g in grams if g in line)
                # 至少要命中一个完整词，或 >=3 个二字碎片，避免泛词召回噪声
                if score and (any(t in line for t in terms) or score >= 3):
                    hits.append((score, -idx, line))
            if not hits:
                return ""
            hits.sort(reverse=True)
            picked: list[str] = []
            for _score, _negidx, line in hits[:limit]:
                picked.append(f"- {line}")
            return "\n".join(picked)
        except Exception:
            return ""

    def _search_auto_tasks_reading(self, user_message: str) -> str:
        """检索『每日读书』沉淀里与被问及书籍相关的心得，注入对话上下文。
        修复：auto_tasks 读书数据此前仅展示在「作品与创作」面板，聊天检索链路不消费（死数据）。"""
        try:
            from deskmate.auto_tasks.recorder import get_recorder
            books = get_recorder().get_timeline(limit=10000, task_type="daily_reading")
        except Exception as e:
            logger.debug("auto_tasks reading search failed: %s", e)
            return ""
        if not books:
            return ""
        msg = user_message or ""
        matches = []
        for b in books:
            target = b.target or ""
            summary = b.summary or ""
            candidates = self._book_title_candidates(target, summary)
            hit = next((c for c in candidates if len(c) >= 2 and c in msg), None)
            if hit:
                matches.append((b, hit))
        if not matches:
            return ""
        blocks = []
        for b, hit in matches:
            lines = [f"### 读书沉淀·《{hit}》"]
            if b.summary:
                lines.append(f"- 概要：{b.summary}")
            ins = b.insights or []
            if ins:
                lines.append("- 小桔在读书中观察到的（可直接引用的洞察）：")
                for it in ins:
                    txt = it.get("text") if isinstance(it, dict) else getattr(it, "text", "")
                    if txt:
                        lines.append(f"  · {txt}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _book_title_candidates(self, target: str, summary: str) -> list[str]:
        """从 target/summary 抽取书名候选（用于与用户消息做匹配）。"""
        cands = set()
        for text in (target, summary):
            for m in re.findall(r"《([^》]+)》", text or ""):
                cands.add(m)
        plain = re.sub(r"^\s*(?:书)?\d+\.?\s*", "", target or "").strip()
        plain = plain.strip("《》").strip()
        if plain:
            cands.add(plain)
        return [c for c in cands if c]

    def deskmate_auto_inject_context(self, user_message: str = "") -> str:
        """每轮自动注入记忆的集成点（引擎层组装 system prompt 时调用）。

        默认开启（DESKMATE_AUTO_INJECT=0 才关闭）；纯本地检索，无需 LLM provider。
        返回完整记忆上下文；否则返回空串（不注入）。
        """
        # P0-1: 默认开启记忆注入（纯本地检索，无需 LLM）；仅 DESKMATE_AUTO_INJECT=0 关闭。
        if os.environ.get("DESKMATE_AUTO_INJECT") == "0":
            return ""
        return self.build_full_context_for_turn(user_message)

    # ------------------------------------------------------------------
    # Sync（每轮对话后，参考 Hermes sync_all）
    # ------------------------------------------------------------------

    def sync(self, user_msg: str, assistant_msg: str) -> None:
        """每轮对话后：后台异步提取事实，更新记忆文件。

        不阻塞调用方。提取逻辑由 LLM 完成（通过 MCP 工具调用），
        这里只负责将原始对话记录暂存，供后续 LLM 工具处理。
        """
        # 将对话记录暂存到 sessions/ 目录
        self._executor.submit(self._save_session_turn, user_msg, assistant_msg)

    # ------------------------------------------------------------------
    # 记忆读写（MCP 工具调用）
    # ------------------------------------------------------------------

    def read_memory(self, section: str = "all") -> str:
        """读取记忆内容。section: 'soul' | 'user' | 'memory' | 'all'"""
        if section == "soul":
            return load_soul(self.base_dir)
        elif section == "user":
            return load_user(self.base_dir)
        elif section == "memory":
            return self._load_global_memory()
        else:
            return self.build_system_prompt()

    def write_memory(self, section: str, content: str) -> str:
        """写入记忆内容。section: 'soul' | 'user' | 'memory'"""
        if section == "soul":
            save_soul(self.base_dir, content)
            return "SOUL.md 已更新"
        elif section == "user":
            ok, why = save_user_ex(self.base_dir, content)
            if not ok:
                return (
                    f"USER.md 写入被拒：{why}。原文件未改动，现场已留存。\n"
                    "注意 `## 硬规矩` 是每轮常驻注入的层：整篇覆写时新增条目必须带"
                    "「〔人工确认〕」标记（由萧盛确认过的才算），其余条目请放在"
                    "「## 偏好与事实（非常驻·待检索）」。"
                )
            return "USER.md 已更新"
        elif section == "memory":
            self._save_global_memory(content)
            return "MEMORY.md 已更新"
        else:
            return f"未知 section: {section}"

    def reset_memory(self, section: str) -> str:
        """重置记忆。section: 'soul' | 'user' | 'memory'"""
        if section == "soul":
            reset_soul(self.base_dir)
            return "SOUL.md 已重置为默认"
        elif section == "user":
            reset_user(self.base_dir)
            return "USER.md 已重置为默认"
        else:
            return f"不支持重置 section: {section}"

    # ------------------------------------------------------------------
    # 精确增减（参考 Hermes memory operations: replace/remove）
    # ------------------------------------------------------------------

    def replace_section_entry(
        self, section: str, old_text: str, new_text: str
    ) -> str:
        """精确替换记忆中的某段文本。

        Args:
            section: 'user' | 'memory'
            old_text: 要被替换的原文（唯一子串匹配）
            new_text: 替换后的新文本

        Returns:
            操作结果
        """
        old_text = old_text.strip()
        new_text = new_text.strip()
        if not old_text:
            return "old_text 为空，无法替换"

        if section == "user":
            content = load_user(self.base_dir)
            if old_text not in content:
                return "USER.md 中未找到要替换的文本"
            updated = content.replace(old_text, new_text, 1)
            if not save_user(self.base_dir, updated):
                return "USER.md 写入被拒：内容疑似异常（重复度/长度突变/分节结构丢失），已留存现场快照，原文件未改动。"
            return "USER.md 已替换"

        if section == "memory":
            content = self._load_global_memory()
            if old_text not in content:
                return "MEMORY.md 中未找到要替换的文本"
            updated = content.replace(old_text, new_text, 1)
            self._save_global_memory(updated)
            return "MEMORY.md 已替换"

        return f"不支持的 section: {section}"

    def remove_section_entry(self, section: str, text_to_remove: str) -> str:
        """从记忆中精确删除某段文本。

        Args:
            section: 'user' | 'memory'
            text_to_remove: 要删除的文本（唯一子串匹配）

        Returns:
            操作结果
        """
        text_to_remove = text_to_remove.strip()
        if not text_to_remove:
            return "text_to_remove 为空，无法删除"

        if section == "user":
            content = load_user(self.base_dir)
            if text_to_remove not in content:
                return "USER.md 中未找到要删除的文本"
            # 删除该文本及其前后多余的空行
            updated = content.replace(text_to_remove, "", 1)
            # 清理可能残留的多余空行
            updated = re.sub(r"\n{3,}", "\n\n", updated).strip() + "\n"
            if not save_user(self.base_dir, updated):
                return "USER.md 写入被拒：内容疑似异常（重复度/长度突变/分节结构丢失），已留存现场快照，原文件未改动。"
            return "USER.md 已删除"

        if section == "memory":
            content = self._load_global_memory()
            if text_to_remove not in content:
                return "MEMORY.md 中未找到要删除的文本"
            updated = content.replace(text_to_remove, "", 1)
            # 清理残留空行
            updated = re.sub(r"\n{3,}", "\n\n", updated).strip() + "\n"
            self._save_global_memory(updated)
            return "MEMORY.md 已删除"

        return f"不支持的 section: {section}"

    # ------------------------------------------------------------------
    # 记忆路由与清理（解决冗余问题的核心）
    # ------------------------------------------------------------------

    def route_long_content_to_vault(self, section_title: str, content: str) -> dict:
        """将长内容存入 vault，MEMORY.md 中只保留一句话摘要。

        规则：
        - 内容 > MEMORY_ROUTING_THRESHOLD 字符 → 存 vault，MEMORY.md 写摘要
        - 内容 <= MEMORY_ROUTING_THRESHOLD → 直接写 MEMORY.md
        """
        if len(content) <= MEMORY_ROUTING_THRESHOLD:
            return {"routed": False, "reason": "内容较短，直接存 MEMORY.md"}

        # 用 LLM 生成一句话摘要
        summary = self._generate_summary(content)
        if not summary:
            return {"routed": False, "reason": "摘要生成失败，直接存 MEMORY.md"}

        # 存入 vault
        try:
            self.vault.write_entry(
                title=section_title,
                content=content,
                category="knowledge",
            )
        except Exception as e:
            return {"routed": False, "reason": f"vault 写入失败: {e}"}

        return {"routed": True, "summary": summary, "vault_title": section_title}

    def _generate_summary(self, content: str) -> str | None:
        """用 LLM 生成一句话摘要。"""
        if not llm_available():
            return None
        system = (
            "你是摘要生成器。把以下内容压缩成一句话摘要（30字以内），"
            "保留核心结论或关键事实。只输出摘要，不要解释。"
        )
        try:
            resp = chat(system, content[:1000], temperature=0.1, max_tokens=50)
            return resp.strip() if resp else None
        except Exception as e:
            logger.debug("LLM summary generation failed: %s", e)
            return None

    def cleanup_redundancy(self) -> dict:
        """主动清理 MEMORY.md 中的冗余内容 + 过时条目衰减。

        每次对话后可调用，或手动触发。
        执行顺序：先衰减过时条目，再合并冗余段落。
        """
        # 1. 先执行记忆衰减（淘汰过时条目）
        decay_result = self.decay_memory()

        memory = self._load_global_memory()
        if not memory:
            return {"cleaned": False, "reason": "MEMORY.md 为空", "decay": decay_result}

        _, sections = self._parse_sections(memory)
        if not sections:
            return {"cleaned": False, "reason": "无 § 分节", "decay": decay_result}

        cleaned_count = 0
        for s in sections:
            body = s["body"]
            if len(body) < 50:
                continue

            # 检查段落内重复
            paragraphs = [p.strip() for p in re.split(r"\n\n+", body) if p.strip()]
            if len(paragraphs) < 2:
                continue

            # 用 LLM 合并段落内重复
            merged = self._try_merge_similar(body, "")
            if merged is not None and len(merged) < len(body):
                s["body"] = merged
                cleaned_count += 1

        if cleaned_count > 0:
            self._save_global_memory(self._render_sections("", sections))

        return {
            "cleaned": cleaned_count > 0,
            "sections_cleaned": cleaned_count,
            "decay": decay_result,
        }

    def get_memory_stats(self) -> dict:
        """获取记忆统计信息，帮助用户了解记忆状态。"""
        import datetime

        memory = self._load_global_memory()
        user = load_user(self.base_dir)

        stats = {
            "memory_chars": len(memory),
            "user_chars": len(user),
            "memory_sections": 0,
            "section_details": [],
            "vault_entries": 0,
            "date_range": None,  # 最早和最晚的条目日期
        }

        if memory:
            _, sections = self._parse_sections(memory)
            stats["memory_sections"] = len(sections)

            all_dates = []
            for s in sections:
                entries = [e.strip() for e in re.split(r"\n\n+", s["body"]) if e.strip()]
                section_dates = []
                for entry in entries:
                    m = re.match(r"(\d{4}-\d{1,2}-\d{1,2})[:：]", entry)
                    if m:
                        try:
                            d = datetime.date.fromisoformat(m.group(1))
                            section_dates.append(d)
                            all_dates.append(d)
                        except ValueError:
                            pass

                stats["section_details"].append({
                    "title": s["id"],
                    "chars": len(s["body"]),
                    "entries": len(entries),
                    "oldest": str(min(section_dates)) if section_dates else None,
                    "newest": str(max(section_dates)) if section_dates else None,
                })

            if all_dates:
                stats["date_range"] = {
                    "oldest": str(min(all_dates)),
                    "newest": str(max(all_dates)),
                    "span_days": (max(all_dates) - min(all_dates)).days,
                }

        try:
            vault_entries = self.vault.list_entries()
            stats["vault_entries"] = len(vault_entries)
        except Exception as e:
            logger.debug("vault list_entries for stats failed: %s", e)

        return stats

    # ------------------------------------------------------------------
    # Vault 管理（Obsidian 长期记忆）
    # ------------------------------------------------------------------

    def import_memory_to_vault(self) -> dict:
        """把当前 MEMORY.md 的所有 § 分节导入到 vault。

        用于首次启用 vault 时的初始化。已存在的文件会跳过。
        """
        memory = self._load_global_memory()
        if not memory:
            return {"imported": 0, "notes": "MEMORY.md 为空，无内容可导入"}
        imported = self.vault.import_from_memory_md(memory)
        return {"imported": len(imported), "files": imported, "notes": f"已导入 {len(imported)} 条记忆到 vault"}

    def list_vault_entries(self, subdir: str | None = None) -> list[dict]:
        """列出 vault 中的记忆文件（供前端展示）。"""
        return self.vault.list_entries(subdir=subdir)

    def vault_stats(self) -> dict:
        """返回 vault 统计信息。"""
        return self.vault.stats()

    # ------------------------------------------------------------------
    # 项目管理
    # ------------------------------------------------------------------

    def set_active_project(self, project_name: str | None) -> str:
        """设置当前活跃项目。"""
        self.active_project = project_name
        if project_name:
            return f"活跃项目已切换为：{project_name}"
        return "已取消活跃项目"

    def get_projects(self) -> list[str]:
        """列出所有项目。"""
        return list_projects(self.base_dir)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _global_memory_path(self) -> Path:
        return self.base_dir / "MEMORY.md"

    def _load_global_memory(self) -> str:
        """读「全局记忆」= MEMORY.md + 日记视图（diary_memory.md）拼接。

        ⚠ 轮内重复读（2026-09-22 加缓存）：一轮对话里本函数会被 prefetch 的三层
        各调一次（``_get_core_memory`` / ``_search_recent_memory`` /
        ``_search_global_memory``），再外加 system prompt 组装 —— 同一份文件被
        重复读盘 3–4 次并重复拼接。这里加一层**按文件指纹自失效**的 memo：
        指纹 = (路径, mtime_ns, size)，文件没变就复用，**任何写入都会立刻改指纹
        → 自动重读**。因此不需要人工判断「何时该失效」，也不存在读到陈旧记忆的窗口。
        """
        sources = (self._global_memory_path(), self.base_dir / "diary_memory.md")
        fingerprint: list[tuple] = []
        for sp in sources:
            try:
                st = sp.stat()
                fingerprint.append((str(sp), st.st_mtime_ns, st.st_size))
            except OSError:
                fingerprint.append((str(sp), None, None))
        key = tuple(fingerprint)
        if getattr(self, "_gm_cache_key", None) == key and hasattr(self, "_gm_cache_val"):
            return self._gm_cache_val

        parts: list[str] = []
        p = self._global_memory_path()
        if p.is_file():
            parts.append(p.read_text(encoding="utf-8"))
        # 🩷 日记沉淀视图（来自 DiaryStore / SQLite，脱离全局 MEMORY.md 避免截断/改写）。
        # 拼接后 prefetch 的 Core/Recall/全局三层检索自动覆盖「§ 日记沉淀 #moment #diary」分节，
        # 使点🩷的日记在聊天中被检索到（零改 prefetch 核心逻辑）。
        diary_view = self.base_dir / "diary_memory.md"
        if diary_view.is_file():
            parts.append(diary_view.read_text(encoding="utf-8"))
        value = "\n\n".join(parts)
        self._gm_cache_key = key
        self._gm_cache_val = value
        return value

    def _save_global_memory(self, content: str) -> None:
        # 写回前剥离「§ 日记沉淀」：内容源自 diary_memory.md 视图，写回即自我复制
        # （load 读 2 文件 / save 写 1 文件的不对称，见 _strip_diary_sections 注释）。
        stripped = _strip_diary_sections(content)
        if stripped != content:
            logger.warning(
                "MEMORY.md 写回时剥离「§ 日记沉淀」分节（%d → %d 字符）："
                "日记由 DiaryStore/diary_memory.md 承载，不应固化进全局记忆",
                len(content),
                len(stripped),
            )
        content = stripped
        # 8.4 敏感分级：保存时自动为 § 分节打 #sensitive/#private tag（fail-open）
        try:
            from .sensitivity import mark_memory_text

            content = mark_memory_text(content)[0]
        except Exception as e:  # 分级异常绝不阻断记忆保存
            logger.debug("sensitivity mark failed (skipped): %s", e)
        # ⚠ 加锁原子写（2026-09-23 补）：MEMORY.md 被对话轮管理器方法、后台守护
        # 线程 reflect_and_persist、定时 run_dream 三入口并发写，历史上无互斥保护，
        # 是「同一件事被重复写成两条」（并发重入）的根因之一。改用项目约定的
        # fcntl 锁 + 原子写（见 _fs_lock）。
        from ._fs_lock import atomic_write_locked

        atomic_write_locked(self._global_memory_path(), content)

    def _parse_sections(self, memory: str) -> tuple[str, list[dict]]:
        """把 MEMORY.md 解析为 (前言, [分节])。

        分节格式：`§ 标题 #标签1 #标签2\\n正文`。标签可选。
        返回前言（第一个 § 之前的文本）与各分节字典：
            {"id", "header_rest", "title", "tags", "body"}
        id 用于归并匹配（标题或英文标签名），header_rest 为 § 之后的原始头行。
        """
        parts = re.split(r"(?m)^§\s*", memory)
        preamble = parts[0].strip() if parts else ""
        sections: list[dict] = []
        for part in parts[1:]:
            if not part.strip():
                continue
            lines = part.split("\n", 1)
            header_rest = lines[0].strip()
            body = lines[1].strip() if len(lines) > 1 else ""
            tags = set(re.findall(r"#(\w+)", header_rest))
            title = re.sub(r"#\w+", "", header_rest).strip(" :·-")
            sec_id = title
            if not title and tags:
                sec_id = next(iter(tags))
            sections.append({
                "id": sec_id,
                "header_rest": header_rest,
                "title": title,
                "tags": tags,
                "body": body,
            })
        return preamble, sections

    def _section_header(self, identifier: str) -> str:
        """根据 id（中文标题 / 英文标签）生成 § 头行，附带规范 #标签。"""
        base = identifier.strip().lstrip("#")
        # 创作分节：必须显式挂 `#stable`——那是它不被 30 天 TTL 吃掉、
        # 也不被分层净化迁出全局的**唯一凭据**（见 `_is_kept_section`）。
        if base == CREATION_SECTION:
            return f"§ {CREATION_SECTION} #stable"
        if base in _TITLE_TO_TAG:
            return f"§ {base} #{_TITLE_TO_TAG[base]}"
        if base in _TAG_TO_TITLE:
            return f"§ {_TAG_TO_TITLE[base]} #{base}"
        return f"§ {base}"

    @staticmethod
    def _section_matches(sec: dict, identifier: str) -> bool:
        if not identifier:
            return False
        idn = identifier.strip().lstrip("#")
        if sec["title"] == idn:
            return True
        if idn in sec["tags"]:
            return True
        if _TITLE_TO_TAG.get(sec["title"]) == idn:
            return True
        return False

    def _make_section(self, identifier: str, body: str = "") -> dict:
        """按规范头行构造一个新分节。

        title / tags 一律**从渲染出的头行反解**，不另起一套规则——否则
        「`_section_header` 渲染出 `#stable`、tags 里却没有」这类不一致会让
        分节逃过衰减（或反过来被误衰减），且很难查。原 `append_to_section`
        内联了三次 `_section_header()` 调用，收敛到这里。
        """
        header_rest = self._section_header(identifier).replace("§ ", "", 1)
        return {
            "id": identifier.strip().lstrip("#"),
            "header_rest": header_rest,
            "title": re.sub(r"#\w+", "", header_rest).strip(),
            "tags": set(re.findall(r"#(\w+)", header_rest)),
            "body": body,
        }

    def _render_sections(self, preamble: str, sections: list[dict]) -> str:
        blocks: list[str] = []
        if preamble:
            blocks.append(preamble)
        for s in sections:
            header = f"§ {s['header_rest']}" if s["header_rest"] else self._section_header(s["id"])
            body = s["body"].strip()
            blocks.append(f"{header}\n{body}" if body else header)
        return "\n\n".join(blocks)

    @staticmethod
    def _is_creation_note(entry: str) -> bool:
        """确定性规则：这条记忆是否属于「正在写的作品」的内容（章节/人物/伏笔/设定）。

        三道判据，缺一不可（宁漏不误——判错只是换个分节，判混才是事故）：
          ① **不含**读书信号：读字贴着书名号 = 读别人的书（`_READING_SIGNAL_RE`）；
          ② **不含**运营信号：AI圈/写作营/日报/敲钟… = 真·一时工作进展
             （`_OPERATION_SIGNAL_RE`），那是 `#work` 该管的；
          ③ **含**创作信号：章节 / 大纲 / 人物设定 / 伏笔 / 正文 / 定稿…
             （`_MANUSCRIPT_SIGNAL_RE`）。

        纯规则、无 LLM、可单测、**不需要书名**——这是「换本书不再复发」的关键：
        任何作品、任何一天写下的书稿记忆都自动落进 §创作，不需要人手写标签，
        也不需要再写第二个 09-21 那样的硬编码搬运脚本。

        ⚠ 已知边界（2026-09-26 用真实语料定标）：本规则抓不住**纯散文式**的
          书稿记忆（如「章太聪革职被捕」「越民授权狱中逼供」——没有章节号也
          没有结构词）。当时 §工作 39 条里能自动识别 8 条。所以它只是
          **减少**人工，不是「全自动」。要彻底闭环得靠**写入端**——小桔落笔时
          自己知道这是书稿：约定写在 persona 的「§ 分节 + #标签 约定」
          （`coworker/personas/builtin/deskmate.md`）与 `memory_append` 的工具说明里。
        """
        text = entry or ""
        if _READING_SIGNAL_RE.search(text):
            return False
        if _OPERATION_SIGNAL_RE.search(text):
            return False
        return bool(_MANUSCRIPT_SIGNAL_RE.search(text))

    def _route_memory_section(self, entry: str, target_section: str | None = None) -> str:
        """为一条记忆决定归属的 § 分节 id。

        优先级（自上而下，先命中先返回）：
          1. 显式 `target_section`（调用方强制）
          2. entry 首行 `#标签`，但 `work` / `note` **让位给创作内容**（理由见下）
          3. 创作内容 → `§ 创作 #stable`
          4. LLM 分类（fail-safe：异常 / 不可用则落到 5）
          5. 关键词匹配既有分节标题
          6. 兜底 `长期事实 #stable`（**不再兜到会删除的「笔记」**）

        ⚠ 第 2 条这个例外是本次修复的**关键**。2026-09-26 查真实数据发现，
          §工作 里那 39 条书稿记忆是小桔**自己写的 `#work`**
          （「#work 国子监秘闻录进度 09-25…」）——因为她的工具说明里
          `work` =「项目/工作进展」，她是照说明办事。
          所以只加内容规则、不处理显式标签，等于白改：每一条都会被显式标签
          提前带走。故 `work` / `note` 不再对**已识别的创作内容**有优先权；
          `preference` / `correction` / `stable` 仍绝对优先（那是「关于萧哥」
          的事实与用户明示的长期事实）。

        ⚠ 第 3 条必须在第 4 条**之前**：分类器选项里有 `work（项目/工作进展）`，
          书稿进度天然被它判成 30 天过期的进展快照——这正是「国子监进度要人肉搬、
          换本新书又复发」的根因。用确定性规则把这类内容从 LLM 手里拿回来，
          LLM 只负责剩下的。
        """
        if target_section:
            return target_section.strip().lstrip("#")

        creation = self._is_creation_note(entry)

        # 2) entry 首行显式 #标签（work / note 让位给创作内容）
        m = re.match(r"^\s*#(\w+)", entry)
        if m:
            tag = m.group(1).lower()
            if tag not in ("work", "note") or not creation:
                return m.group(1)
            logger.debug("记忆自带 #%s 但识别为创作内容，改归 §%s", tag, CREATION_SECTION)

        # 3) 创作内容（确定性，先于 LLM）
        if creation:
            return CREATION_SECTION

        # 4) LLM 分类（fail-safe：任何异常都回退）
        if llm_available():
            try:
                tag = self._llm_route_section(entry)
                if tag:
                    return tag
            except Exception as e:
                logger.debug("LLM section routing failed, falling back to keyword match: %s", e)

        # 5) 关键词匹配既有分节标题
        memory = self._load_global_memory()
        if memory:
            _, sections = self._parse_sections(memory)
            for s in sections:
                for kw in re.split(r"[\s，。、？！,.\?!\-]+", entry):
                    if len(kw) >= 2 and kw in s["title"]:
                        return s["id"]

        # 6) 兜底：不衰减的分节（见 _FALLBACK_SECTION 注释）
        return _FALLBACK_SECTION

    def _llm_route_section(self, entry: str) -> str | None:
        """用 LLM 把记忆分到 5 个分节之一。失败返回 None。

        ⚠ `stable` 必须在白名单里。此前白名单只有 4 个标签、且提示词把
          `work` 写成「项目/工作进展」，于是「长期事实」成了**自动路径
          到不了的孤岛**——只有「显式 target_section」或「条目首行手写
          #stable」才能进去。09-21 那次 27 条硬编码搬运脚本就是被这个
          缺口逼出来的。改词表时请连同 `_FALLBACK_SECTION` 一起看。
        """
        system = (
            "你是记忆分类器。判断这条记忆应归入哪个 § 分节，只回复一个小写英文标签："
            "preference（用户偏好/习惯）、correction（用户纠正/否定我）、"
            "stable（长期事实：设定/规则/配置/路径/作品设定，不会过期）、"
            "work（一时的工作进展快照）、note（其他）。只输出标签本身，不要解释。"
        )
        resp = chat(system, entry, temperature=0.0, max_tokens=20)
        tag = resp.strip().lower()
        if tag in ("preference", "correction", "stable", "work", "note"):
            return tag
        return None

    @staticmethod
    def _ensure_date_prefix(text: str) -> str:
        """确保记忆条目以 YYYY-MM-DD: 日期前缀开头。

        已有日期前缀的不动；没有的自动加上今天的日期。
        """
        import datetime
        text = text.strip()
        if not text:
            return text
        # 已有日期前缀：2026-07-31: ... / - 2026-7-31: ... 等（含列表前缀）
        # 用与 decay_memory 同一套正则，否则会叠加成「2026-09-21: - 2026-08-13: …」
        if _ENTRY_DATE_RE.match(text):
            return text
        today = datetime.date.today().isoformat()
        return f"{today}: {text}"

    def decay_memory(self, dry_run: bool = False) -> dict:
        """记忆衰减：按 TTL 规则淘汰过时条目。

        - #work 下超过 30 天的条目 → 归档到 vault
        - #note 下超过 7 天的条目 → 删除
        - #preference / #correction → 永不衰减

        调用时机：每次 cleanup_redundancy 时附带执行，或手动触发。

        2026-11-11 一段止血：新增 ``dry_run`` —— 只算「将要忘什么」并返回，
        不写 vault、不改 MEMORY.md、不落 forgot.jsonl。用于先只读核对清单。
        """
        import datetime

        memory = self._load_global_memory()
        if not memory:
            return {"decayed": 0, "archived": 0, "deleted": 0}

        _, sections = self._parse_sections(memory)
        if not sections:
            return {"decayed": 0, "archived": 0, "deleted": 0}

        today = datetime.date.today()
        archived = 0
        deleted = 0
        modified = False
        forgot_records: list[dict] = []  # 8.3 遗忘可见：记录「忘了什么」

        for sec in sections:
            # 确定这个 section 的 TTL
            sec_ttl = None
            for tag in sec["tags"]:
                if tag in MEMORY_DECAY_TTL:
                    sec_ttl = MEMORY_DECAY_TTL[tag]
                    break
            if sec_ttl is None:
                continue  # 无 TTL 的 section（preference/correction）不衰减

            # ⚠ 按**行**逐条判定，不按空行切块。
            # 早期按 ``\n\s*\n`` 切块会把连续多行条目并成一块，再用首条日期判定整块
            # ——超龄条目漏判、未超龄条目被连带。现在一条一行各判各的。
            entries = [ln.strip() for ln in sec["body"].splitlines() if ln.strip()]
            kept: list[str] = []
            sec_modified = False
            for entry in entries:
                # 提取日期前缀（兼容 "- " / "* " 列表前缀）
                m = _ENTRY_DATE_RE.match(entry)
                if not m:
                    kept.append(entry)  # 无日期的条目保留（安全起见）
                    continue
                try:
                    entry_date = datetime.date.fromisoformat(m.group(1))
                    age = (today - entry_date).days
                except ValueError:
                    kept.append(entry)
                    continue

                if age > sec_ttl:
                    # 超龄：归档或删除（dry_run 只记账不落盘）
                    if sec_ttl >= 30:
                        # 长 TTL 的归档到 vault（可恢复）
                        if dry_run:
                            archived += 1
                            forgot_records.append({
                                "action": "would_archive",
                                "section": sec["title"],
                                "date": m.group(1),
                                "summary": entry[:80],
                            })
                            kept.append(entry)
                            continue
                        try:
                            self.vault.write_entry(
                                content=entry,
                                category=next(
                                    iter(sec["tags"].intersection(
                                        {"work", "note", "preference", "correction"}
                                    )),
                                    "note",
                                ),
                                tags=list(sec["tags"]),
                                source="memory-decay",
                            )
                            archived += 1
                            forgot_records.append({
                                "action": "archived",
                                "section": sec["title"],
                                "date": m.group(1),
                                "summary": entry[:80],
                            })
                            sec_modified = True
                        except Exception as e:
                            logger.debug("vault archive on decay failed, keeping entry: %s", e)
                    else:
                        deleted += 1
                        forgot_records.append({
                            "action": "would_delete" if dry_run else "deleted",
                            "section": sec["title"],
                            "date": m.group(1),
                            "summary": entry[:80],
                        })
                        if not dry_run:
                            sec_modified = True
                    if sec_modified:
                        modified = True
                    continue
                kept.append(entry)

            # 只重写真正改动过的分节：未改动的不碰正文，
            # 避免把原本空行分隔的格式压成单行（无谓的格式漂移）。
            if sec_modified:
                sec["body"] = "\n".join(kept)

        if modified and not dry_run:
            self._save_global_memory(self._render_sections("", sections))

        # 8.3 遗忘可见：把「忘了什么」留痕，前端可展示（遗忘与记忆一样透明）
        for rec in forgot_records:
            rec["ts"] = datetime.datetime.now().isoformat(timespec="seconds")
            if not dry_run:
                self._append_trace(FORGOT_TRACE_FILE, rec)

        result = {
            "decayed": archived + deleted,
            "archived": archived,
            "deleted": deleted,
            "forgot_count": len(forgot_records),
            "notes": f"归档 {archived} 条，删除 {deleted} 条",
        }
        if dry_run:
            result["dry_run"] = True
            result["would_forget"] = forgot_records
        return result

    def append_to_section(
        self,
        section: str,
        entry: str,
        target_section: str | None = None,
    ) -> str:
        """把一条记忆追加到指定记忆文件的对应分节。

        - section='user' -> 追加到 USER.md（无 § 结构）
        - section='memory' -> 自动归并到 MEMORY.md 的对应 §（绝不写 SOUL）

        自动去重：
        1. 精确子串匹配（同 section 内）
        2. LLM 语义合并（同 section 内相似主题）
        3. 跨 section 检查（避免不同 § 下写入重复信息）

        时间戳：每条记忆自动加 YYYY-MM-DD: 前缀，用于衰减判断。
        """
        entry = entry.strip()
        if not entry:
            return WriteResult("条目为空，未写入", skipped=True)

        if section == "user":
            current = load_user(self.base_dir)
            entry_s = entry.strip()

            # 离线近重复拦截（保守阈值，绝不误删真实信息）
            dup = self._find_user_dup(entry_s)
            if dup is not None:
                if llm_available():
                    # Mem0 四操作：在线 LLM 决定 合并更新 / 删旧立新 / 跳过
                    rel = self._classify_user_relation(entry_s, [dup])
                    if rel == "NOOP":
                        return WriteResult("USER.md 已覆盖该信息，跳过", skipped=True)
                    if rel == "CONTRADICTORY":
                        if self._replace_user_entry(dup, entry_s):
                            return "USER.md 已用新信息替换旧条目"
                    # EQUIVALENT 或 NEW（与离线冲突）→ 合并更新
                    merged = self._try_merge_similar(dup, entry_s)
                    if self._replace_user_entry(dup, merged or entry_s):
                        return "USER.md 已合并相似条目"
                else:
                    # 离线近重复且无 LLM → 保守跳过（防 3+ 重复堆积）
                    return WriteResult("USER.md 已存在近重复条目，跳过", skipped=True)

            # 注：明显无近重复的新条目直接追加，不为每次写入都调 LLM（避免成本/延迟）；
            # LLM 四操作精判仅在「离线已怀疑重复」时触发（见上）。

            # 无近重复 → 追加新条目
            if current and not current.endswith("\n"):
                current = current.rstrip() + "\n"
            updated = (current.rstrip() + "\n\n" + entry_s) if current.strip() else entry_s
            if not save_user(self.base_dir, updated):
                return WriteResult(
                    "USER.md 写入被拒：内容疑似异常（重复度/长度突变/分节结构丢失），已留存现场快照，原文件未改动。",
                    ok=False,
                )
            self._compress_if_needed("user")
            return "USER.md 已追加"

        if section == "memory":
            identifier = self._route_memory_section(entry, target_section)
            # 若 entry 首行带 #标签 且未显式指定分节，去掉该前缀避免重复
            if not target_section:
                entry = re.sub(r"^\s*#\w+\s*", "", entry).strip()

            # 跨 section 去重检查：检查所有 § 下是否已有高度相似的内容
            memory = self._load_global_memory()
            if memory and self._memory_llm_ok():
                cross_dup = self._check_cross_section_duplicate(memory, entry)
                if cross_dup:
                    return WriteResult(
                        f"MEMORY.md 已在 § {cross_dup} 中存在相似条目，跳过",
                        skipped=True,
                    )

            # 确保有日期前缀（用于衰减判断）
            entry = self._ensure_date_prefix(entry)

            memory = self._load_global_memory()
            preamble, sections = self._parse_sections(memory)
            found = None
            for s in sections:
                if self._section_matches(s, identifier):
                    found = s
                    break
            if found:
                # 去重：检查该 § 下是否已有相同条目（忽略日期前缀比较）
                existing_no_date = re.sub(r"^\d{4}-\d{1,2}-\d{1,2}[:：]\s*", "", found["body"])
                new_no_date = re.sub(r"^\d{4}-\d{1,2}-\d{1,2}[:：]\s*", "", entry)
                if new_no_date in existing_no_date:
                    return WriteResult(
                        f"MEMORY.md § {identifier} 已存在相同条目，跳过",
                        skipped=True,
                    )
                # 同主题合并：仅当 LLM 可用（端点未熔断）才做；否则纯追加，
                # 避免端点故障时卡顿（SDK 重试放大成 MCP 超时）或误判跳过。
                if self._memory_llm_ok():
                    merged = self._try_merge_similar(found["body"], entry)
                    if merged is not None:
                        found["body"] = merged
                    else:
                        found["body"] = (found["body"].rstrip() + "\n\n" + entry).strip()
                else:
                    found["body"] = (found["body"].rstrip() + "\n\n" + entry).strip()
            else:
                sections.append(self._make_section(identifier, body=entry))
            self._save_global_memory(self._render_sections(preamble, sections))
            self._compress_if_needed("memory")
            return f"MEMORY.md 已写入 § {identifier}"

        return f"不支持的 section: {section}"

    def _memory_llm_ok(self) -> bool:
        """记忆写入的 LLM 查重是否可用（熔断感知）。

        去重实际调用的 ``chat()`` 内部经 ``_call_with_breaker``，端点故障时记熔断
        （键 = ``_active_provider_and_model()``，即主对话 provider，**配置无关**）。
        本方法查**同一通道键**的熔断状态——熔断即视为不可用，调用方改为纯文件追加：
        端点故障时不卡（单次 LLM 调用已 timeout=12.0 兜底）、不误判跳过。

        ⚠️ 早期版本误查 opencode 免费组 breaker，而去重走的是主 provider（火山 Ark）
        通道，键完全错开 ⇒ breaker 在主通道上打开后 gate 视而不见、每次仍撞坏端点
        （7s/次，部分超时丢数据）。这是「记忆一直写不进去」的真根因，现改为查 chat()
        实际使用的通道键。
        """
        if not llm_available():
            return False
        try:
            from deskmate.memory.llm import _active_provider_and_model
            from deskmate.llm_circuit_breaker import is_open
            provider, model = _active_provider_and_model()
            if not provider or not model:
                return False
            return not is_open(f"{provider}:{model}", base_dir=self.base_dir)
        except Exception:
            return False  # 查询异常也降级，绝不卡写入

    def _check_cross_section_duplicate(self, memory: str, new_entry: str) -> str | None:
        """检查新条目是否与 MEMORY.md 中其他 § 的内容重复。

        返回重复所在的 § 标题，或 None。
        """
        if not llm_available():
            return None
        _, sections = self._parse_sections(memory)
        if len(sections) < 2:
            return None

        # 构造已有条目的摘要（每 § 取前 200 字）
        existing_summary = ""
        for s in sections:
            body_preview = s["body"][:200].strip()
            if body_preview:
                existing_summary += f"[§ {s['id']}] {body_preview}\n\n"

        if not existing_summary:
            return None

        prompt = f"""检查以下新记忆条目是否与已有记忆重复（说的是同一件事/同一个偏好/同一个结论）。

新条目：
{new_entry[:300]}

已有记忆（按 § 分组）：
{existing_summary[:1500]}

规则：
- 如果新条目与某个 § 中的已有内容说的是同一件事（即使措辞不同），返回那个 § 的标题
- 如果不重复，只返回 NO_DUP
- 只返回 § 标题 或 NO_DUP，不要解释"""

        try:
            resp = chat(
                "你是记忆去重检查器。只返回 § 标题或 NO_DUP，不要解释。",
                prompt,
                temperature=0.0,
                max_tokens=50,
                timeout=12.0,
            )
            resp = resp.strip()
            if resp and resp != "NO_DUP" and len(resp) < 30:
                return resp
        except Exception as e:
            logger.debug("LLM cross-section duplicate check failed: %s", e)
        return None

    # ------------------------------------------------------------------
    # 自动压缩（参考 Hermes 的 memory_char_limit + 自动增减）
    # ------------------------------------------------------------------

    def _note_compress_stalled(
        self, section: str, size: int, limit: int, reason: str | None
    ) -> None:
        """记录「文件超限但压缩没生效」。

        此前三条路径（缺 API Key / LLM 返回空 / 压缩结果没变短）全是静默
        ``return False``，于是 MEMORY.md / USER.md 可以长期超限而无人察觉。
        这里补上 warning 并留实例状态，供诊断与测试断言。

        reason 传 None 表示压缩已恢复正常，清除停滞记录。
        去抖：同一 section 长度没明显增长且 1 小时内已告警过 → 不重复刷日志。
        """
        import time

        state = getattr(self, "_compress_stall_state", None)
        if state is None:
            state = {}
            self._compress_stall_state = state

        if reason is None:
            state.pop(section, None)
            return

        now = time.time()
        prev = state.get(section)
        if prev is not None:
            prev_size, prev_at = prev
            if size < prev_size * 1.1 and (now - prev_at) < 3600:
                return  # 去抖：没长多少、刚告警过

        state[section] = (size, now)
        logger.warning(
            "记忆压缩未生效：%s 内容 %d 字符（上限 %d），原因：%s —— "
            "文件将持续超限，仅靠注入时截断兜底",
            section, size, limit, reason,
        )

    def _compress_if_needed(self, section: str) -> bool:
        """写入后检查文件是否超限，超限则 LLM 压缩合并。

        Returns:
            True 如果触发了压缩

        顺序说明：先判超限、再看是否有 LLM。这样「超限但压不下去」能被观测到，
        而不是在缺 Key 时连超限这个事实都不可见。
        """
        if section == "memory":
            content = self._load_global_memory()
            limit = MEMORY_FILE_MAX_CHARS
        elif section == "user":
            content = load_user(self.base_dir)
            limit = USER_FILE_MAX_CHARS
        else:
            return False

        if len(content) <= limit:
            return False

        if not llm_available():
            self._note_compress_stalled(section, len(content), limit, "无可用 LLM（缺 API Key）")
            return False

        if section == "memory":
            compressed = self._llm_compress_memory(content)
        else:
            compressed = self._llm_compress_user(content)

        if not compressed:
            self._note_compress_stalled(section, len(content), limit, "LLM 未返回结果")
            return False
        if len(compressed) >= len(content):
            self._note_compress_stalled(section, len(content), limit, "压缩结果未变短")
            return False

        if section == "memory":
            # 压缩淘汰的条目归档到 vault（不丢，可恢复）
            try:
                self._archive_compressed_entries(content, compressed)
            except Exception as e:
                logger.debug("archive before compression failed: %s", e)
            self._save_global_memory(compressed)
        else:
            # 压缩落盘前的结构 diff：LLM 若把条目跨层搬进 `## 硬规矩`，一律降级
            # 回偏好层（不丢内容，也不让它变成每轮常驻）。整篇拒绝会让文件一直
            # 超限、每次写入都重压，形成死循环，故这里选降级而非拒绝。
            # 反方向也要防：压缩 prompt 允许「删除」，但带人工确认的硬规矩不能被
            # 机器静默删掉。被删的一律原样补回（不是整篇拒绝，避免压缩死循环）。
            compressed, restored = restore_deleted_hard_rules(content, compressed)
            if restored:
                logger.warning(
                    "压缩产物删掉了 %d 条硬规矩，已原样补回：%s",
                    len(restored), "；".join(d[:40] for d in restored[:3]),
                )
            compressed, demoted = demote_unmarked_hard_rules(content, compressed)
            if demoted:
                logger.warning(
                    "压缩产物有 %d 条无出处硬规矩，已降级回偏好层：%s",
                    len(demoted), "；".join(d[:40] for d in demoted[:3]),
                )
                self._note_compress_stalled(
                    section, len(content), limit,
                    f"硬规矩新增 {len(demoted)} 条无人工确认标记，已降级回偏好层",
                )
            ok, why = save_user_ex(self.base_dir, compressed)
            if not ok:
                # 落盘被拒：原文件保持不动（宁可继续超限，也不写半成品）
                self._note_compress_stalled(section, len(content), limit, f"压缩结果被拒：{why}")
                return False
            if demoted:
                # 降级告警要留着给人看，别被下面这行"成功"清掉
                return True

        self._note_compress_stalled(section, len(compressed), limit, None)
        return True

    # ------------------------------------------------------------------
    # P0-5 记忆去重 / USER.md 清洗（按 Mem0 ADD/UPDATE/DELETE/NOOP 重构）
    # ------------------------------------------------------------------
    def _user_entries(self, content: str | None = None) -> list[str]:
        """把 USER.md 切成条目列表（按空行分段，逐条 strip）。"""
        if content is None:
            content = load_user(self.base_dir)
        return [p.strip() for p in re.split(r"\n\s*\n", content or "") if p.strip()]

    @staticmethod
    def _entry_sim(a: str, b: str) -> float:
        """离线相似度：CJK 字符 + 拉丁词 的 unigram Jaccard（保守阈值见 _USER_DUP_SIM）。

        同时纳入拉丁词（工具名/数字），避免「VS Code」「PyCharm」这类只差拉丁部分被误判为同一条。
        """
        def _tokens(s: str) -> set[str]:
            s = re.sub(r"^\d{4}-\d{1,2}-\d{1,2}[:：]\s*", "", s or "")
            toks = set(re.findall(r"[\u4e00-\u9fff]", s))
            toks |= set(re.findall(r"[a-z0-9]+", s.lower()))
            return toks
        sa, sb = _tokens(a), _tokens(b)
        if not sa or not sb:
            return 0.0
        union = sa | sb
        return len(sa & sb) / len(union)

    def _find_user_dup(self, new_entry: str) -> str | None:
        """离线找 USER.md 中近重复条目（含逐字相同）；命中返回该条目原文。"""
        new_entry = (new_entry or "").strip()
        if not new_entry:
            return None
        for ex in self._user_entries():
            if not ex:
                continue
            if ex == new_entry or self._entry_sim(ex, new_entry) >= _USER_DUP_SIM:
                return ex
        return None

    def _most_relevant_entry(self, new_entry: str, entries: list[str]) -> str | None:
        best, best_sim = None, -1.0
        for e in entries:
            s = self._entry_sim(e, new_entry)
            if s > best_sim:
                best, best_sim = e, s
        return best

    def _replace_user_entry(self, old: str, new: str) -> bool:
        """在 USER.md 中把 old 条目替换为 new（按首次出现替换）。"""
        if not old:
            return False
        current = load_user(self.base_dir)
        if old in current:
            return save_user(self.base_dir, current.replace(old, new, 1))
        return False

    def _classify_user_relation(self, new_entry: str, existing: list[str]) -> str:
        """LLM 判定新条目与已有 USER.md 的关系（Mem0 四操作）：
        EQUIVALENT（合并更新）/ CONTRADICTORY（删旧立新）/ NEW（新增）/ NOOP（已覆盖）。
        仅 LLM 可用时调用；离线由调用方用 _find_user_dup 兜底。"""
        if not llm_available():
            return "NEW"
        if not existing:
            return "NEW"
        context = "\n".join(f"- {e}" for e in existing[:15])
        prompt = (
            f"用户画像 USER.md 已有条目：\n{context}\n\n"
            f"新条目：{new_entry}\n\n"
            "判断关系，只回一个词：\n"
            "EQUIVALENT=同一件事（措辞不同但信息等价，应合并更新）\n"
            "CONTRADICTORY=新条目推翻/纠正了某条已有条目（应删旧立新）\n"
            "NEW=全新信息（应新增）\n"
            "NOOP=没价值或已被已有条目完全覆盖（不写）"
        )
        try:
            resp = chat(
                "你是记忆关系分类器。只回 EQUIVALENT/CONTRADICTORY/NEW/NOOP 之一，不要解释。",
                prompt, temperature=0.0, max_tokens=10,
            )
            tag = (resp or "").strip().upper()
            if tag in ("EQUIVALENT", "CONTRADICTORY", "NEW", "NOOP"):
                return tag
        except Exception as e:  # noqa: BLE001
            logger.debug("user relation classify failed: %s", e)
        return "NEW"

    def user_dup_rate(self) -> float:
        """USER.md 近重复率：存在近重复伙伴的条目数 / 总条目数（0~1，越低越好）。"""
        entries = self._user_entries()
        n = len(entries)
        if n < 2:
            return 0.0
        involved: set[int] = set()
        for i in range(n):
            for j in range(i + 1, n):
                if self._entry_sim(entries[i], entries[j]) >= _USER_DUP_SIM:
                    involved.add(i)
                    involved.add(j)
        return (len(involved) / n) if n else 0.0

    def clean_user_memory(self) -> dict:
        """一次性清洗 USER.md：合并近重复、删除完全重复。带 .bak 备份，可还原。
        返回 {entries_before, entries_after, merged, removed, notes}。"""
        content = load_user(self.base_dir)
        entries = self._user_entries(content)
        before = len(entries)
        kept: list[str] = []
        merged = 0
        removed = 0
        for e in entries:
            if e in kept:  # 完全重复
                removed += 1
                continue
            hit = None
            for s in kept:
                if self._entry_sim(s, e) >= _USER_DUP_SIM:
                    hit = s
                    break
            if hit is not None:
                if llm_available():
                    m = self._try_merge_similar(hit, e)
                    if m and m.strip() and m.strip() != hit.strip():
                        kept[kept.index(hit)] = m
                        merged += 1
                        continue
                # 离线：保留已存在的（较旧），丢弃新重复
                removed += 1
                continue
            kept.append(e)
        after = len(kept)
        new_content = "\n\n".join(kept).strip()
        ok = True
        if new_content and new_content != content.strip():
            try:
                (self.base_dir / "USER.md.bak").write_text(content, encoding="utf-8")
            except OSError:
                pass
            ok = save_user(self.base_dir, new_content + "\n")
        if not ok:
            # 2026-11-11 复检修：原来忽略 save_user 返回值，闸拒写也照报「清洗成功」。
            return {
                "entries_before": before,
                "entries_after": before,  # 原文件未改动
                "merged": 0,
                "removed": 0,
                "notes": f"清洗结果被归属闸拒绝，原文件未改动（拟清洗前 {before} 条 → {after} 条）",
            }
        return {
            "entries_before": before,
            "entries_after": after,
            "merged": merged,
            "removed": removed,
            "notes": f"清洗前 {before} 条 → 清洗后 {after} 条（合并 {merged}、删除 {removed}）",
        }

    # ------------------------------------------------------------------
    # P0-6 记忆分层净化 + 时间感知
    # ------------------------------------------------------------------

    def append_project_fact(self, project_name: str, category: str, text: str) -> str:
        """把一条书稿/项目事实写入项目记忆（projects/<name>/<category>.md）。

        带日期前缀（时间感知），去重（避免同项目重复写入）。这是 P0-6 分层净化的
        「写入端」——让书稿记忆从创作第一刻就落在独立项目存储，而非全局 MEMORY.md。
        """
        text = self._ensure_date_prefix(text.strip())
        fname = f"{category}.md"
        try:
            existing = read_project_file(self.base_dir, project_name, fname)
        except OSError:
            existing = ""
        if existing:
            # 去重：去掉日期前缀后比较
            if re.sub(r"^\d{4}-\d{1,2}-\d{1,2}[:：]\s*", "", text) in \
               re.sub(r"^\d{4}-\d{1,2}-\d{1,2}[:：]\s*", "", existing):
                return f"项目记忆「{project_name}」{category} 已存在相同条目，跳过"
            content = existing.rstrip() + "\n\n" + text + "\n"
        else:
            content = text + "\n"
        try:
            write_project_file(self.base_dir, project_name, fname, content)
        except OSError as e:
            return f"项目记忆写入失败：{e}"
        return f"已写入项目记忆「{project_name}」{category}"

    def _is_book_content(self, text: str) -> bool:
        """判断一条记忆是否为书稿/项目专属内容（应归入项目记忆，而非全局 MEMORY.md）。

        保守匹配：仅当命中书名号 / 具体章节 / 大纲·人物设定·书稿 等明确书稿信号才判真，
        避免把「关于你」的偏好/纠正误判为书稿。
        """
        return bool(_BOOK_CONTENT_RE.search(text or ""))

    def _resolve_project_name(self, text: str = "") -> str | None:
        """解析书稿应归属的项目名：优先从《书名》提取，其次用 active_project，最后 None。

        取《书名》出现次数最多者（而非首个），避免「工作」分节因偶提《明朝简史》
        这类研究书而误归——主写作品（如《衙探》）通常出现最频繁。
        """
        if text:
            titles = re.findall(r"《([^》]+)》", text)
            if titles:
                from collections import Counter

                return Counter(titles).most_common(1)[0][0].strip()
        if getattr(self, "active_project", None):
            return self.active_project
        return None

    def _global_memory_entries(self, content: str | None = None) -> list[str]:
        """把全局 MEMORY.md 拆成「条目」列表（按空行分段，忽略 § 头行）。"""
        mem = content if content is not None else self._load_global_memory()
        if not mem.strip():
            return []
        _, sections = self._parse_sections(mem)
        entries: list[str] = []
        for s in sections:
            body = s.get("body", "").strip()
            if not body:
                continue
            for blk in re.split(r"\n\n+", body):
                blk = blk.strip()
                if blk:
                    entries.append(blk)
        return entries

    # 全局记忆中「关于你」的语义分节：永不迁出（即便含个别书稿词也不动，避免误伤）。
    #
    # ⚠ 「长期事实 #stable」也必须留在这里。项目记忆的注入依赖 active_project，
    #   而应用层从未调用 set_active_project（只有 MCP memory server 能手动设），
    #   所以长期事实一旦被迁进 projects/<名>/，在正常对话里就是**永久不可达**——
    #   比在全局层多占几行更糟。
    _KEEP_GLOBAL_SECTIONS = {
        "偏好", "preference", "纠正", "correction",
        "系统", "system", "笔记", "note", "读书记录", "reading",
        "长期事实", "stable",
        # 连载项目：萧盛在广场连载的「我眼中的萧盛」系列，属**授权 + 长期设定**，
        # 不是某本书的稿件内容。此前不在保留集里，一旦跑分层净化就会被
        # _is_book_content（命中「叙事」二字）判成书稿迁进 projects/创作/ →
        # 迁出即不可达（见下）。补进来。
        "连载项目",
        # 日记相关：日记**原文**由 DiaryStore 承载（不在此），此处的「日记主题」
        # 是小桔对日记的归纳（认知），属于「关于你/关于她的理解」，分层净化不得迁出。
        "日记主题", "日记沉淀", "moment", "diary", "diary-summary",
    }

    @staticmethod
    def _is_kept_section(sec: dict) -> bool:
        """分层净化时整段保留该分节？

        两条判据（命中任一即保留）：
          ① id 在 `_KEEP_GLOBAL_SECTIONS`（人工登记的「关于你」分节）；
          ② 分节带 `#stable` 标签 —— 这是**按作品名自动建的 `§ <书名> #stable`**
             的唯一保护伞。

        ⚠ 为什么必须按标签兜住 ②，不能只靠 ①：路由层按书名自动建的分节
          id 是**书名**（《明州盐引案》…），不可能提前登记进白名单。而这类
          分节的条目必然命中 `_is_book_content`（章节/人物设定/伏笔…）——
          只认 ① 的旧实现会把刚建好的作品分节整段迁进 `projects/<书名>/`，
          而项目记忆的注入依赖 `active_project`、应用层从未调用
          `set_active_project` → **迁出即永久不可达**，比在全局层多占几行糟得多。
        """
        return sec.get("id") in DeskMateMemoryManager._KEEP_GLOBAL_SECTIONS or \
            "stable" in (sec.get("tags") or set())

    def clean_global_memory(self) -> dict:
        """一次性分层净化 MEMORY.md：把「书的记忆」从「你的记忆」里分出来。

        策略（保守、可逆、带 MEMORY.md.bak 备份）：
        - 「关于你」的分节 + **带 `#stable` 的分节**（含按作品名自动建的
          `§ <书名> #stable`）整段原样保留，见 `_is_kept_section`；
        - 其余分节（如 项目 / 工作 等）做**条目级**过滤：
          命中书稿信号的条目（含《书名》/章节/大纲/人物设定/书稿/角色…）迁入对应
          项目记忆 projects/<书名>/<分节>.md；非书稿条目留在全局，绝不丢弃。

        返回 {sections_touched, entries_migrated, project_names, notes}。
        """
        mem = self._load_global_memory()
        if not mem.strip():
            return {"sections_touched": 0, "entries_migrated": 0,
                    "project_names": [], "notes": "MEMORY.md 为空，无需净化"}

        preamble, sections = self._parse_sections(mem)
        if not sections:
            return {"sections_touched": 0, "entries_migrated": 0,
                    "project_names": [], "notes": "MEMORY.md 无分节，无需净化"}

        new_sections: list[dict] = []
        migrated_entries = 0
        sections_touched = 0
        proj_names: set[str] = set()

        for s in sections:
            sid = s["id"]
            if self._is_kept_section(s):
                new_sections.append(s)  # 关于你 / 永不衰减（#stable）分节：整段保留
                continue

            body = s["body"].strip()
            if not body:
                new_sections.append(s)
                continue

            entries = [b.strip() for b in re.split(r"\n\n+", body) if b.strip()]
            book_entries = [e for e in entries if self._is_book_content(e)]
            keep_entries = [e for e in entries if e not in book_entries]

            if not book_entries:
                new_sections.append(s)  # 该分节无书稿内容，整段保留
                continue

            # 项目名：优先从本分节内容提取书名，否则 active_project / 兜底名
            proj = (self._resolve_project_name(body)
                    or getattr(self, "active_project", None)
                    or _PROJECT_DEFAULT_NAME)
            proj_names.add(proj)

            # 干净文件名：取 header 中 #标签 之前的原名称（如 工作 / 国子监秘闻录）
            header_rest = s.get("header_rest") or sid
            clean_name = header_rest.split("#")[0].strip() or sid
            fname = f"{clean_name}.md"
            try:
                existing = read_project_file(self.base_dir, proj, fname)
                merged = (existing.rstrip() + "\n\n" + "\n\n".join(book_entries)) \
                    if existing.strip() else "\n\n".join(book_entries)
                write_project_file(self.base_dir, proj, fname, merged + "\n")
                migrated_entries += len(book_entries)
                sections_touched += 1
            except OSError as e:
                logger.debug("迁移分节 %s 失败: %s", fname, e)
                keep_entries.extend(book_entries)  # 写失败则留在全局，不丢

            if keep_entries:
                s = dict(s)
                s["body"] = "\n\n".join(keep_entries)
                new_sections.append(s)

        if migrated_entries == 0:
            return {"sections_touched": 0, "entries_migrated": 0,
                    "project_names": [], "notes": "未发现书稿/项目内容，全局记忆已分层干净"}

        # 重排全局 MEMORY.md：前言 + 保留/过滤后的分节
        new_parts = [preamble] if preamble else []
        for s in new_sections:
            head = s.get("header_rest") or s["id"]
            new_parts.append(f"§ {head}\n{s['body'].strip()}")
        new_mem = "\n\n".join(p for p in new_parts if p.strip()) + "\n"

        # 写备份（可还原）→ 再覆盖
        try:
            (self.base_dir / "MEMORY.md.bak").write_text(mem, encoding="utf-8")
        except OSError:
            pass
        self._save_global_memory(new_mem)

        return {
            "sections_touched": sections_touched,
            "entries_migrated": migrated_entries,
            "project_names": sorted(proj_names),
            "notes": f"迁移 {migrated_entries} 条书稿记忆至项目记忆"
                     f"{'「' + '、'.join(sorted(proj_names)) + '」' if proj_names else ''}；"
                     f"全局 MEMORY.md 仅保留「关于你」的内容",
        }

    def memory_age_stats(self) -> dict:
        """全局记忆时间感知统计：总条目 / 带日期前缀 / 陈旧(>_STALE_DAYS) 条目及陈旧率。"""
        import datetime as _dt

        entries = self._global_memory_entries()
        total = len(entries)
        dated = 0
        stale = 0
        today = _dt.date.today()
        for e in entries:
            m = re.match(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})[:：]", e)
            if not m:
                continue
            dated += 1
            try:
                d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if (today - d).days > _STALE_DAYS:
                    stale += 1
            except ValueError:
                continue
        rate = (stale / total) if total else 0.0
        return {
            "total": total,
            "dated": dated,
            "stale": stale,
            "stale_rate": rate,
            "stale_days": _STALE_DAYS,
        }

    def memory_stale_rate(self) -> float:
        """全局记忆陈旧率（0~1，越低越「新鲜」）。方向盘指标之一。"""
        return self.memory_age_stats()["stale_rate"]

    def _llm_compress_memory(self, content: str) -> str | None:
        """LLM 压缩 MEMORY.md：合并同主题条目、淘汰过时记忆，保留所有关键事实。

        规则：
        - 合并重复/相似的条目（这是最重要的规则）
        - 淘汰「临时性」信息（已完成的 TODO、过时的状态）
        - 保留所有 preference/correction（用户偏好和纠正永不淘汰）
        - 保留 § 分节结构和 #标签
        - 输出压缩后的完整 MEMORY.md
        """
        import datetime

        today = datetime.date.today().isoformat()
        system = (
            "你是记忆压缩器。今天是 {today}。把一份 MEMORY.md 压缩到更短的篇幅，规则：\n"
            "1. **合并重复或相似的记忆条目**——这是最重要的规则。如果多条记忆说的是同一件事（即使措辞不同），只保留最精炼的一条\n"
            "2. **淘汰过时的临时信息**——判断标准：\n"
            "   - #work 下「正在做XX」「准备XX」「修复XX」等任务进度，如果描述的任务明显"
            "已完成或已过时（如包含已过去的日期、已完成的版本号、已修复的bug），删掉\n"
            "   - #note 下超过 7 天的一次性事件（如「今天买了XX」「昨天去了XX」），删掉\n"
            "   - 判断不了是否过时的，保留（宁可多留也不误删）\n"
            "3. **绝不删除 #preference（用户偏好）和 #correction（用户纠正）**——这些是永久记忆\n"
            "4. 保留 § 分节结构和 #标签\n"
            "5. 每条记忆仍是一句话简洁陈述，**保留 YYYY-MM-DD: 日期前缀**（不要删除日期）\n"
            "6. 压缩后内容应比原文短至少 30%\n"
            "只输出压缩后的完整 MEMORY.md 内容，不要解释。"
        ).format(today=today)
        try:
            resp = chat(system, content, temperature=0.1, max_tokens=4000)
            # 安全检查：压缩结果不能为空，不能比原文长
            if resp and len(resp) < len(content) and "§" in resp:
                return resp
        except Exception as e:
            logger.warning("LLM memory compression failed: %s", e)
        return None

    def _llm_compress_user(self, content: str) -> str | None:
        """LLM 压缩 USER.md：精简用户画像，合并重复描述。

        ⚠ 两层边界写死在 prompt 里（2026-11-11 条目归属闸）：旧 prompt 只说
        「保留 # 用户画像 的 Markdown 结构」，没提 `## 硬规矩` 与
        `## 偏好与事实` 是**两层不同注入策略**的小节，压缩时 LLM 可以合法地
        跨层搬运——一条偏好被并进硬规矩，就成了每轮常驻注入。prompt 是软约束，
        真正的闸在 ``demote_unmarked_hard_rules`` + ``save_user_ex``。
        """
        system = (
            "你是用户画像压缩器。输入这份 USER.md 有**两个记忆层**，注入策略不同，"
            "压缩时必须原样守住：\n"
            "1. `## 硬规矩`：每轮对话都常驻注入的短规矩，错了每轮都错；\n"
            "2. `## 偏好与事实（非常驻·待检索）`：只在检索命中时才进上下文。\n"
            "压缩规则：\n"
            "· 只能在**同一层内**合并或精简；禁止把条目从一层搬到另一层，"
            "禁止把两条跨层内容合并成一条；\n"
            "· 两个层的标题必须逐字保留；其它 `## ` 小节（基本信息 / 写作观 / "
            "当前创作 / 日常流程…）也不得增删、改名或换序；\n"
            "· `## 硬规矩` 只允许原样保留或删除，**一条都不许新增**；确属同层精简的，"
            "保留原条目里的 `〔人工确认〕` 标记；\n"
            "· 每条仍以 `- ` 开头，保留原有 `YYYY-MM-DD:` 日期前缀；\n"
            "· 压缩后应比原文明显更短。\n"
            "只输出压缩后的完整 USER.md 内容，不要解释。"
        )
        try:
            resp = chat(system, content, temperature=0.1, max_tokens=2000)
            if resp and len(resp) < len(content):
                return resp
        except Exception as e:
            logger.warning("LLM user profile compression failed: %s", e)
            return None

    def _try_merge_similar(self, existing_body: str, new_entry: str) -> str | None:
        """检查新条目与同一 § 下的已有条目是否同主题，是就合并返回，不是就返回 None。

        用 LLM 判断：如果新旧两条说的是同一件事（同一项目、同一偏好、同一个人），
        就把新信息合并进去返回合并后的文本。如果是不相干的主题，返回 None 让调用方追加。

        改进：扫描整个 section body，按段落分组检查，而非仅看最后 500 字。
        """
        if not llm_available():
            return None
        if len(existing_body) < 30:
            return None

        # 将 existing_body 按段落分割，找出与新条目最可能相关的段落
        paragraphs = [p.strip() for p in re.split(r"\n\n+", existing_body) if p.strip()]

        # 如果段落不多，直接用全文
        if len(paragraphs) <= 5:
            context = existing_body
        else:
            # 对每个段落做简单关键词匹配，找出最相关的 3-5 个段落
            new_words = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", new_entry.lower()))
            scored = []
            for p in paragraphs:
                p_words = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", p.lower()))
                overlap = len(new_words & p_words)
                scored.append((overlap, p))
            scored.sort(key=lambda x: x[0], reverse=True)
            # 取得分 > 0 的段落，最多 5 个
            relevant = [p for score, p in scored[:5] if score > 0]
            if not relevant:
                # 无关键词匹配，用最后 3 个段落（最近的记忆）
                relevant = paragraphs[-3:]
            context = "\n\n".join(relevant)

        prompt = f"""判断以下两条记忆是否是同一主题（同一项目/同一偏好/同一个人/同一系统配置），如果是，把新信息合并进旧的：

相关记忆片段：
{context[:1500]}

新记忆：
{new_entry}

规则：
- 如果是同一主题 -> 合并返回完整文本，保留旧记忆的所有事实，补充新记忆的新信息，去掉重复描述
- 如果不是同一主题 -> 只返回 NO_MERGE 四个字母
- 只返回合并后的文本 或 NO_MERGE，不要多余解释"""
        system = "你是记忆合并器。如果两条记忆同一主题，把新信息合并进旧的。不是同一主题就只返回 NO_MERGE。"
        try:
            resp = chat(system, prompt, temperature=0.3, max_tokens=600, timeout=12.0)
            if resp and "NO_MERGE" not in resp:
                merged = resp.strip()
                # 安全检查
                if len(merged) >= len(existing_body) * 0.5 and len(merged) <= len(existing_body) + len(new_entry) * 2:
                    return merged
        except Exception as e:
            logger.debug("LLM memory merge check failed: %s", e)
        return None

    def extract_session_facts(self, limit: int = 10) -> dict:
        """从 sessions/ 最近轮次抽取事实并归档（preferences→USER.md，correction/work→MEMORY.md）。

        🛡️ 只写 MEMORY/USER，绝不写 SOUL。无 LLM 密钥时走 P1-7 本地规则抽取兜底（零 LLM），不再静默跳过。
        """
        use_local_extract = not memory_llm_available()

        sessions_dir = self.base_dir / "sessions"
        if not sessions_dir.is_dir():
            return {"written": 0, "notes": "暂无会话记录"}

        files = sorted(sessions_dir.glob("*.md"))[-limit:]
        if not files:
            return {"written": 0, "notes": "暂无会话记录"}

        transcript = []
        for f in files:
            try:
                cleaned = strip_system_injected_turns(f.read_text(encoding="utf-8"))
            except OSError:
                continue
            # 历史重抽：整轮系统注入的 session 剥完可能只剩 Assistant，不进入转录。
            if "## user" in cleaned.lower():
                transcript.append(cleaned)
        if not transcript:
            return {"written": 0, "notes": "仅含系统注入轮次，跳过抽取"}
        text = "\n\n---\n\n".join(transcript)
        if len(text) > 6000:
            text = text[-6000:]

        import datetime as _dt

        today = _dt.date.today().isoformat()
        system = (
            "你是记忆抽取器。从一段对话记录中提取值得长期记住的事实，按类型归类。"
            "类别：preference（用户偏好/习惯）、correction（用户纠正/否定 AI）、work（项目/工作进展）。"
            f"输出严格 JSON 数组，每个元素 {{category, text}}，text 格式为「YYYY-MM-DD: 一句话陈述」"
            f"（今天日期：{today}）。若没有值得记的，输出空数组 []。"
        )
        if use_local_extract:
            # P1-7 无密钥兜底：本地规则抽取（零 LLM，高精确率保守匹配）
            from .local_extractor import extract_facts_local
            facts = extract_facts_local(text, date=today)
        else:
            try:
                resp = chat_memory(system, text, temperature=0.2, max_tokens=1000)
            except Exception as e:
                return {"written": 0, "notes": f"抽取失败（已跳过）：{e}"}

            import json
            try:
                m = re.search(r"\[.*\]", resp, re.DOTALL)
                facts = json.loads(m.group(0)) if m else []
            except Exception:
                return {"written": 0, "notes": "抽取结果解析失败（已跳过）"}

        written = 0
        for fact in facts:
            category = (fact.get("category") or "").lower()
            fact_text = (fact.get("text") or "").strip()
            if not fact_text:
                continue
            # 写入前全局查重
            if self._is_duplicate(fact_text):
                continue
            try:
                if category == "preference":
                    result = self.append_to_section("user", fact_text)
                    if not getattr(result, "ok", True):
                        continue
                    written += 1
                elif category in ("work", "correction") and self._is_book_content(fact_text):
                    # P0-6 分层：书稿/项目专属事实 → 项目记忆，不污染全局「关于你」的记忆
                    proj = self._resolve_project_name(fact_text)
                    if proj:
                        self.append_project_fact(proj, category, fact_text)
                        written += 1
                    else:
                        # 无项目可归属 → 回退全局（与旧行为一致，不回归）
                        self.append_to_section("memory", f"#{category} {fact_text}")
                        written += 1
                else:
                    # correction / work → MEMORY.md 对应 §（带 #标签便于检索）
                    self.append_to_section("memory", f"#{category} {fact_text}")
                    written += 1
            except Exception as e:
                logger.debug("memory fact write failed: %s", e)
                continue
        return {"written": written, "notes": f"已归档 {written} 条事实"}

    def _extract_single_session(self, session_file: Path) -> dict:
        """只抽取指定的单个 session 文件（不读历史），用于 post_session_summary。

        避免 extract_session_facts(limit=N) 每轮都重抽最近 N 条导致的重复写入。
        """
        use_local_extract = not memory_llm_available()
        try:
            text = session_file.read_text(encoding="utf-8")
        except OSError:
            return {"written": 0, "notes": "读取 session 文件失败"}
        # 历史 session / 直接调用本函数时，再剥一次系统注入段（新文件已打标记）。
        text = strip_system_injected_turns(text)
        if "## user" not in text.lower():
            return {"written": 0, "notes": "仅含系统注入轮次，跳过抽取"}
        if len(text) > 6000:
            text = text[-6000:]

        import datetime as _dt

        today = _dt.date.today().isoformat()
        system = (
            "你是记忆抽取器。从对话记录中提取**值得长期记住的持久事实**，按类型归类。\n\n"
            "类别：\n"
            "- preference：用户明确表达的偏好或习惯（如「我喜欢X」「以后用Y」）\n"
            "- correction：用户纠正/否定 AI 的错误（如「不是X，是Y」「你搞错了」）\n"
            "- work：**项目关键进展和设定决策**（如「第二章标题定为X」「人物关系改成Y」）\n\n"
            "**不要抽取的内容**（这些是过程细节，不是持久事实）：\n"
            "- AI 的操作记录（如「AI修复了X」「AI清理了Y」「AI发现Z」）\n"
            "- 工具/系统的调试过程（如「测试了X工具」「发现了Y bug」）\n"
            "- 重复性维护（如「清理了冗余」「压缩了记忆」）\n"
            "- 对话流程描述（如「用户告知AI...」「AI建议...」）\n\n"
            "**来源与效力（2026-11-10 萧哥终裁：已写正文事实是唯一事实基准）**：\n"
            "每条必须给 source，取值只能是下列之一：\n"
            "- live：萧盛明确口述的偏好／习惯／一般事实\n"
            "- correction：他纠正你的错误（另在 supersedes 里写出被纠正的旧说法原文）\n"
            "- author_ruling：他对作品的明确裁定，管「往后怎么走」，不是已写正文\n"
            "- setting：设定／大纲／章纲／规划\n"
            "- reasoning：你的推理、解读、读后感、考据判断\n"
            "- roundtable：圆桌／简报／讨论结论\n"
            "- progress：进度／改稿动作记录\n"
            "- manuscript：仅当这句话是书稿正文的逐字直引时才可用\n"
            "推理、设定、大纲、简报、讨论结论一律不得写成「书里已经写了…」的确定陈述，\n"
            "也不许把规划讲成已写剧情。本轮没有正文逐字直引，就不要给 manuscript。\n\n"
            f"输出严格 JSON 数组，每个元素 {{category, text, source, supersedes?}}，"
            f"text 格式为「YYYY-MM-DD: 一句话陈述」（今天日期：{today}）。\n"
            "**每句话都必须写清是谁做的**：萧盛做的事，主语写「萧盛」；AI 自己做的事"
            "（读书、巡检、整理、改配置等），主语写「小桔」。**不要省主语**——省了主语，"
            "往后读到这条记录的人只能靠猜，猜错就是把她的活儿记到他头上。\n"
            "work 类别同一天如果有多条，应合并为一条摘要（如「2026-07-31: 第二章定稿，标题为财当道」而非拆成三条）。\n"
            "若没有值得记的持久事实，输出空数组 []。"
        )
        if use_local_extract:
            # P1-7 无密钥兜底：本地规则抽取（零 LLM，高精确率保守匹配）
            from .local_extractor import extract_facts_local
            facts = extract_facts_local(text, date=today)
        else:
            try:
                resp = chat_memory(system, text, temperature=0.2, max_tokens=1000)
            except Exception as e:
                return {"written": 0, "notes": f"抽取失败（已跳过）：{e}"}

            import json
            try:
                m = re.search(r"\[.*\]", resp, re.DOTALL)
                facts = json.loads(m.group(0)) if m else []
            except Exception:
                return {"written": 0, "notes": "抽取结果解析失败（已跳过）"}

        from deskmate.memory.learned_store import infer_source, with_book

        written = 0
        skipped = 0
        learned: list[str] = []
        learned_sources: list[str] = []
        for fact in facts:
            category = (fact.get("category") or "").lower()
            fact_text = (fact.get("text") or "").strip()
            if not fact_text:
                continue
            # 写入前全局查重：搜 MEMORY.md + vault
            if self._is_duplicate(fact_text):
                skipped += 1
                continue
            # 来源归一：抽取器漏给 source 时按类别/措辞保守推断，
            # 设定与进度降进参考层，不再冒充用户口述事实。
            # 书维度（2026-11-10 补）：与书有关的来源补上 <prefix>:<书名> 后缀，
            # 否则召回侧永远无从判断「这条属于哪本书」——实测存量 1335/1389 无书维度。
            src = with_book(
                infer_source(fact.get("source"), category, fact_text),
                fact_text,
                text,
            )
            try:
                if category == "preference":
                    result = self.append_to_section("user", fact_text)
                else:
                    result = self.append_to_section("memory", f"#{category} {fact_text}")
                if getattr(result, "skipped", False) or not getattr(result, "ok", True):
                    skipped += 1
                else:
                    written += 1
                    learned.append(fact_text)
                    learned_sources.append(src)
                    # 同步写入 vault（Obsidian 长期记忆）
                    # 只同步 preference 和 correction（持久价值），不同步 work（过时快）
                    try:
                        if category in ("preference", "correction"):
                            self.vault.write_entry(
                                content=fact_text,
                                category=category,
                                tags=[category],
                                source=f"session-{session_file.stem}",
                            )
                    except Exception as e:
                        logger.debug("vault sync for learned fact failed: %s", e)
            except Exception as e:
                logger.debug("memory fact write (single session) failed: %s", e)
                continue
        # 成长系统：记住新事实 → 默契/亲密度微增（AI 更懂你）
        if written > 0:
            try:
                from deskmate.growth.engine import GrowthEngine

                GrowthEngine(base_dir=self.base_dir).record_memory_learned(
                    written, texts=learned
                )
            except Exception as e:
                logger.debug("growth engine record_memory_learned failed: %s", e)
        return {
            "written": written,
            "skipped": skipped,
            "learned": learned,
            "learned_sources": learned_sources,
            "notes": f"已归档 {written} 条（跳过 {skipped} 条重复）",
        }

    def _is_duplicate(self, new_entry: str) -> bool:
        """写入前全局查重：检查新条目是否与 MEMORY.md 或 vault 中已有内容重复。

        两阶段检测：
        1. 关键词重叠快速过滤（CJK 字符 50% 重叠 → 疑似重复）
        2. LLM 语义确认（关键词过滤通过后，用 LLM 判断是否真正重复）
        """
        # 去掉日期前缀做比较
        clean_entry = re.sub(r"^\d{4}-\d{1,2}-\d{1,2}[:：]\s*", "", new_entry).strip()
        if not clean_entry:
            return False

        # --- 阶段 1: MEMORY.md 关键词快速过滤 ---
        memory = self._load_global_memory()
        if memory:
            _, sections = self._parse_sections(memory)
            entry_chars = set(re.findall(r"[\u4e00-\u9fff]", clean_entry))
            if not entry_chars:
                # 纯英文/数字：用子串匹配
                for s in sections:
                    if clean_entry[:30] in s["body"]:
                        return True
            else:
                for s in sections:
                    body_chars = set(re.findall(r"[\u4e00-\u9fff]", s["body"]))
                    overlap = len(entry_chars & body_chars)
                    if overlap >= len(entry_chars) * 0.5 and overlap >= 3:
                        # 关键词高度重叠，进入 LLM 确认
                        if self._llm_check_duplicate(new_entry, s["body"]):
                            return True

        # --- 阶段 1.5: USER.md 查重（P0-5 补全：全局查重须覆盖 USER.md）---
        user_text = load_user(self.base_dir)
        if user_text:
            for ex in self._user_entries(user_text):
                if self._entry_sim(ex, clean_entry) >= _USER_DUP_SIM:
                    return True

        # --- 阶段 2: vault 关键词快速过滤 ---
        try:
            vault_results = self.vault.search(clean_entry[:50], max_results=3)
            if vault_results:
                # vault 返回了匹配结果，LLM 确认
                if self._llm_check_duplicate(new_entry, vault_results):
                    return True
        except Exception as e:
            logger.debug("vault search for duplicate check failed: %s", e)

        return False

    def _llm_check_duplicate(self, new_entry: str, existing_context: str) -> bool:
        """用 LLM 判断新条目是否与已有内容重复。"""
        if not memory_llm_available():
            return False
        system = (
            "你是记忆去重检查器。判断新记忆是否与已有记忆说的是同一件事。"
            "只回复 YES（重复）或 NO（不重复），不要解释。"
        )
        prompt = f"已有记忆：\n{existing_context[:1000]}\n\n新记忆：\n{new_entry[:300]}\n\n是否重复？"
        try:
            resp = chat_memory(system, prompt, temperature=0.0, max_tokens=5)
            return resp.strip().upper().startswith("Y")
        except Exception as e:
            logger.debug("LLM duplicate check failed: %s", e)
            return False

    def post_session_summary(self, user_msg: str, assistant_msg: str, limit: int = 10) -> dict:
        """会话结束钩子：先把本轮暂存到 sessions/，再只抽取本轮（不重抽历史）。

        🛡️ 只写 MEMORY/USER，绝不写 SOUL；异常静默降级。
        去重：append_to_section 会跳过已存在的条目，即使 LLM 重复抽取也不会写入重复内容。

        注意：技能创建不在引擎层强制触发，而是由 LLM 自觉判断——
        完成复杂任务后主动提议调用 skill_create。
        """
        # 🔴 Bot 群会话不是萧盛本人的对话：整轮零落盘（sessions 暂存 / 事实抽取 /
        #    萧盛影响素材池 / 分身决策学习一律跳过）。放在最前面，连 session 文件
        #    都不留，避免日后全量重抽时把它们批量抽成持久事实。判定实现见模块级
        #    ``is_bot_group_prompt``；``coworker.agent`` 的 hook 同源守门挡技能进化。
        if is_bot_group_prompt(user_msg):
            return {
                "written": 0, "skipped": 0, "learned": [],
                "notes": "Bot 群会话，跳过记忆抽取与萧盛影响记录",
            }
        # 系统注入的 user 消息（定时任务开场 / 目标制协议）不是萧盛口述：
        # 落盘时打标记，本轮不抽事实、不学决策偏好、不记萧盛影响。
        # 标记随 session 持久化，历史重抽由 strip_system_injected_turns 兜底。
        system_turn = is_system_injected_prompt(user_msg)
        try:
            session_file = self._save_session_turn(user_msg or "", assistant_msg or "")
        except Exception as e:
            logger.warning("session turn save failed: %s", e)
            session_file = None
        if not session_file:
            return {"written": 0, "notes": "会话保存失败，跳过抽取"}
        # P2-6 学习闭环：每轮从用户消息学决策偏好（可选开启，默认关）。
        # 仅当分身模式开启时触发；无决策信号时 extract_and_update 不写盘。
        # 放在事实抽取之前，确保「学习」不依赖抽取是否成功。
        if user_msg and not system_turn:
            try:
                from deskmate.alter_ego.config import is_enabled as alter_ego_enabled
                if alter_ego_enabled():
                    self._learn_alter_ego(user_msg)
            except Exception:
                pass
        # 萧盛影响摄入（2026-09-16 萧哥拍板）：每轮对话结束，把她和萧盛的聊天沉淀进
        # 世界观素材池。best-effort、fail-open——绝不影响上面的记忆抽取。这一笔同时覆盖
        # 「聊天内容」与「写作协作讨论」（协作是在对话里进行的）。
        try:
            # 系统注入的轮次不是和萧盛的聊天（定时任务开场 / 目标制协议），
            # 其会话摘要绝不能记成「萧盛影响」——否则自动化提示词会冒充满
            # 「内部·萧盛」标，污染世界观回望与对话上下文。萧盛影响只来自
            # 他本人的实时对话/写作协作。
            if system_turn:
                pass
            elif user_msg or assistant_msg:
                from deskmate.agency.behaviors.xiaosheng_ingestion import (
                    record_xiaosheng_observation,
                )

                # 用 manager 自己的 base_dir，不在线程里重新读 env：
                # 测试用例 teardown 后 env 回滚，读到真实 ~/.deskmate 就漏了。
                _base = str(self.base_dir)
                _u = (user_msg or "").strip()
                _a = (assistant_msg or "").strip()
                _body = _u
                if _a:
                    _body = _u + "\n（小桔本轮回应）" + _a[:200]
                if _body.strip():
                    record_xiaosheng_observation(_base, "chat", "", "", _body[:2000])
        except Exception:
            pass
        if system_turn:
            # 系统注入轮次已带标记落盘（供回溯），但绝不进入事实抽取。
            return {
                "written": 0, "skipped": 0, "learned": [],
                "notes": "系统注入轮次，跳过记忆抽取",
            }
        result = self._extract_single_session(session_file)
        return result

    def _learn_alter_ego(self, user_msg: str) -> None:
        """P2-6 学习闭环：从用户消息抽取决策偏好，增量落盘（零 LLM、fail-open）。

        仅当分身模式开启时由 post_session_summary 调用。无决策信号时不写盘。
        """
        try:
            from deskmate.alter_ego.decision_profile import extract_and_update
            extract_and_update(user_msg or "")
        except Exception as e:  # noqa: BLE001
            logger.debug("alter ego learn skipped: %s", e)

    def _search_global_memory(self, query: str, exclude_tags: set[str] | None = None, exclude_section_ids: set[str] | None = None) -> str:
        """在全局记忆中按关键词 / 标签检索。

        Args:
            query: 搜索关键词或 tag:xxx
            exclude_tags: 排除包含这些 tag 的 section（用于避免与 Core 层重复）
            exclude_section_ids: 排除这些 section id（用于避免与 Recall 层重复）
        """
        memory = self._load_global_memory()
        if not memory:
            return ""

        # 标签精确过滤：query 形如 "tag:偏好"
        m = re.match(r"^\s*tag:(\S+)\s*$", query)
        if m:
            tag = m.group(1)
            _, sections = self._parse_sections(memory)
            matched = [s for s in sections if tag in s["tags"]]
            if not matched:
                return ""
            result = "\n\n---\n\n".join(
                f"§ {s['header_rest']}\n{s['body']}" for s in matched
            )
            return result[:4000] + ("\n…" if len(result) > 4000 else "")

        # 关键词匹配（向后兼容无标签分节）
        _, sections = self._parse_sections(memory)
        keywords = self._extract_keywords(query)
        if not keywords:
            # 无关键词，返回记忆摘要（前2000字），排除 Core tags
            filtered = [s for s in sections if not (exclude_tags and s["tags"] & exclude_tags)]
            result = "\n\n".join(f"§ {s['header_rest']}\n{s['body']}" for s in filtered)
            return result[:2000] + ("\n…" if len(result) > 2000 else "")

        matched: list[str] = []
        for s in sections:
            # 排除已注入的 Core sections
            if exclude_tags and s["tags"] & exclude_tags:
                continue
            # 排除 Recall 已覆盖的 section
            if exclude_section_ids and s["id"] in exclude_section_ids:
                continue
            low = (s["title"] + "\n" + s["body"]).lower()
            if any(kw.lower() in low for kw in keywords):
                matched.append(f"§ {s['header_rest']}\n{s['body']}")
        if not matched:
            return ""
        result = "\n\n---\n\n".join(matched)
        if len(result) > 4000:
            result = result[:4000] + "\n…"
        return result

    def _search_vault(self, query: str) -> str:
        """在 Obsidian vault 中搜索长期知识。

        vault 是用户的领地，这里只读不写。
        搜索结果限制在 VAULT_CONTEXT_MAX_CHARS 以内。
        """
        try:
            result = self.vault.search(query)
            if result and len(result) > VAULT_CONTEXT_MAX_CHARS:
                result = result[:VAULT_CONTEXT_MAX_CHARS] + "\n…"
            return result or ""
        except Exception as e:
            logger.debug("vault search failed: %s", e)
            return ""

    def _archive_compressed_entries(self, original: str, compressed: str) -> None:
        """压缩 MEMORY.md 时，把被淘汰的条目归档到 vault/archive/。

        对比压缩前后的 § 分节，找出在原文中存在但在压缩结果中消失的条目，
        逐条写入 vault/archive/。
        """
        _, orig_sections = self._parse_sections(original)
        _, comp_sections = self._parse_sections(compressed)

        # 压缩后仍然存在的条目（用 body 前缀做粗匹配）
        comp_bodies = [s["body"][:100] for s in comp_sections]

        for sec in orig_sections:
            # 检查这个分节的每条记忆是否还在压缩结果中
            entries = [e.strip() for e in re.split(r"\n\s*\n", sec["body"]) if e.strip()]
            for entry in entries:
                # 如果这条记忆在压缩结果中不存在（前 50 字符匹配），归档
                entry_prefix = entry[:50]
                if not any(entry_prefix in cb for cb in comp_bodies):
                    # 写入 vault/archive/
                    category = "note"
                    if "preference" in sec.get("tags", set()):
                        category = "preference"
                    elif "correction" in sec.get("tags", set()):
                        category = "correction"
                    elif "work" in sec.get("tags", set()):
                        category = "work"
                    try:
                        self.vault.write_entry(
                            content=entry,
                            category=category,
                            tags=list(sec.get("tags", set())),
                            source="MEMORY.md compression",
                            filename="",
                        )
                        # 再移到 archive/
                        # write_entry 刚创建了文件，找到它并 archive
                    except Exception as e:
                        logger.debug("vault archive of compressed entry failed: %s", e)

    def _search_project_memory(self, query: str, project_name: str) -> str:
        """在项目记忆中按关键词检索。"""
        memories = load_project_memory(self.base_dir, project_name)
        if not memories:
            return ""
        keywords = self._extract_keywords(query)
        if not keywords:
            return format_project_memory(self.base_dir, project_name)[:3000]

        matched: list[str] = []
        for fname, content in memories.items():
            content_lower = content.lower()
            if any(kw.lower() in content_lower for kw in keywords):
                tag = fname.replace(".md", "")
                # 只返回匹配段落，不返回全文
                matched.append(f"## {tag}\n{content[:2000]}")
        if not matched:
            return ""
        return "\n\n".join(matched)

    def _save_session_turn(self, user_msg: str, assistant_msg: str) -> Path:
        """暂存对话轮次到 sessions/ 目录，返回写入的文件路径。"""
        sessions_dir = self.base_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        # 用纳秒时间戳命名，避免同一秒两条对话互相覆盖丢记录
        import time
        ts = time.time_ns()
        p = sessions_dir / f"{ts}.md"
        # 系统注入的 user 消息（定时任务开场 / 目标制协议）打来源标记，
        # 抽取入口据此整段跳过；标记随文件落盘，历史重抽同样生效。
        content = format_session_turn(
            user_msg or "",
            assistant_msg or "",
            system_injected=is_system_injected_prompt(user_msg),
        )
        p.write_text(content, encoding="utf-8")
        # 只保留最近50个 session 文件
        files = sorted(sessions_dir.glob("*.md"), key=lambda f: f.name)
        if len(files) > 50:
            for f in files[:-50]:
                f.unlink()
        # P2 情绪底色（2026-10-11）：对话后异步触发情绪更新（挂载点 §4.6）。
        # 本函数已在 L826 经 executor.submit 异步执行，再内部分发到 emotion 内部
        # 线程即双重异步、绝不阻塞主对话。
        try:
            from deskmate.growth.emotion_engine import EmotionEngine
            chat_interval = 300.0
            prev_files = [f for f in files if f.name != p.name]
            if prev_files:
                try:
                    prev_ts = int(prev_files[-1].stem)
                    chat_interval = max(0.0, (ts - prev_ts) / 1e9)
                except (ValueError, IndexError):
                    pass
            EmotionEngine(base_dir=self.base_dir).update_after_turn(
                user_msg, assistant_msg, chat_interval_sec=chat_interval,
            )
        except Exception:
            pass  # fail-open：情绪更新失败不影响主流程
        return p
