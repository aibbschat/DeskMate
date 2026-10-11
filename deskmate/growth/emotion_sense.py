"""情感感知——从他**亲口写的日记**里读出这一天的心情（E3 的核心）。

## 为什么需要它

情感层原来的「感知」全是硬编码规则：花种表写死情绪、情感时间线按事件类型
贴标签。后果是——他 09-01 在日记里写「下午第三章写顺了，真好」「心里踏实」，
系统给的判定是**压力大**（因为那天有一次打回）；09-05 写「有些事情不能去
细想，细想就会郁闷」，判定是**平静**。

他明明写下了心情，系统却拿一个恒为 80 的假数值去猜。猜错还只是难堪，
真正的问题是：**小桔会基于错误判断去关怀**——他开心时安慰他，比不说话更惊悚。

所以这一层的原则是：**他自己说的（自述）永远压过我们观察的（推断）。**

## 两条腿

1. **LLM（主力）**：能读懂含蓄表达——「今天没写几个字，特别困」是疲惫沮丧，
   词库一个字都命不中，人一眼就看得出。
2. **规则词库（降级）**：零成本、可验证。provider 没配、调用失败、
   模型跑野时兜底。宁可判得粗，也不能没有。

## 三道闸门（照搬 diary_narrator 已验证的模式）

1. **能力闸门**：kill switch 开了 / 没配 provider → 不调，直接走规则。
2. **素材闸门**：文本太短或凑不出一句完整的话 → 不调，模型只会把空壳
   扩写成一段华丽的废话。
3. **结果闸门**：不是五态之一 / 数值越界 / 置信度太低 / 证据是编的
   → 丢弃，回落到规则版。

全程 try/except。**本模块永不抛异常**，最坏情况返回 None（不表态）。

## 「不表态」是一种正当结果

拿不准就返回 None，由调用方回落到行为推断并标注来源。硬猜一个情绪贴上去，
比承认不知道糟糕得多。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Iterable, Optional

from deskmate.constants.emotion import EMOTION_STATES, STATE_MOOD

logger = logging.getLogger(__name__)

# kill switch：DESKMATE_EMOTION_SENSE=0 一键退回纯规则（不出网、不烧 token）
_ENV_FLAG = "DESKMATE_EMOTION_SENSE"
# 环境变量取这些值 = 关掉 LLM（未设置/设成别的 = 开启）
_OFF_VALUES = {"0", "false", "off", "no", "none"}

# 素材闸门：短于这个长度的日记撑不起判断（「今天累了」四个字谁都会看走眼）
_MIN_TEXT_CHARS = 10

# 结果闸门：置信度低于此值视同「读不出来」，不表态
_MIN_CONFIDENCE = 0.35

# 单则日记进模型的最大字符数（长文截断，防 token 空烧）
_MAX_PER_POST = 1200

# 规则层：情绪「低落」的强信号词（比一般负面词更指向低能量、提不起劲）
_LOW_SPIRIT_WORDS = [
    "没劲", "提不起", "不想动", "什么也不想", "没意思", "空落落",
    "沮丧", "低落", "消沉", "无力", "心灰", "麻木", "撑不住",
]

_SYSTEM = """\
你在读一个人当天写的日记，要判断他这一天的整体情绪状态。

只做判断，不安慰、不建议、不复述、不评价文笔。
你只能从他写下的文字里读情绪——他没写的，一个字都不许替他补。
读不出来就老实说读不出来，猜一个交上去比承认不知道更糟。"""

_PROMPT = """\
下面是他 {day} 写的日记（可能不止一则）：

---
{texts}
---

请判断这一天的整体情绪，只输出一行 JSON，不要任何其它内容：
{{"state":"<状态>","mood":<0-100的整数>,"confidence":<0-1的小数>,"evidence":"<最能支撑这个判断的一句原文，不超过40字>"}}

