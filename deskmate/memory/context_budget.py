"""每轮注入上下文的 token 预算 + 闲聊轮重块跳过。

2026-11-10 萧哥排队项：按 token 预算裁剪上下文 + 闲聊轮跳过重块。

实测背景（克隆真实库，build_full_context_for_turn）：
- 每轮注入约 40KB（≈3 万 token）：USER 画像 12.4KB / cognitive 8KB / world 7.5KB /
  prefetch 5.3KB 是 prefill 大头；这些块每轮都全量注入，闲聊一句「早」也一样。
- 结果是模型首包前的 prefill 被拉长，写作类问题也会因为上下文噪声而分心。

本模块只做两件事，且全部 fail-open（任何异常 → 退回不裁剪的旧行为）：

1. 分块预算：每个块带优先级和字符上限；总预算按估算 token 数控制，超预算时
   从最低优先级的可选块整块丢，再不够就截断最大的块；骨架块（时钟/当下心情/
   事实纪律）最后才动。
2. 闲聊轮：短、无写作/书籍/人物线索、不像提问的消息判为闲聊 → 跳过
   cognitive / world / prefetch / lessons / reflection / intent 等重块，
   只留人格画像（短）、用户事实（短）、当下情绪与时钟。

env 旋钮（都可在设置/环境里改，不改代码）：
- DESKMATE_CONTEXT_BUDGET=0         整体关闭预算（恢复旧行为，排障用）
- DESKMATE_CONTEXT_TOKEN_BUDGET     总预算，默认 8000（估算 token，口径见 estimate_tokens）
- DESKMATE_CONTEXT_LIGHT=0          关闭闲聊跳块
- DESKMATE_CONTEXT_LIGHT_MAX_CHARS  闲聊判定的消息长度上限，默认 24

设计取舍：宁可把「画像/认知/世界观」这类长块截断或跳过，也不动「事实纪律 +
用户口述事实 + 当下心情」——后者是萧哥反复强调的「别把记忆当确定事实」的骨架。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# CJK 统一表意文字（含扩展 A / 兼容区）：中文按 ~0.75 token/字 估算（偏保守）
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

_DEFAULT_TOKEN_BUDGET = 13000
_DEFAULT_LIGHT_MAX_CHARS = 24
_ELLIPSIS = "\n…（已按上下文预算节选）"

#: 出现这些词 → 不是闲聊（写作/书籍/检索/任务/工具类意图）。
_WORK_HINTS = (
    "写", "改", "章", "节", "卷", "稿", "正文", "大纲", "细纲", "章纲",
    "设定", "人物", "角色", "剧情", "情节", "伏笔", "线索", "时间线",
    "续", "校对", "润色", "史料", "资料", "查", "搜", "找", "读", "书",
    "分析", "建议", "为什么", "怎么", "如何", "区别", "对比", "第",
    "段", "错", "格式", "导出", "文件", "工作区", "任务", "提醒", "计划",
    "整理", "总结", "翻译", "排版", "封面", "标题",
    # 2026-11-10 补：短问句也可能需要记忆/前文（「林岚后来怎样」「他最后死了吗」
    # 「你还记得吗」「上次那个」）——这些一律不当闲聊，否则她会丢开记忆瞎答。
    "记得", "想起", "回忆", "上次", "之前", "刚才", "昨天", "说过", "聊过",
    "那个", "后来", "结局", "最后", "怎样", "之后", "当时",
)

#: 明确的社交寒暄/情绪短句——即使包含「怎么」这类词也当闲聊（「今天怎么这么累」）。
_LIGHT_HINTS = (
    "早", "晚安", "早安", "午安", "你好", "在吗", "在么", "在不在",
    "谢谢", "多谢", "辛苦", "哈哈", "嘿嘿", "累了", "累", "困了", "困", "烦", "难受",
    "开心", "想你", "抱抱", "吃饭", "睡了", "拜拜", "再见", "好呀", "好啊",
)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name)
        if raw is None or not str(raw).strip():
            return default
        return float(raw)
    except (TypeError, ValueError):
        return default


def budget_enabled() -> bool:
    return os.environ.get("DESKMATE_CONTEXT_BUDGET", "1") != "0"


def token_budget() -> int:
    """总预算（估算 token）。<=0 视为不限制。"""
    return int(_env_float("DESKMATE_CONTEXT_TOKEN_BUDGET", _DEFAULT_TOKEN_BUDGET))


def estimate_tokens(text: str | None) -> int:
    """粗略估算 token 数（中文 0.75/字，其它 0.25/字符；偏保守，不追求精确）。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = max(0, len(text) - cjk)
    return int(cjk * 0.75 + other * 0.25) + 1


def is_light_turn(user_message: str | None) -> bool:
    """这条用户消息是不是「闲聊轮」——判断不了就返回 False（保守，走完整上下文）。

    规则（全部满足才算闲聊）：
    - 开关打开、消息非空；
    - 消息长度不超过阈值（默认 24 字）；
    - 不含《书名》；
    - 不含写作/书籍/检索/任务类词；
    - 例外：命中寒暄/情绪短句时，即使含「怎么」也当闲聊（「今天怎么这么累」）。
    """
    if os.environ.get("DESKMATE_CONTEXT_LIGHT", "1") == "0":
        return False
    msg = (user_message or "").strip()
    if not msg:
        return False
    limit = int(_env_float("DESKMATE_CONTEXT_LIGHT_MAX_CHARS", _DEFAULT_LIGHT_MAX_CHARS))
    if len(msg) > limit:
        return False
    # 只对中文/中英混排的短句启用闲聊判定：纯英文短词（"Python"）常是话题/专著名，
    # 保守按工作轮处理（宁多注入，不丢记忆）。
    if not _CJK_RE.search(msg):
        return False
    if "《" in msg or "》" in msg:
        return False
    if any(h in msg for h in _LIGHT_HINTS):
        return True
    if any(h in msg for h in _WORK_HINTS):
        return False
    return True


