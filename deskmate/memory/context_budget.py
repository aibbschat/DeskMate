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
2. 闲聊轮：只有明确命中寒暄/情绪短句才判为闲聊 → 跳过
   cognitive / prefetch / lessons / reflection / intent 等重块，
   只留人格画像（短）、用户事实（短）、当下情绪与时钟。认不出来一律按工作轮。
   2026-11-15 修：world 与 user_episodes **不再整块丢**——world 走 compact
   精简配额（见 world_context._quotas），user_episodes 是轻量的真实状态块。
   这两块恰是「越像跟人说话的场合越需要」的部分，整块跳过等于把她自己抽掉。

env 旋钮（都可在设置/环境里改，不改代码）：
- DESKMATE_CONTEXT_BUDGET=0         整体关闭预算（恢复旧行为，排障用）
- DESKMATE_CONTEXT_TOKEN_BUDGET     总预算，默认 13000（估算 token，口径见 estimate_tokens）
- DESKMATE_CONTEXT_LIGHT=0          关闭闲聊跳块
- DESKMATE_CONTEXT_LIGHT_MAX_CHARS  闲聊判定的消息长度上限，默认 24
- 2026-11-10 修误判：闲聊改为白名单制（只有寒暄/情绪短句算闲聊），认不出来一律走
  完整上下文；「好了没/进度到哪了」这类进度追问强制按工作轮处理。

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
#: 2026-11-10 修误判：这组判断必须排在寒暄词之前。此前「早上好，接着写第二章」
#: 会被单字「早」短路成闲聊，把 prefetch/cognitive/world/lessons/intent 整块丢掉。
_WORK_HINTS = (
    "写", "改", "章", "节", "卷", "稿", "正文", "大纲", "细纲", "章纲",
    "设定", "人物", "角色", "剧情", "情节", "伏笔", "线索", "时间线",
    "续", "校对", "润色", "史料", "资料", "查", "搜", "找", "读", "书",
    "分析", "建议", "第",
    "段", "错", "格式", "导出", "文件", "工作区", "任务", "提醒", "计划",
    "整理", "总结", "翻译", "排版", "封面", "标题",
    # 任务动作词：短指令（「执行」「生成一份周报」）同样是工作轮。
    "生成", "执行", "部署", "提交", "测试",
    # 话题延续：需要前文/反思/记忆才接得上（「我们接着聊」不是寒暄）。
    "接着聊", "继续聊", "接着说", "然后呢",
    # 2026-11-10 补：短问句也可能需要记忆/前文（「林岚后来怎样」「他最后死了吗」
    # 「你还记得吗」「上次那个」）——这些一律不当闲聊，否则她会丢开记忆瞎答。
    "记得", "想起", "回忆", "上次", "之前", "刚才", "昨天", "说过", "聊过",
    "那个", "后来", "结局", "最后", "怎样", "之后", "当时",
)

#: 任务进度追问：这一轮最需要 intent / delivered 块（她做过什么、递过什么）。
#: 2026-11-10 生产 trace 里「好了没」被误判成闲聊，恰好在问任务进度。
_PROGRESS_HINTS = (
    "好了没", "好了吗", "好了么", "做完没", "做完么", "做完了吗", "完成了吗",
    "完成没", "搞定了没", "搞定了吗", "进度", "到哪了", "跑到哪", "结果呢",
    "怎么样了", "写完了吗",
)

#: 明确的社交寒暄/情绪短句——这是「闲聊轮」的**唯一**放行依据。
#: 2026-11-10 修误判：去掉「早 / 累 / 困 / 烦」这类单字——它们出现在正常任务句里
#: （「我早说过这段要删」「麻烦把第四章大纲整理一下」）。单字问候按整句匹配处理。
_LIGHT_HINTS = (
    "早上好", "早安", "午安", "晚安", "你好", "在吗", "在么", "在不在",
    "谢谢", "多谢", "辛苦", "哈哈", "嘿嘿", "好累", "很累", "累了", "累死了",
    "好困", "很困", "困了", "好烦", "很烦", "烦死", "心烦", "怎么这么累",
    "开心", "想你", "抱抱", "吃饭", "吃了吗", "睡了", "睡了吗", "忙吗", "难受",
    "在干嘛", "干嘛呢", "叫什么名字", "拜拜", "再见", "好呀", "好啊",
)