state 只能取这五个值之一：
- 顺利：写得顺、有进展、心情好、踏实满足
- 平静：平淡，没有明显起伏
- 忙碌：事情多、赶、时间紧，但情绪上没有明显负面
- 压力大：卡住、被打回、不顺、烦躁焦虑
- 低落：沮丧、无力、难过、没劲、提不起精神

【判断时最容易犯的错】
1. **把假设当成事实。** 他写「幸好有她在，不然我心情会跌到极点」——
   真实情绪是**庆幸**，不是低落。「不然」「如果没有」「要是」后面跟的
   是**没发生的事**，不能拿来当这一天的状态。
2. **把长期习惯当成当天状态。** 「我心情郁闷时就去写小说」说的是一贯
   的排解方式，不等于他今天郁闷。
3. **把别人的事当成他的情绪。** 转述行业新闻、写书中人物的遭遇，
   不等于他本人低落——要看他对此**自己的反应**。

mood 的刻度（是这一天的整体心情，不是情绪的激烈程度）：
  90-100 特别好，有明显的高兴或满足
  75-89  不错，或平稳偏正面
  60-74  一般，有点累 / 有点赶
  45-59  不太好，有明显的压力或低落
  25-44  很差，需要被体谅

硬要求：
- evidence 必须引**陈述他真实状态**的原句：不要引假设句、引文、转述或别人的话。
- evidence 必须是原文里**真实出现过的字句**，一个字都不许改。找不到就给空串。
- 读不出情绪（比如只记了事务性的流水账），输出
  {{"state":"","mood":0,"confidence":0,"evidence":""}}
- confidence 是你对自己这个判断的把握，不是情绪的强度。"""

_JSON_RE = re.compile(r"\{[^{}]*\}")


# ----------------------------------------------------------------------
# 闸门 1：要不要调 LLM
# ----------------------------------------------------------------------
def _provider_ok() -> bool:
    """provider 是否配好。探测本身也可能失败，一律当不可用（fail-open 的反向）。"""
    try:
        from deskmate.memory.llm import memory_llm_available
    except Exception:
        return False
    try:
        return bool(memory_llm_available())
    except Exception:
        return False


def _llm_enabled() -> bool:
    """能力闸门：kill switch 关着 + provider 配好了，才允许出网。"""
    return str(os.environ.get(_ENV_FLAG, "")).strip().lower() not in _OFF_VALUES and _provider_ok()


def llm_disabled_reason() -> str:
    """为什么没走 LLM（供日志/诊断）。空串表示 LLM 可用。"""
    flag = str(os.environ.get(_ENV_FLAG, "")).strip().lower()
    if flag in _OFF_VALUES:
        return f"{_ENV_FLAG} 已关闭（kill switch）"
    return "" if _provider_ok() else "未配置 provider"


# ----------------------------------------------------------------------
# 素材准备
# ----------------------------------------------------------------------
def _join(texts: Iterable[str]) -> str:
    """把多则日记拼成给模型看的一块文本（每则截断、去空白）。"""
    out: list[str] = []
    for t in texts:
        s = " ".join(str(t or "").split())
        if s:
            out.append(s[:_MAX_PER_POST])
    return "\n\n".join(out)


def digest(texts: Iterable[str]) -> str:
    """内容指纹：日记没变就不必重问模型（tick 一天跑很多次）。

    同样的内容 → 同样的指纹 → 复用上次的结论，不重复烧 token。
    """
    return hashlib.md5(_join(texts).encode("utf-8")).hexdigest()[:12]


def _has_substance(text: str) -> bool:
    """素材闸门：够不够格拿去问模型。

    只有三五字的碎片（「今天累了」）不调——这种长度，规则词库就够了，
    叫模型来是杀鸡用牛刀，而且它容易把碎片脑补成一篇。
    """
    t = " ".join(str(text or "").split())
    return len(t) >= _MIN_TEXT_CHARS


# ----------------------------------------------------------------------
# 结果闸门
# ----------------------------------------------------------------------
def _norm_for_match(s: str) -> str:
    """去标点空白，只留中日文与字母数字，用于「证据是否原文」的比对。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(s or "")).lower()


