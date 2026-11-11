"""P1-7 无密钥记忆兜底——本地轻量规则抽取器（零 LLM）。

当用户未配置 LLM provider 时，``extract_session_facts`` / ``_extract_single_session``
不再静默跳过，而是用本模块的纯规则从对话中提取关键事实，避免「越用越懂你」断链。

设计原则：
- **零 LLM**：纯正则 + 关键词，不依赖任何外部 API
- **高精确率**：宁可少抽不可错抽——只匹配高置信度的句式，不做模糊推断
- **只从用户消息提取**：解析 session 的 ``## User`` 段，忽略 AI 的话
- **输出格式与 LLM 抽取一致**：``[{category, text}]``，text 带 ``YYYY-MM-DD: `` 前缀
- **三类事实**：preference（偏好/习惯/个人信息）、correction（纠正）、work（项目进展）
- **fail-open**：任何异常返回空列表，绝不阻断主对话
"""

from __future__ import annotations

import datetime
import re
from typing import Any

# ---------------------------------------------------------------------------
# session 解析
# ---------------------------------------------------------------------------

_USER_SECTION_RE = re.compile(r"^##\s*User\s*$", re.IGNORECASE | re.MULTILINE)
_SECTION_HEADER_RE = re.compile(r"^##\s+\w+", re.MULTILINE)


def _extract_user_messages(text: str) -> list[str]:
    """从 session markdown 中提取所有 ``## User`` 段的文本。"""
    if not text:
        return []
    messages: list[str] = []
    # 按 ## 分段
    parts = _SECTION_HEADER_RE.split(text)
    headers = _SECTION_HEADER_RE.findall(text)
    for header, body in zip(headers, parts[1:]):
        if "user" in header.lower():
            cleaned = body.strip()
            if cleaned:
                messages.append(cleaned)
    return messages


# ---------------------------------------------------------------------------
# 事实提取规则（保守、高精确率）
# ---------------------------------------------------------------------------

# 句末分隔（中文标点 + 换行）
_SENTENCE_END = r"[。！？\n；;]"

# preference：明确的偏好/习惯表达
_PREF_PATTERNS = [
    # 我喜欢/偏好/习惯/更爱/倾向于 X
    (r"我(?:喜欢|偏好|习惯|更爱|倾向于|更习惯|一般用|通常用|一直用)([^。！？\n；;]{2,40})",
     "用户喜欢{content}"),
    # 以后/下次都 X
    (r"(?:以后|下次)(?:都|要|就)?([^。！？\n；;]{2,40})",
     "用户习惯{content}"),
    # 不要/别/不准 X（否定偏好）
    (r"(?:不要|别|不准|不喜欢)([^。！？\n；;]{2,30})",
     "用户不希望{content}"),
    # 我叫/是/在/来自 X（个人信息）
    (r"我(?:叫|是|在|来自|住在|工作于|从事)([^。！？\n；;]{2,30})",
     "用户信息：{content}"),
]

# correction：用户纠正 AI
_CORRECTION_PATTERNS = [
    (r"(?:不对|不是|错了|你搞错了|纠正一下|应该是|实际是|事实是|正确的是)([^。！？\n；;]{2,50})",
     "用户纠正：{content}"),
]

# work：项目进展（书名 + 关键动作 / 章节 + 定稿）
_WORK_BOOK_RE = re.compile(r"《([^》]{1,30})》")
_WORK_ACTION_RE = re.compile(
    r"(定稿|完成|写完|写好了|设定|决定|大纲|章节|开头|结尾|主角|反派|人物关系|世界观)"
)
_WORK_CHAPTER_RE = re.compile(
    r"第([一二三四五六七八九十百\d]+)[章节回集]"
)


def _clean_content(s: str) -> str:
    """清理提取出的内容片段：去首尾空白/标点/语气词。"""
    s = s.strip().strip("，,、：:。！？；;")
    # 去掉开头的"了/的/地/得"等助词
    s = re.sub(r"^(了|的|地|得|要|会|能|可以|应该)\s*", "", s)
    return s.strip()


