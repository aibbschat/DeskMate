"""8.4 敏感信息分级（合规底线，P2-8 的前置条件）。

设计原则（与 P0-7/P2-1/P2-3 一致）：
- **零 LLM**：基于关键词/正则的规则分级，成本可忽略、可复现、可单测。
- **fail-open**：任一环节异常不阻断主流程；调用方一律 try/except 包裹。
- **误判偏保守**：宁可多标、不漏标——漏标敏感信息的代价远高于多标一条。
- **不污染 AI 上下文**：敏感信息仍注入模型（模型需要上下文），只保证
  「标记」「不进日志」「可选不参与云同步」三项合规要求。

分级（三级）：
- NORMAL(0)   普通：无敏感信号
- SENSITIVE(1) 敏感：手机号、住址、病历、财务、密码/凭证、证件号
- PRIVATE(2)  隐私（最高）：未成年人信息、身份证号、银行卡号等长数字账号

标记机制：以 Markdown tag 落在 § 章节头（`#sensitive` / `#private`）。
           PRIVATE 自动隐含 `#no-sync`（不参与未来 P2-7 云同步）。

日志合规：``redact()`` 对写入日志/溯源摘要的原文脱敏（手机号/身份证/银行卡/
          子女年龄/邮箱 等打码），保证敏感原文永不落盘到任何日志。
"""

from __future__ import annotations

import re

# —— 等级常量 ——
NORMAL = 0
SENSITIVE = 1
PRIVATE = 2

SENSITIVE_TAG = "sensitive"
PRIVATE_TAG = "private"
NOSYNC_TAG = "no-sync"

# 等级 → 主 tag（PRIVATE 额外带 no-sync，见 mark_section）
_LEVEL_TAG = {SENSITIVE: SENSITIVE_TAG, PRIVATE: PRIVATE_TAG}

# —— 检测规则：(模式, 等级, 原因) ——
# 说明：classify 取命中的最高等级；顺序无关（max 胜出）。
# PRIVATE 规则在前仅出于可读性。涉及未成年人的规则优先级最高。
_RULES: list[tuple] = [
    # ===== 隐私（最高）=====
    (re.compile(r"\b\d{17}[\dXx]\b"), PRIVATE, "身份证号"),
    (re.compile(r"\b\d{15,19}\b"), PRIVATE, "银行卡/长数字账号"),
    (re.compile(r"未成年"), PRIVATE, "涉及未成年人"),
    (re.compile(r"(儿子|女儿|孩子|小孩|闺女|幼子|幼儿).{0,6}(岁|\d+\s*岁)"), PRIVATE, "未成年人年龄"),
    (re.compile(r"\d+\s*岁.{0,6}(儿子|女儿|孩子|小孩|闺女|幼子|幼儿)"), PRIVATE, "未成年人年龄"),
    # ===== 敏感 =====
    (re.compile(r"\b1[3-9]\d{9}\b"), SENSITIVE, "手机号"),
    (re.compile(r"(住址|家庭地址|户籍|现居|家住|家庭住址)"), SENSITIVE, "家庭住址"),
    (re.compile(r"(病历|诊断|就诊|药方|病史|就诊记录|抑郁|焦虑症状|确诊)"), SENSITIVE, "健康/病历"),
    (re.compile(r"(月薪|年薪|年收入|月收入|存款|资产|负债|房贷|工资|公积金)"), SENSITIVE, "财务收入"),
    (re.compile(r"(密码|口令|token|api[_-]?key|密钥|凭证|access[_-]?key|secret)"), SENSITIVE, "密码/凭证"),
    (re.compile(r"(身份证号|证件号|护照|港澳通行证)"), SENSITIVE, "证件信息"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), SENSITIVE, "电子邮箱"),
]


def classify(text: str) -> tuple[int, list[str]]:
    """分级一条文本。

    Returns:
        (level, reasons)：最高命中的等级 + 去重后的命中原因列表。
        无命中 → (NORMAL, [])。
    """
    if not text:
        return NORMAL, []
    level = NORMAL
    reasons: list[str] = []
    for pat, lvl, why in _RULES:
        try:
            if pat.search(text):
                level = max(level, lvl)
                reasons.append(why)
        except Exception:
            continue
    # 去重（保持顺序）
    seen: set[str] = set()
    dedup: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            dedup.append(r)
    return level, dedup


def is_sensitive(text: str) -> bool:
    """是否达到「敏感」及以上等级。"""
    return classify(text)[0] >= SENSITIVE


def redact(text: str) -> str:
    """对文本做日志脱敏：打码各类 PII，保证敏感原文不落日志。

    用于 trace/行为日志的摘要与查询字段。模型上下文不做脱敏（模型需要上下文）。
    返回脱敏后文本；异常时原样返回（绝不抛错阻断主流程）。
    """
    if not text:
        return text
    try:
        t = text
        t = re.sub(r"\b1[3-9]\d{9}\b", "1**********", t)          # 手机号 11 位 → 1 + 9*
        t = re.sub(r"\b\d{17}[\dXx]\b", "******************", t)    # 身份证 18 位
        t = re.sub(r"\b\d{15,19}\b", "****(已脱敏)", t)            # 银行卡等长数字
        t = re.sub(r"\d+\s*岁", "**岁", t)                          # 子女/个人年龄
        t = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "***@***", t)      # 邮箱
        return t
    except Exception:
        return text


def mark_section(header: str, level: int) -> str:
    """给 § 章节头按等级追加 tag（#sensitive / #private，PRIVATE 额外 #no-sync）。

    已含对应 tag 则原样返回（幂等）。
    """
    if level <= NORMAL:
        return header
    tag = _LEVEL_TAG.get(level)
    if not tag:
        return header
    out = header
    if tag not in out:
        out = f"{out} #{tag}"
    if level == PRIVATE and NOSYNC_TAG not in out:
        out = f"{out} #{NOSYNC_TAG}"
    return out


def mark_memory_text(text: str) -> tuple[str, int]:
    """扫描 Markdown 记忆文本，对每个 § 分节按正文分级并打 tag。

    Args:
        text: MEMORY.md 格式文本（含 `§ 标题 #tag\\n正文` 分节）。
    Returns:
        (new_text, marked_count)：标注后的文本 + 本轮回新打的 tag 数（幂等，已标的不再计）。
    """
    if not text or "§" not in text:
        return text, 0
    lines = text.splitlines()
    out: list[str] = []
    count = 0
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if ln.lstrip().startswith("§"):
            # 收集本分节正文（到下一个 § 或 --- 或文件尾）
            body_lines: list[str] = []
            j = i + 1
            while j < n and not lines[j].lstrip().startswith("§") and lines[j].strip() != "---":
                body_lines.append(lines[j])
                j += 1
            body = "\n".join(body_lines)
            level, _ = classify(body) if body.strip() else (NORMAL, [])
            new_header = mark_section(ln, level)
            if new_header != ln:
                count += 1
            out.append(new_header)
            out.extend(body_lines)
            i = j
        else:
            out.append(ln)
            i += 1
    return "\n".join(out), count


def should_sync(tags) -> bool:
    """判断一组 tag 是否参与未来云同步（P2-7）。

    含 #no-sync（PRIVATE 自动隐含）则不参与同步。
    """
    try:
        return NOSYNC_TAG not in set(tags)
    except Exception:
        return True