#: 只有这些单独成句时才算问候（避免「早」当成子串误伤）。
_GREETING_ONLY = ("早", "早呀", "早啊")


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
    """这条用户消息是不是「闲聊轮」。

    2026-11-10 修误判后改为**白名单制**：只有明确命中社交寒暄/情绪短句（或单独成句的
    「早」）才算闲聊；认不出来一律按工作轮走完整上下文。此前「短句 + 无关键词」默认
    当闲聊，导致「正史」「早上好，接着写第二章」这类任务轮被砍掉 prefetch/认知/
    世界观/课时/意图（回答变薄、重复已纠正过的错）。

    判定顺序：
    - 开关打开、消息非空、长度不超阈值、含中文、不含《书名》；
    - 单独成句的问候 → 闲聊；
    - 任务进度追问（「好了没」「进度到哪了」）→ 工作轮（这轮最需要 intent）；
    - 工作线索（写作/检索/任务/话题延续）→ 工作轮，**先于**寒暄判定；
    - 命中寒暄/情绪白名单 → 闲聊；
    - 其余 → 工作轮（保守）。
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
    if msg.strip("！!。.,，~～?？ ") in _GREETING_ONLY:
        return True
    if any(h in msg for h in _PROGRESS_HINTS):
        return False
    if any(h in msg for h in _WORK_HINTS):
        return False
    if any(h in msg for h in _LIGHT_HINTS):
        return True
    # 认不出来就当工作轮：多注入一点只是慢，少注入了会让她答得薄、答错。
    return False


_AUTO_REVIEW_HEADING_RE = re.compile(r"^##\s+自动复盘")

#: 2026-11-11 C 方案：非常驻偏好层也不进常驻注入（改由关键词兜底按需取用）。
#: 与「自动复盘」同样按行剥 bullet，段内非 bullet 段落照旧保留。
_NON_RESIDENT_HEADING_RE = re.compile(r"^##\s+(?:自动复盘|偏好与事实)")

#: 复盘段里的流水行（bullet）。真身是 LLM 每天写的一行行「- ...」。
_REVIEW_BULLET_PREFIXES = ("-", "*", "•", "·")


def strip_auto_review(text: str | None) -> str:
    """去掉「## 自动复盘 · 日期」段的流水条目，不是稳定用户画像。

    2026-11-10 实测 USER.md 12.4KB 里 ~9.7KB 是这类日期段，真正稳定的画像
    （基本信息/写作观/协作模式/日常流程）只有 ~2.6KB。注入前剥掉流水段，可以在
    不丢画像的前提下把这一块从 12.4KB 降到 ~2.6KB。

    2026-11-10 二次修复：记忆写入把长期事实追加到 USER.md 文末，而文末正好落在
    最后一个「## 自动复盘」标题后面。旧实现用正则一直删到文件末尾，把「《夜不收》
    正稿固定位置」「桌面那份是旧快照」这类长期事实一起吞了——她因此每次都要重新
    找稿子，还会误读桌面上的旧快照。现在按行剥：复盘段只删 bullet 流水，段内非
    bullet 的独立段落（= 后追加的长期事实）保留。

    2026-11-11 C 方案：非常驻偏好层（``## 偏好与事实``）与复盘段同法剥掉——它不进
    常驻注入，改由 ``user_preferences.recall_preferences`` 按本轮关键词少量召回。
    """
    if not text:
        return ""
    try:
        out: list[str] = []
        in_review = False
        for line in str(text).split("\n"):
            if _NON_RESIDENT_HEADING_RE.match(line):
                in_review = True
                continue
            if in_review:
                if line.startswith("## "):
                    in_review = False
                    out.append(line)
                    continue
                stripped = line.lstrip()
                if not stripped:
                    continue
                if stripped[0] in _REVIEW_BULLET_PREFIXES:
                    continue
                # 非 bullet：这是记忆写入落在文末的长期事实，必须保留。
                out.append(line)
                continue
            out.append(line)
        return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
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