# 反事实/假设的引导词。出现在证据**前面**时，那句话描述的不是真实发生的事。
# 真实案例（2026-09-05 的日记）：他写「我一直在庆幸一件事——幸好有尹遥……
# 如果没有尹遥，我的心情就会跌落至极点」，模型摘录了后半句当证据，
# 把「庆幸」判成了「低落，mood 20」。他明明在庆幸，小桔却要去安慰他——
# 这种错比不判断糟糕得多。
_HYPOTHETICAL_MARKS = (
    "如果", "要是", "假如", "倘若", "如若", "一旦",
    "不然", "否则", "要不然", "的话", "没有的",
)
# 往前看多少字来找假设引导词（引文常被截断，引导词在证据之外的前半句里）
_HYPOTHETICAL_LOOKBACK = 40


def _is_hypothetical(evidence: str, text: str) -> bool:
    """证据是不是一句假设（「如果没有…，我就会…」）。

    只查证据本身不够——模型常把引导词所在的前半句截掉，
    只留下「心情就会跌落至极点」这种看着像陈述的片段。
    所以要回溯它在原文里的**前置上下文**。
    """
    norm_e = _norm_for_match(evidence)
    norm_t = _norm_for_match(text)
    if not norm_e or not norm_t:
        return False
    # ① 引导词可能在证据自己身上（模型把整句引全了：
    #    「如果没有尹遥，我的心情就会跌落至极点」）
    if any(m in norm_e for m in _HYPOTHETICAL_MARKS):
        return True
    # ② 也可能被截在证据之外，只剩「心情就会跌落至极点」这种像陈述的片段
    idx = norm_t.find(norm_e)
    if idx < 0:
        return False
    window = norm_t[max(0, idx - _HYPOTHETICAL_LOOKBACK):idx]
    return any(m in window for m in _HYPOTHETICAL_MARKS)