# 2026-11-11 一段止血：残句入库的三道闸。
#
# 旧规则是「关键词 + 后面任意 2~50 字」，于是「不对，结束）」被抽成
# 「用户纠正：结束）」、「我习惯比对>」被抽成「用户习惯比对>」。这些半截
# 片段进了 MEMORY.md 会被当成永久事实反复注入，还会污染 dream/反思。
# 这里在出口处统一判断「是不是一句完整的话」，宁可少抽不可错抽。
_TRUNCATED_TAILS = (
    ">", "…", "...", "：", ":",
    # 2026-11-11 补：分隔线与破折号也是「话没说完就被结构切开」的形态。
    # 典型事故：目标制重试提示里的 ``不要再原样重复）---`` 被抽成
    # 「用户不希望再原样重复）---」——它既不是句子，也不该成为永久偏好。
    # 只收多字符分隔线，不收单个 "-"（英文连字符/负数结尾是合法写法）。
    "---", "--", "——", "—", "–", "==", "***", "___", "···",
)
# 2026-11-11 复检修：不能把「以右括号/右引号结尾」一律判残句——那会误杀
# 「我喜欢《明朝那些事儿》」「不喜欢太吵的环境（比如咖啡厅）」。真正的信号是
# **右括号多于左括号**（话被从中间切开），成对出现则是合法收尾。
_BRACKET_PAIRS = (
    ("（", "）"), ("(", ")"), ("【", "】"), ("[", "]"),
    ("《", "》"), ("〈", "〉"), ("「", "」"), ("『", "』"),
)


def _has_dangling_closer(s: str) -> bool:
    for opener, closer in _BRACKET_PAIRS:
        if s.count(closer) > s.count(opener):
            return True
    if s.count("”") > s.count("“") or s.count("’") > s.count("‘"):
        return True
    # ASCII 引号：奇数个才说明有一个没闭合
    if s.count('"') % 2 == 1 or s.count("'") % 2 == 1:
        return True
    return False


# Markdown 水平分隔线（整行 --- / === / ___ / *** / ——）：片段跨过分隔线说明
# 正则已经吃到了两块本不相连的文本边界，一律视为残句。
_SEPARATOR_LINE_RE = re.compile(r"(?m)^\s*(?:-{2,}|={2,}|\*{3,}|_{3,}|—{2,})\s*$")
_TRUNCATED_TAIL_WORDS = (
    "结束", "继续", "如下", "以下", "比如", "例如", "包括", "以及", "还有",
    "然后", "接着", "于是", "所以", "但是", "不过", "如果", "因为", "的话",
    "比对",
)
_QUOTE_LEADING_RE = re.compile(r"^(?:他说|她说|原文|引用|书上说|资料说|有人|据说)")
# 信息量下限：中英数字符至少 2 个（「喝茶」「早起」是合法偏好，
# 「结束」「比对」这类半句已由尾部词黑名单拦截）。
_MIN_INFO_CHARS = 2


def _is_complete_fragment(s: str) -> bool:
    """判断抽出的片段是不是一句完整、可入库的话（防残句）。

    三道闸：
    1. 截断符收尾（``结束）`` / ``比对>``）——原文被切断了；
    2. 半句词收尾（``计划时间继续``）——语义没有闭合；
    3. 去掉标点后中文字符不足 3 个——没有信息量。
    另外排除「他说/原文/引用」开头的复述，避免把别人的话记成用户的偏好。
    """
    s = (s or "").strip()
    if not s:
        return False
    if s.endswith(_TRUNCATED_TAILS):
        return False
    if _has_dangling_closer(s):
        return False
    if _SEPARATOR_LINE_RE.search(s):
        return False
    if any(s.endswith(w) for w in _TRUNCATED_TAIL_WORDS):
        return False
    if _QUOTE_LEADING_RE.match(s):
        return False
    core = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s)
    return len(core) >= _MIN_INFO_CHARS


