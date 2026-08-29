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
from .user_profile import load_user, save_user, reset_user
from .llm import llm_available, chat, chat_memory, memory_llm_available
from .vault_store import VaultStore

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

# 记忆衰减 TTL（天数）：超过此天数的条目自动归档或删除
MEMORY_DECAY_TTL = {
    "work": 30,       # 工作进展：30 天后归档到 vault
    "note": 7,        # 笔记：7 天后删除（一次性信息）
    # preference / correction：永不衰减（用户偏好和纠正永久保留）
}

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
TRACE_MAX_BYTES = 512 * 1024           # 单文件字节上限

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
}
_TAG_TO_TITLE = {v: k for k, v in _TITLE_TO_TAG.items()}


def _default_base_dir() -> Path:
    """默认记忆目录：~/.deskmate"""
    return Path(os.path.expanduser("~/.deskmate"))


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

    def build_system_prompt(self) -> str:
        """组装 system prompt：SOUL.md + USER.md + MEMORY.md + 项目记忆。"""
        parts: list[str] = []

        # 1. AI 人格
        soul = load_soul(self.base_dir)
        if soul:
            parts.append(soul)

        # 2. 用户画像
        user = load_user(self.base_dir)
        if user:
            parts.append(user)

        # 3. 全局记忆
        memory = self._load_global_memory()
        if memory:
            parts.append(memory)

        # 4. 项目记忆（如果有活跃项目）
        if self.active_project:
            proj_mem = format_project_memory(self.base_dir, self.active_project)
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

    def prefetch(self, user_message: str) -> str:
        """每轮对话前：检索相关记忆，返回 <memory-context> 标签包裹的内容。

        三层记忆架构：
        1. Core（热）— preference/correction，每轮无条件注入（占 1/3 预算）
        2. Recall（温）— 7 天内的近期记忆，按关键词匹配优先注入（占 1/3 预算）
        3. Archival（冷）— vault 长期知识 + 旧记忆，按关键词匹配（占 1/3 预算）
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

        # 5. 项目记忆
        if self.active_project:
            proj_mem = self._search_project_memory(user_message, self.active_project)
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
        """追加一行 trace 记录；单文件超过字节上限时截断保留最新。"""
        try:
            path = self._trace_dir() / filename
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            if path.stat().st_size > TRACE_MAX_BYTES:
                lines = path.read_text(encoding="utf-8").splitlines()[-TRACE_MAX_LINES:]
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
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
                    refs.append({
                        "fp": fp,
                        "section": header[1:].strip(),
                        "date": date,
                        "summary": e[:80],
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

    def build_full_context_for_turn(self, user_message: str = "") -> str:
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
        parts: list[str] = []
        relevant = self.prefetch(user_message)
        if relevant:
            inner = relevant.strip()
            if inner.startswith("<memory-context>"):
                inner = inner[len("<memory-context>"):]
            if inner.endswith("</memory-context>"):
                inner = inner[:-len("</memory-context>")]
            inner = inner.strip()
            if inner:
                parts.append(inner)
        user = load_user(self.base_dir)
        if user and user.strip():
            parts.append(f"## 用户画像\n{user.strip()}")
        if not parts:
            return ""

        # P0-4/8.3：只标注 prefetch 记忆块（parts[0]），USER 画像不标注
        refs: list[dict] = []
        if parts:
            marked, refs = self._annotate_memory_blocks(parts[0])
            parts[0] = marked

        body = "\n\n".join(parts)

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
            body += ref_block
            # 8.3 溯源：记录本轮注入了哪些记忆 id
            self._append_trace(INJECT_TRACE_FILE, {
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "query": (user_message or "")[:100],
                "n": len(refs),
                "refs": refs[:20],
            })

        return "<memory-context>\n" + body + "\n</memory-context>"

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
            save_user(self.base_dir, content)
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
            save_user(self.base_dir, updated)
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
            save_user(self.base_dir, updated)
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
        p = self._global_memory_path()
        if p.is_file():
            return p.read_text(encoding="utf-8")
        return ""

    def _save_global_memory(self, content: str) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._global_memory_path().write_text(content, encoding="utf-8")

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
        """根据 id（中文标题或英文标签）生成 § 头行，附带规范 #标签。"""
        base = identifier.strip().lstrip("#")
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

    def _render_sections(self, preamble: str, sections: list[dict]) -> str:
        blocks: list[str] = []
        if preamble:
            blocks.append(preamble)
        for s in sections:
            header = f"§ {s['header_rest']}" if s["header_rest"] else self._section_header(s["id"])
            body = s["body"].strip()
            blocks.append(f"{header}\n{body}" if body else header)
        return "\n\n".join(blocks)

    def _route_memory_section(self, entry: str, target_section: str | None = None) -> str:
        """为一条记忆决定归属的 § 分节 id。

        优先级：显式 target_section > entry 首行 #标签 > LLM 分类(失败回退) > 关键词匹配现有标题 > 默认「笔记」。
        """
        if target_section:
            return target_section.strip().lstrip("#")

        # 1) entry 首行显式 #标签
        m = re.match(r"^\s*#(\w+)", entry)
        if m:
            return m.group(1)

        # 2) LLM 分类（fail-safe：任何异常都回退）
        if llm_available():
            try:
                tag = self._llm_route_section(entry)
                if tag:
                    return tag
            except Exception as e:
                logger.debug("LLM section routing failed, falling back to keyword match: %s", e)
        memory = self._load_global_memory()
        if memory:
            _, sections = self._parse_sections(memory)
            for s in sections:
                for kw in re.split(r"[\s，。、？！,.\?!\-]+", entry):
                    if len(kw) >= 2 and kw in s["title"]:
                        return s["id"]

        # 4) 默认
        return "笔记"

    def _llm_route_section(self, entry: str) -> str | None:
        """用 LLM 把记忆分到 preference/correction/work/note 之一。失败返回 None。"""
        system = (
            "你是记忆分类器。判断这条记忆应归入哪个 § 分节，只回复一个小写英文标签："
            "preference（用户偏好/习惯）、correction（用户纠正/否定我）、"
            "work（项目/工作进展）、note（其他）。只输出标签本身，不要解释。"
        )
        resp = chat(system, entry, temperature=0.0, max_tokens=20)
        tag = resp.strip().lower()
        if tag in ("preference", "correction", "work", "note"):
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
        # 已有日期前缀：2026-07-31: ... 或 2026-7-31: ...
        if re.match(r"^\d{4}-\d{1,2}-\d{1,2}[:：]", text):
            return text
        today = datetime.date.today().isoformat()
        return f"{today}: {text}"

    def decay_memory(self) -> dict:
        """记忆衰减：按 TTL 规则淘汰过时条目。

        - #work 下超过 30 天的条目 → 归档到 vault
        - #note 下超过 7 天的条目 → 删除
        - #preference / #correction → 永不衰减

        调用时机：每次 cleanup_redundancy 时附带执行，或手动触发。
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

            entries = [e.strip() for e in re.split(r"\n\s*\n", sec["body"]) if e.strip()]
            kept = []
            for entry in entries:
                # 提取日期前缀
                m = re.match(r"(\d{4}-\d{1,2}-\d{1,2})[:：]", entry)
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
                    # 超龄：归档或删除
                    if sec_ttl >= 30:
                        # 长 TTL 的归档到 vault（可恢复）
                        try:
                            self.vault.write_entry(
                                content=entry,
                                category=sec["tags"].intersection({"work", "note", "preference", "correction"}).pop()
                                if sec["tags"] else "note",
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
                        except Exception as e:
                            logger.debug("vault archive on decay failed, keeping entry: %s", e)
                    else:
                        deleted += 1
                        forgot_records.append({
                            "action": "deleted",
                            "section": sec["title"],
                            "date": m.group(1),
                            "summary": entry[:80],
                        })
                    modified = True
                    continue
                kept.append(entry)

            if modified:
                sec["body"] = "\n\n".join(kept)

        if modified:
            self._save_global_memory(self._render_sections("", sections))

        # 8.3 遗忘可见：把「忘了什么」留痕，前端可展示（遗忘与记忆一样透明）
        for rec in forgot_records:
            rec["ts"] = datetime.datetime.now().isoformat(timespec="seconds")
            self._append_trace(FORGOT_TRACE_FILE, rec)

        return {
            "decayed": archived + deleted,
            "archived": archived,
            "deleted": deleted,
            "forgot_count": len(forgot_records),
            "notes": f"归档 {archived} 条，删除 {deleted} 条",
        }

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
            return "条目为空，未写入"

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
                        return "USER.md 已覆盖该信息，跳过"
                    if rel == "CONTRADICTORY":
                        if self._replace_user_entry(dup, entry_s):
                            return "USER.md 已用新信息替换旧条目"
                    # EQUIVALENT 或 NEW（与离线冲突）→ 合并更新
                    merged = self._try_merge_similar(dup, entry_s)
                    if self._replace_user_entry(dup, merged or entry_s):
                        return "USER.md 已合并相似条目"
                else:
                    # 离线近重复且无 LLM → 保守跳过（防 3+ 重复堆积）
                    return "USER.md 已存在近重复条目，跳过"

            # 注：明显无近重复的新条目直接追加，不为每次写入都调 LLM（避免成本/延迟）；
            # LLM 四操作精判仅在「离线已怀疑重复」时触发（见上）。

            # 无近重复 → 追加新条目
            if current and not current.endswith("\n"):
                current = current.rstrip() + "\n"
            updated = (current.rstrip() + "\n\n" + entry_s) if current.strip() else entry_s
            save_user(self.base_dir, updated)
            self._compress_if_needed("user")
            return "USER.md 已追加"

        if section == "memory":
            identifier = self._route_memory_section(entry, target_section)
            # 若 entry 首行带 #标签 且未显式指定分节，去掉该前缀避免重复
            if not target_section:
                entry = re.sub(r"^\s*#\w+\s*", "", entry).strip()

            # 跨 section 去重检查：检查所有 § 下是否已有高度相似的内容
            memory = self._load_global_memory()
            if memory and llm_available():
                cross_dup = self._check_cross_section_duplicate(memory, entry)
                if cross_dup:
                    return f"MEMORY.md 已在 § {cross_dup} 中存在相似条目，跳过"

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
                    return f"MEMORY.md § {identifier} 已存在相同条目，跳过"
                # 同主题合并：用 LLM 检查是否有相似条目，有就合并而非追加
                merged = self._try_merge_similar(found["body"], entry)
                if merged is not None:
                    found["body"] = merged
                else:
                    found["body"] = (found["body"].rstrip() + "\n\n" + entry).strip()
            else:
                new_sec = {
                    "id": identifier,
                    "header_rest": self._section_header(identifier).replace("§ ", "", 1),
                    "title": re.sub(r"#\w+", "", self._section_header(identifier).replace("§ ", "", 1)).strip(),
                    "tags": set(re.findall(r"#(\w+)", self._section_header(identifier))),
                    "body": entry,
                }
                sections.append(new_sec)
            self._save_global_memory(self._render_sections(preamble, sections))
            self._compress_if_needed("memory")
            return f"MEMORY.md 已写入 § {identifier}"

        return f"不支持的 section: {section}"

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

    def _compress_if_needed(self, section: str) -> bool:
        """写入后检查文件是否超限，超限则 LLM 压缩合并。

        Returns:
            True 如果触发了压缩
        """
        if not llm_available():
            return False  # 无 API Key 时不压缩，只靠截断兜底

        if section == "memory":
            content = self._load_global_memory()
            if len(content) <= MEMORY_FILE_MAX_CHARS:
                return False
            compressed = self._llm_compress_memory(content)
            if compressed and len(compressed) < len(content):
                # 压缩淘汰的条目归档到 vault（不丢，可恢复）
                try:
                    self._archive_compressed_entries(content, compressed)
                except Exception as e:
                    logger.debug("archive before compression failed: %s", e)
                self._save_global_memory(compressed)
                return True
        elif section == "user":
            content = load_user(self.base_dir)
            if len(content) <= USER_FILE_MAX_CHARS:
                return False
            compressed = self._llm_compress_user(content)
            if compressed and len(compressed) < len(content):
                save_user(self.base_dir, compressed)
                return True
        return False

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
            save_user(self.base_dir, current.replace(old, new, 1))
            return True
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
        if new_content and new_content != content.strip():
            try:
                (self.base_dir / "USER.md.bak").write_text(content, encoding="utf-8")
            except OSError:
                pass
            save_user(self.base_dir, new_content + "\n")
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
    _KEEP_GLOBAL_SECTIONS = {
        "偏好", "preference", "纠正", "correction",
        "系统", "system", "笔记", "note", "读书记录", "reading",
    }

    def clean_global_memory(self) -> dict:
        """一次性分层净化 MEMORY.md：把「书的记忆」从「你的记忆」里分出来。

        策略（保守、可逆、带 MEMORY.md.bak 备份）：
        - 「关于你」的分节（偏好/纠正/系统/笔记/读书记录）原样保留；
        - 其余分节（如 项目 / 国子监秘闻录 / 工作 等）做**条目级**过滤：
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
            if sid in self._KEEP_GLOBAL_SECTIONS:
                new_sections.append(s)  # 关于你：整段保留
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
        """LLM 压缩 USER.md：精简用户画像，合并重复描述。"""
        system = (
            "你是用户画像压缩器。把一份 USER.md 压缩到更短的篇幅，规则：\n"
            "1. 合并重复或相似的描述\n"
            "2. 保留所有关键偏好、工作习惯、需要避免的事项\n"
            "3. 保留 # 用户画像 的 Markdown 结构\n"
            "4. 每条仍简洁可读\n"
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
            resp = chat(system, prompt, temperature=0.3, max_tokens=600)
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

        🛡️ 只写 MEMORY/USER，绝不写 SOUL。无 LLM 密钥或异常时静默跳过，不阻断主对话。
        """
        if not memory_llm_available():
            return {"written": 0, "notes": "未配置 LLM provider，跳过会话抽取"}

        sessions_dir = self.base_dir / "sessions"
        if not sessions_dir.is_dir():
            return {"written": 0, "notes": "暂无会话记录"}

        files = sorted(sessions_dir.glob("*.md"))[-limit:]
        if not files:
            return {"written": 0, "notes": "暂无会话记录"}

        transcript = []
        for f in files:
            try:
                transcript.append(f.read_text(encoding="utf-8"))
            except OSError:
                continue
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
                    self.append_to_section("user", fact_text)
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
        if not memory_llm_available():
            return {"written": 0, "notes": "未配置 LLM provider，跳过会话抽取"}
        try:
            text = session_file.read_text(encoding="utf-8")
        except OSError:
            return {"written": 0, "notes": "读取 session 文件失败"}
        if not text.strip():
            return {"written": 0, "notes": "空对话"}
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
            f"输出严格 JSON 数组，每个元素 {{category, text}}，text 格式为「YYYY-MM-DD: 一句话陈述」"
            f"（今天日期：{today}）。\n"
            "work 类别同一天如果有多条，应合并为一条摘要（如「2026-07-31: 第二章定稿，标题为财当道」而非拆成三条）。\n"
            "若没有值得记的持久事实，输出空数组 []。"
        )
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
        skipped = 0
        learned: list[str] = []
        for fact in facts:
            category = (fact.get("category") or "").lower()
            fact_text = (fact.get("text") or "").strip()
            if not fact_text:
                continue
            # 写入前全局查重：搜 MEMORY.md + vault
            if self._is_duplicate(fact_text):
                skipped += 1
                continue
            try:
                if category == "preference":
                    result = self.append_to_section("user", fact_text)
                else:
                    result = self.append_to_section("memory", f"#{category} {fact_text}")
                if "跳过" in result:
                    skipped += 1
                else:
                    written += 1
                    learned.append(fact_text)
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

                GrowthEngine(base_dir=self.base_dir).record_memory_learned(written)
            except Exception as e:
                logger.debug("growth engine record_memory_learned failed: %s", e)
        return {"written": written, "skipped": skipped, "learned": learned, "notes": f"已归档 {written} 条（跳过 {skipped} 条重复）"}

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
        if not memory_llm_available():
            return {"written": 0, "notes": "未配置 LLM provider，跳过会话抽取"}
        try:
            session_file = self._save_session_turn(user_msg or "", assistant_msg or "")
        except Exception as e:
            logger.warning("session turn save failed: %s", e)
            session_file = None
        if not session_file:
            return {"written": 0, "notes": "会话保存失败，跳过抽取"}
        return self._extract_single_session(session_file)

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
        # 用时间戳命名
        import time
        ts = int(time.time())
        p = sessions_dir / f"{ts}.md"
        content = f"## User\n{user_msg}\n\n## Assistant\n{assistant_msg}\n"
        p.write_text(content, encoding="utf-8")
        # 只保留最近50个 session 文件
        files = sorted(sessions_dir.glob("*.md"), key=lambda f: f.name)
        if len(files) > 50:
            for f in files[:-50]:
                f.unlink()
        return p