def _validate(raw: dict, text: str) -> Optional[dict]:
    """校验模型输出。任何一项不合格 → 丢弃（返回 None）。

    evidence 单独处理：查不到不算致命（模型可能引得略有出入），
    但**编造的证据必须清零**——留着假证据比没有证据更危险。
    """
    try:
        state = str(raw.get("state", "") or "").strip()
        mood = float(raw.get("mood", 0) or 0)
        confidence = float(raw.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        return None

    if state and state not in EMOTION_STATES:
        return None
    if not state:
        # 模型自己说读不出来 —— 正当结果，直接不表态
        return None
    if not (0 <= mood <= 100):
        return None
    if not (0.0 <= confidence <= 1.0):
        return None
    if confidence < _MIN_CONFIDENCE:
        return None

    evidence = str(raw.get("evidence", "") or "").strip()
    if evidence:
        if _norm_for_match(evidence) not in _norm_for_match(text):
            logger.debug("情感感知：丢弃编造的证据 %r", evidence[:40])
            evidence = ""
        elif _is_hypothetical(evidence, text):
            # 证据是一句假设 —— 说明模型把「没发生的事」当成了他的状态。
            # 它的整段理解都建立在误读上，这次判断整体作废，宁可不表态。
            logger.debug("情感感知：证据是假设句，整次判定作废 %r", evidence[:40])
            return None

    # 心情值的合理区间。模型偶尔会给极端值（多半是把假设句当成了事实），
    # 夹一下不至于让花园瞬间开满灰紫色的花。留有余地但不许走极端。
    _MOOD_FLOOR = 25.0
    _MOOD_CEIL = 95.0

    return {
        "state": state,
        # 模型没给 mood（0）时落回该态的基准值；给了就夹进合理区间。
        # ⚠️ 顺序要紧：先判 0 再夹，否则 0 会被夹成 25，基准值永远用不上。
        "mood": round(
            STATE_MOOD[state] if mood <= 0
            else max(_MOOD_FLOOR, min(_MOOD_CEIL, mood)),
            1,
        ),
        "confidence": round(confidence, 2),
        "evidence": evidence[:80],
        "source": "llm",
    }


# ----------------------------------------------------------------------
# 规则降级
# ----------------------------------------------------------------------
def rule_sense(texts: Iterable[str]) -> Optional[dict]:
    """词库判定（零成本、可验证）。读不出信号就返回 None —— 不表态。

    复用认知层情感词库（``POSITIVE_EMOTION_WORDS`` / ``NEGATIVE_EMOTION_WORDS``），
    保证「长期画像」和「当天心情」两处口径一致。
    """
    text = _join(texts)
    if not _has_substance(text):
        return None

    try:
        from deskmate.cognitive.emotion_extract import (
            NEGATIVE_EMOTION_WORDS,
            POSITIVE_EMOTION_WORDS,
        )
    except Exception:
        return None

    pos_hits = [w for w in POSITIVE_EMOTION_WORDS if w in text]
    neg_hits = [w for w in NEGATIVE_EMOTION_WORDS if w in text]
    if not pos_hits and not neg_hits:
        return None  # 一个情绪词都没有 → 不猜

    low = [w for w in _LOW_SPIRIT_WORDS if w in text]
    if low and len(neg_hits) > len(pos_hits):
        state = "低落"
        evidence = low[0]
    elif len(neg_hits) > len(pos_hits):
        state = "压力大"
        evidence = neg_hits[0]
    elif len(pos_hits) > len(neg_hits):
        state = "顺利"
        evidence = pos_hits[0]
    else:
        return None  # 正负打平 → 不表态，交给行为推断

    return {
        "state": state,
        "mood": STATE_MOOD[state],
        # 规则判定的置信度固定偏低：它只数了词，没读句子
        "confidence": 0.5,
        "evidence": evidence,
        "source": "rule",
    }


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def llm_sense(texts: Iterable[str]) -> Optional[dict]:
    """LLM 判定。三道闸门任一不过就返回 None，永不抛异常。"""
    try:
        text = _join(texts)
        if not _has_substance(text):          # 闸门 2：素材不够
            return None
        if not _llm_enabled():                # 闸门 1：能力不够
            return None

        from deskmate.memory.llm import chat_memory

        day = ""
        try:
            import datetime

            day = datetime.date.today().isoformat()
        except Exception:
            pass
        out = chat_memory(
            _SYSTEM,
            _PROMPT.format(day=day or "这一天", texts=text),
            temperature=0.2,
            max_tokens=200,
            timeout=20.0,
        )
        m = _JSON_RE.search(str(out or ""))
        if not m:
            logger.debug("情感感知：模型没返回 JSON")
            return None
        try:
            raw = json.loads(m.group(0))
        except (ValueError, TypeError):
            logger.debug("情感感知：JSON 解析失败 %r", m.group(0)[:80])
            return None
        if not isinstance(raw, dict):
            return None
        return _validate(raw, text)           # 闸门 3：结果异常丢弃
    except Exception as e:
        logger.warning("情感感知 LLM 失败，回落规则: %s", e)
        return None


def sense(texts: Iterable[str]) -> Optional[dict]:
    """读一天的心情。LLM 优先，规则兜底，都不行就 None（不表态）。

    **本函数永不抛异常**：调用方在 proactive tick 的主链路上，
    一次情感感知失败绝不该把整条 tick 拖垮。llm_sense 内部已经吞了异常，
    这里再包一层是双保险——契约是本模块对外承诺的，不能只靠内层自觉。

    Returns:
        {"state", "mood", "confidence", "evidence", "source"} 或 None
    """
    texts = list(texts or [])
    if not texts:
        return None
    try:
        got = llm_sense(texts)
        if got:
            return got
    except Exception as e:
        logger.warning("情感感知 LLM 链路异常（已回落规则）: %s", e)
    try:
        return rule_sense(texts)
    except Exception as e:
        logger.warning("情感感知规则层异常（放弃本次判定）: %s", e)
        return None