_AUTO_REVIEW_RE = re.compile(r"(?ms)^## 自动复盘.*?(?=^## |\Z)")


def strip_auto_review(text: str | None) -> str:
    """去掉「## 自动复盘 · 日期」段：那是每日复盘流水，不是稳定用户画像。

    2026-11-10 实测 USER.md 12.4KB 里 ~9.7KB 是这类日期段，真正稳定的画像
    （基本信息/写作观/协作模式/日常流程）只有 ~2.6KB。注入前剥掉流水段，可以在
    不丢画像的前提下把这一块从 12.4KB 降到 ~2.6KB。
    """
    if not text:
        return ""
    try:
        return _AUTO_REVIEW_RE.sub("", str(text)).strip()
    except Exception:  # noqa: BLE001
        return str(text).strip()


@dataclass
class ContextBlock:
    """一个注入块：优先级（越大越先被丢）、字符上限、闲聊轮是否保留。"""

    name: str
    text: str
    priority: int = 50
    cap: int = 4000
    cap_light: int | None = None
    keep_light: bool = True

    def effective_cap(self, light: bool) -> int:
        if light and self.cap_light is not None:
            return self.cap_light
        return self.cap


def _cap_text(text: str, cap: int) -> str:
    if cap <= 0:
        return ""
    if len(text) <= cap:
        return text
    return text[:cap] + _ELLIPSIS


def render_blocks(
    blocks: list[ContextBlock],
    *,
    user_message: str = "",
    budget_tokens: int | None = None,
    light: bool | None = None,
) -> tuple[str, dict]:
    """按预算/闲聊规则渲染注入块。

    Returns:
        (渲染文本, 统计 dict)：统计含 light/tokens/budget/dropped/trimmed/kept，
        供 trace 与排障（不注入正文）。
    """
    stats: dict = {
        "budget_enabled": budget_enabled(),
        "light": False,
        "tokens": 0,
        "budget": 0,
        "dropped": [],
        "trimmed": [],
        "kept": [],
    }
    try:
        if not budget_enabled():
            kept = [b for b in blocks if str(b.text or "").strip()]
            text = "\n\n".join(str(b.text).strip() for b in kept)
            stats.update({"tokens": estimate_tokens(text), "kept": [b.name for b in kept]})
            return text, stats

        light = is_light_turn(user_message) if light is None else light
        budget = token_budget() if budget_tokens is None else int(budget_tokens)
        stats["light"] = bool(light)
        stats["budget"] = budget

        kept: list[ContextBlock] = []
        for b in blocks:
            text = str(b.text or "").strip()
            if not text:
                continue
            if light and not b.keep_light:
                stats["dropped"].append(b.name)
                continue
            capped = _cap_text(text, b.effective_cap(light))
            if capped != text:
                stats["trimmed"].append(f"{b.name}:cap")
            kept.append(
                ContextBlock(
                    name=b.name,
                    text=capped,
                    priority=b.priority,
                    cap=b.cap,
                    cap_light=b.cap_light,
                    keep_light=b.keep_light,
                )
            )

        def _total() -> int:
            return sum(estimate_tokens(b.text) for b in kept)

        if budget > 0:
            # 第一步：整块丢「明显可选」的长块（priority > 50：信箱/课时/读后感/
            # 反思/意图/溯源）。功能内容（画像/记忆检索/事实/情绪/时钟/认知/分身/
            # 世界观）只截断不整块丢——宁可少注入，也不让开了的功能静默失效。
            for b in sorted(kept, key=lambda x: -x.priority):
                if _total() <= budget:
                    break
                if b.priority <= 50:  # 认知/分身/世界观属「功能内容」：只截断不整块丢
                    continue
                kept.remove(b)
                stats["dropped"].append(b.name)
            # 第二步：仍超预算 → 从最大的骨架块尾部截断（保头：事实纪律在最前）
            guard = 0
            while budget > 0 and _total() > budget and guard < 12:
                guard += 1
                biggest = max(kept, key=lambda x: len(x.text), default=None)
                if biggest is None:
                    break
                excess_tokens = _total() - budget
                cut_chars = max(200, int(excess_tokens / 0.75))
                keep_chars = max(120, len(biggest.text) - cut_chars)
                biggest.text = biggest.text[:keep_chars] + _ELLIPSIS
                stats["trimmed"].append(f"{biggest.name}:budget")

        stats["kept"] = [b.name for b in kept]
        text = "\n\n".join(b.text for b in kept)
        stats["tokens"] = estimate_tokens(text)
        return text, stats
    except Exception as exc:  # noqa: BLE001 — fail-open：预算异常绝不阻断注入
        kept = [b for b in blocks if str(b.text or "").strip()]
        text = "\n\n".join(str(b.text).strip() for b in kept)
        stats["error"] = f"{type(exc).__name__}: {exc}"
        stats["tokens"] = estimate_tokens(text)
        return text, stats
