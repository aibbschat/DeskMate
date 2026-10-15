"""动态路由 judge 层（借鉴 PilotDeck classifyAndRoute，2026-10-15）。

## 目标
让「强模型干难的、弱模型干简单的」自动发生：对单条用户消息做难度分类，
简单 → 免费组（Kilo），复杂 → 付费组（ark→小米→腾讯）。

## 与 PilotDeck 的取舍（重要，萧哥拍板前先讲清楚）
PilotDeck 用一个小号 **judge LLM** 做分类。我们第一版用**启发式分类器**：
- 零额外 LLM 调用 → 零延迟、零成本（这恰恰是个"省钱"功能，再为它发一次 LLM
  调用是自相矛盾）；
- 确定性、fail-open 由构造保证（永远不会因分类失败而卡住或选错）；
- 结构上预留 ``backend="llm"`` 扩展点（见 ``classify_difficulty``），需要时再接一个小号
  judge 模型——那是忠实复刻 PilotDeck 的升级路径，不在第一版。

## 接入方式（中央钩子）
所有模型调用收口在 ``ProviderRouter.complete/stream``，这两方法本身拿到 messages，
故 judge 层只在「model == ``AUTO_MODEL`` 哨兵 且 总开关开启」时介入，否则完全透传——
默认零行为改动，且不越权替用户换模型（对齐 ``paid_fallback`` 的红线原则）。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 哨兵模型名：会话模型设为它 + 总开关开启时，才按 turn 动态选组。
AUTO_MODEL = "auto"

#: 难度分类结果。
TIER_SIMPLE = "simple"
TIER_COMPLEX = "complex"

GROUP_FREE = "free"
GROUP_PAID = "paid"


def last_user_text(messages: List[dict[str, Any]]) -> Optional[str]:
    """从 messages 里取最后一条 user 文本（content 可能是 str 或 OpenAI content-parts）。

    取不到（没有 user 消息 / content 为空）返回 None。
    """
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = [
                p.get("text", "")
                for p in c
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            joined = "".join(parts)
            return joined or None
        return None
    return None


def is_short_continuation(text: str) -> bool:
    """短续接消息（「好」「继续」「ok」「嗯」等）—— 继承上一轮分组，避免被误判为简单任务。

    对齐 PilotDeck 的 ``isShortContinuation``：用户一句「好的」不该触发免费组降级。
    """
    if not text:
        return True
    t = text.strip()
    if len(t) <= 2:  # 纯短词（含标点/单字）
        return True
    low = t.lower()
    if low in {
        "好", "好的", "好。", "继续", "继续。", "可以", "可以。", "收到", "对", "是的",
        "嗯", "嗯。", "ok", "ok.", "yes", "go", "好的。",
    }:
        return True
    return False


def classify_difficulty(text: str, *, backend: str = "heuristic") -> str:
    """难度分类：返回 ``TIER_SIMPLE`` / ``TIER_COMPLEX``。

    - ``backend="heuristic"``（默认）：规则判定，确定性、零成本、fail-open。
    - ``backend="llm"``：预留给小号 judge 模型（暂未实现，需要 secrets/provider 接线）。

    fail-open：空消息、异常、未知 backend 一律返回 ``TIER_COMPLEX``（回落强模型），
    绝不卡主流程、绝不因分类失败而选错。
    """
    if not text or not text.strip():
        return TIER_COMPLEX  # 空消息按复杂处理（保守，走强模型）
    if backend == "heuristic":
        try:
            return _classify_heuristic(text)
        except Exception:  # noqa: BLE001
            logger.debug("启发式分类异常，fail-open 到复杂", exc_info=True)
            return TIER_COMPLEX
    # 未知 backend 或未来 llm 未就绪 → fail-open 到复杂
    logger.debug("难度分类 backend=%r 不可用，fail-open 到复杂", backend)
    return TIER_COMPLEX


def _classify_heuristic(text: str) -> str:
    """启发式难度判定（确定性，无外部依赖）。

    命中任一「复杂信号」即判复杂；否则简单。

    复杂信号分四类（避免单一维度误判）：
    - 代码类：写/调试/重构代码、报错、SQL、API、正则、编译、堆栈等；
    - 长文写作类：起草/润色/扩写/论文/报告/小说章节等（用多字短语，不用单字「写」防误伤）；
    - 推理分析类：分析/对比/为什么/如何/方案/架构/设计/评估等；
    - 工具执行类：运行/安装/部署/修复/抓取/构建等。
    - 长度兜底：> 300 字的长指令通常更复杂。
    """
    t = text
    low = t.lower()

    code_signals = [
        "```", "代码", "函数", "调试", "debug", "bug", "报错", "重构", "正则", "regex",
        "sql", " api ", "脚本", "python", "javascript", "typescript", "堆栈",
        "stack trace", "编译", "运行报错", "异常", "报错信息",
    ]
    long_form_signals = [
        "起草", "润色", "扩写", "论文", "报告", "小说", "章节", "大纲", "帮我写",
        "写一篇", "起草一份", "改写成", "总结成", "写一份", "撰写",
    ]
    reasoning_signals = [
        "分析", "对比", "为什么", "如何", "方案", "架构", "设计", "推理", "解释一下",
        "区别", "优劣", "评估", "梳理", "推演", "论证", "推导",
    ]
    tool_signals = [
        "运行", "执行", "安装", "部署", "修复", "抓取", "爬", "查一下", "查收",
        "下载", "构建", "build", "deploy", "install", "execute", "运行它",
    ]

    for s in code_signals + long_form_signals + reasoning_signals + tool_signals:
        if s in t or s in low:
            return TIER_COMPLEX
    if len(t) > 300:
        return TIER_COMPLEX
    return TIER_SIMPLE


def select_group_for_turn(
    messages: List[dict[str, Any]],
    *,
    classifier: Callable[[str], str] = classify_difficulty,
    fallback: str = GROUP_PAID,
    prev_group: Optional[str] = None,
) -> str:
    """给定 messages，返回这一 turn 该走哪组（``GROUP_FREE`` / ``GROUP_PAID``）。

    - 取最后一条 user 文本；取不到 → fail-open 到 ``fallback``（默认付费/强）。
    - 短续接消息且 ``prev_group`` 已知 → 继承 ``prev_group``（不对续接重新分级）。
    - 否则 classifier 判定：simple→free，complex→paid。
    - 任何异常 → ``fallback``。

    纯函数，无副作用，便于单测。
    """
    try:
        text = last_user_text(messages)
        if text is None:
            return fallback
        if prev_group is not None and is_short_continuation(text):
            return prev_group
        tier = classifier(text)
        return GROUP_FREE if tier == TIER_SIMPLE else GROUP_PAID
    except Exception:  # noqa: BLE001
        logger.debug("select_group_for_turn 异常，fail-open 到 %s", fallback, exc_info=True)
        return fallback


def resolve_auto_model(
    messages: List[dict[str, Any]],
    *,
    dynamic_enabled: bool = True,
    prev_group: Optional[str] = None,
    fallback: str = GROUP_PAID,
    route_fn: Optional[Callable[[str], Tuple[Optional[Any], list]]] = None,
) -> Optional[str]:
    """把 ``AUTO_MODEL`` 哨兵解析成具体通道（``provider:model``）。

    仅在 ``dynamic_enabled`` 时生效；返回 ``None`` 表示「不干预，沿用原模型」。

    流程：
    1. ``select_group_for_turn`` → 选组；
    2. 经 ``route_fn(group)`` 取该组一条可试通道（默认 ``deskmate.llm_channels.route``）；
    3. 免费组无可用通道 → 回落付费组；
    4. 付费组也无可用通道 → 返回 ``None``（调用方回落到硬编码默认强模型）。

    fail-open：任何异常返回 ``None``（不干预主流程）。
    """
    if not dynamic_enabled:
        return None
    try:
        group = select_group_for_turn(messages, prev_group=prev_group, fallback=fallback)
        rf = route_fn or _default_route
        ch, _ = rf(group)
        if ch is not None:
            return ch.key
        if group == GROUP_FREE:
            # 免费组全熔 → 回落付费组（绝不因为免费组挂了就让任务失败）
            ch, _ = rf(GROUP_PAID)
            if ch is not None:
                return ch.key
        return None
    except Exception:  # noqa: BLE001
        logger.debug("resolve_auto_model 异常，fail-open 不干预", exc_info=True)
        return None


def _default_route(group: str):
    from deskmate.llm_channels import route as _route

    return _route(group)