def _extract_preferences(user_text: str) -> list[str]:
    """从用户文本中提取偏好类事实。"""
    facts: list[str] = []
    seen: set[str] = set()
    for pattern, template in _PREF_PATTERNS:
        for m in re.finditer(pattern, user_text):
            content = _clean_content(m.group(1))
            if len(content) < 2:
                continue
            # 过滤明显的非偏好（如"我是说"、"我在想"）
            if content in ("说", "想", "问", "看", "听", "写", "做", "找"):
                continue
            if not _is_complete_fragment(content):
                continue
            fact = template.format(content=content)
            if fact not in seen:
                seen.add(fact)
                facts.append(fact)
    return facts


def _extract_corrections(user_text: str) -> list[str]:
    """从用户文本中提取纠正类事实。"""
    facts: list[str] = []
    seen: set[str] = set()
    for pattern, template in _CORRECTION_PATTERNS:
        for m in re.finditer(pattern, user_text):
            content = _clean_content(m.group(1))
            if len(content) < 2:
                continue
            if not _is_complete_fragment(content):
                continue
            fact = template.format(content=content)
            if fact not in seen:
                seen.add(fact)
                facts.append(fact)
    return facts


def _extract_work(user_text: str) -> list[str]:
    """从用户文本中提取项目进展类事实。"""
    facts: list[str] = []
    seen: set[str] = set()

    # 书名 + 关键动作 → 项目进展
    books = _WORK_BOOK_RE.findall(user_text)
    actions = _WORK_ACTION_RE.findall(user_text)
    if books and actions:
        for book in books:
            for action in actions[:2]:  # 每本书最多关联 2 个动作
                fact = f"作品《{book}》：{action}"
                if fact not in seen:
                    seen.add(fact)
                    facts.append(fact)

    # 章节 + 定稿/完成 → 章节进展
    chapters = _WORK_CHAPTER_RE.findall(user_text)
    if chapters and any(a in user_text for a in ("定稿", "完成", "写完", "写好了")):
        for ch in chapters[:2]:
            fact = f"第{ch}章已定稿/完成"
            if fact not in seen:
                seen.add(fact)
                facts.append(fact)

    # 只有书名没有动作 → 记录为"正在创作"（弱信号，仅当书名首次出现）
    if books and not actions and not chapters:
        for book in books[:1]:
            fact = f"用户正在创作作品《{book}》"
            if fact not in seen:
                seen.add(fact)
                facts.append(fact)

    return facts


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def extract_facts_local(text: str, date: str | None = None) -> list[dict[str, str]]:
    """从 session 文本中本地规则抽取事实（零 LLM）。

    Args:
        text: session markdown 全文（含 ``## User`` / ``## Assistant`` 分段）
        date: 日期前缀，默认今天

    Returns:
        ``[{category, text}]``，与 LLM 抽取输出格式一致。text 带 ``YYYY-MM-DD: `` 前缀。
    """
    if not text or not text.strip():
        return []

    if date is None:
        date = datetime.date.today().isoformat()

    try:
        user_messages = _extract_user_messages(text)
        if not user_messages:
            return []

        all_user_text = "\n".join(user_messages)
        results: list[dict[str, str]] = []
        seen_texts: set[str] = set()

        def _add(category: str, fact: str) -> None:
            if fact in seen_texts:
                return
            seen_texts.add(fact)
            results.append({"category": category, "text": f"{date}: {fact}"})

        # preference
        for f in _extract_preferences(all_user_text):
            _add("preference", f)
        # correction
        for f in _extract_corrections(all_user_text):
            _add("correction", f)
        # work
        for f in _extract_work(all_user_text):
            _add("work", f)

        return results
    except Exception:
        # fail-open：任何异常返回空，绝不阻断
        return []


def extract_facts_from_sessions(
    sessions: list[str], date: str | None = None
) -> list[dict[str, str]]:
    """从多个 session 文本中抽取事实（合并去重）。"""
    all_facts: list[dict[str, str]] = []
    seen: set[str] = set()
    for text in sessions:
        for fact in extract_facts_local(text, date):
            key = fact["text"]
            if key not in seen:
                seen.add(key)
                all_facts.append(fact)
    return all_facts
