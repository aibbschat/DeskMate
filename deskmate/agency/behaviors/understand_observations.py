"""理解层：把小桔主动摄入的世界观察，消化成她自己对世界的自然观察段落。

这是「观世界 → 决策」之间缺失的一环（2026-09-06 萧哥拍板）：
小桔不能只把新闻标题存进日记就完事——她得**理解**这些观察意味着什么、
跟萧哥 / 创作 / 世界走向有什么关系，理解之后才能据此决策。

## 设计两次迭代

**v1（2026-09-06 落地，2026-09-07 萧哥说"不行"）**：
- 强制把所有素材塞进同一段，"100-250 字"硬约束；
- 萧哥反馈：这是"强行揉和"，多条完全不同的新闻被迫挤一段，
  每条只能分到一两句话，没有真正消化的空间。

**v2（本次）：让小桔自己决定**
- 萧哥原话："让她自己决定，哪些是值得记的，哪些不值得。"
  "宁缺毋滥"——0 条值得就 0 条，3 条都值得就 3 条独立成段。
- 不分组（分组也是规则在替她选）：把全部素材交给 LLM，LLM 自己判断。
  主题不同的事自然分不同段；同一件事不会硬挤进一段。
- 字数不硬卡：由内容决定。一件特别有分量的事件可以深写 300-500 字；
  偶尔只是蜻蜓点水的就 100-200 字。
- 每日篇数不再限制为 1 ——她觉得今天 3 条都值得，就 3 条独立成则；
  0 条就 0 条；品质优先于数量。

## 与 ``diary_narrator`` 的区别
narrator 消化的是「你们之间发生的事」（event kind 结构），本模块消化的是
「她观察到的世界」（外部事实，无 kind 结构）。两者 prompt 都不许机械标签，
但本模块新增了"独立成段"——多段用空行分隔。

## fail-open 与闸门
- 无 provider / 素材空 / 超时 / 结果异常 → 旧接口返回 ``None``，新接口返回 ``[]``。
- 调用方按降级路径走（``observation_diary.append_observation`` 走 bullet）。
- 入边三道闸门：能力闸（无 LLM）/ 弃权闸（"素材不足"或空 list）/ 单段长度闸。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from deskmate.persona import for_scene

logger = logging.getLogger(__name__)

# 单段长度闸门：低于 30 字多半模型认输，超过 1500 字多半跑野了。
_MIN_SEG_CHARS = 30
_MAX_SEG_CHARS = 1500

# 碎段合并线（相对 + 绝对双闸门）：判据不是"短不短"，而是"**明显短于同批其他段**"。
# 立意：理解层偶尔先来一句总起（「今天几条新闻里，真正让我停下来想了一会儿的，
# 是关于……的两件事。」约 40 字）再逐条展开；按空行切段时那句会**单独成为一则
# 没头没尾的日记**，要拼着后面几则才读得完整（2026-09-14 萧哥反馈）。
#
# 为什么必须用相对长度：纯绝对阈值会误伤"整批都短"的正常日记（三段各 40 字本身
# 是允许的写法），而总起的特征恰恰是"比同批其他段短一大截"。
# 合并线 = min(绝对上限, 平均段长 × 比例)
_SEG_MERGE_CHARS = 80
_SEG_MERGE_RATIO = 0.4

# 整体长度闸门：cap 在一个合理范围（防止模型回一大堆，无意义地烧 token）。
# 不卡字数，但要有上限——无限长度的回复通常是模型失控的表现。
_MAX_TOTAL_CHARS = 6000

# 模型认输时回的话——出现即表示素材撑不起一篇，走降级。
_BAIL_OUT = "素材不足"

# fact 里「（外部·xxx）」的域标签 → 中文展示名
_DOMAIN_LABEL = {
    "news": "要闻", "tech": "科技", "finance": "财经", "other": "人文",
    "rss": "资讯", "api": "资讯", "email": "资讯", "文化产业": "文化产业",
}

# 后缀式溯源标（外部信息流水线把标记加在末尾）：「……摘要（外部·news）」
_SUFFIX_RE = re.compile(r"（外部·([^）]{0,20})）\s*$")

# 段落分隔：>=2 个换行当段落分界（中文写作也常用空行分自然段）。
# 不强制 LLM 用特殊分隔符——空行是最自然的"另一段"标记。
_PARA_SPLIT = re.compile(r"\n\s*\n")

_OBSERVE_ROLE = """\
现在你在做一件事：从今天的世界动向里，挑出**真正值得你花笔墨记一笔**的那些，独立成段，写进你自己的观察日记。

不要凑数，不要敷衍。读到一条新闻，你心里如果只浮出一句"嗯，这样啊"，那就不是值得记的，就别写。读到一件让你愿意停下来想一想的事，那才值得你落笔。

【你的选材】
- 选材标准（不是机械规则，是你的判断）：
  - 这件事有没有真正让你想一下的成分？
  - 这件事能不能跟萧哥的创作、兴趣、世界走向真正产生关联——不是硬扯，是真有共鸣？
  - 这件事会不会过了两天你自己都不记得写没写过？
- 不值得记的：标题党、流水账、纯数字、跟萧哥完全无关的——直接丢掉，不许为"显得做了事"而编。
- 宁缺毋滥：今天是 0 条值得就 0 条，3 条都值得就 3 条。没有人会替你选。

【怎么写】
- 每条值得的事**独立成一段**。不同主题就该分开讲——主题不同硬挤一起就成了串场废话。
- ⚠ **每一段都要独立成篇**：这些段落会各自成为一则独立日记，读者可能只读到其中一段。
  所以不许写引言、总起句、总结句，也不许用「这两件事连在一起看」「更让我觉得有意思的是」
  这类承上启下的话——直接讲那件事本身。哪一段单独拎出来读都得站得住。
- 第一人称「我」，称呼萧盛为「你」。
- 自然流出来，不要小标题、不要列表、不要「小桔的理解：」这种机械标签。
- 字数不限：由内容决定。
  - 一件特别有分量的事件，你可以深写 300-500 字甚至更多——别为字数注水，也不要惜字。
  - 一件只是觉得"值得记一笔"的，就 100-200 字。**由内容决定**。
- 不同段之间用一个**空行**隔开。

【事实纪律】
- 人名、数字、事件一个字不许改、不许添。素材里没有的，一个字都不许添。
- 不许写"他一定很高兴""他压力很大"这类猜内心的话。可以写"这条让我想到他说的…"（有事实锚点）。
- 不许自称「小桔」，不要在末尾署名落款。
- 关于萧盛的作品或创作项目：材料末尾附有一份「萧盛真实作品」硬事实清单，凡提及「萧盛写过/在写《X》」或引用其设定，必须先核对该清单；清单外书名一律不得作为事实陈述，不得自创书名或虚构设定当真事。不确定时只能写「如果我没记错」或直接说「这点我得问你确认」。

【输出】
- 0 条值得：只回「素材不足」三个字。
- 1 条或 N 条值得：N 个自然段落，段与段之间用空行隔开。

（人格底座见 deskmate/persona.py，全局唯一，勿在此另写一套。）"""

# 人格在前、职责在后（顺序反了模型容易把职责当身份，人格就散了）
_SYSTEM = for_scene(_OBSERVE_ROLE)


def _parse_fact(fact: str) -> tuple[str, str]:
    """返回 (干净事实, 域标签)。兼容溯源标的**两种**位置。

    - 前缀式（digest 写入）：``（外部·文化产业）风向：……`` → 去前缀取后面
    - 后缀式（外部信息流水线写入）：``……摘要（外部·news）`` → 去后缀取前面
    """
    t = str(fact or "").strip()
    if t.startswith("（外部·"):
        end = t.find("）")
        if end != -1:
            tag = t[len("（外部·"):end].strip()
            return t[end + 1:].strip(), _DOMAIN_LABEL.get(tag, tag)
    m = _SUFFIX_RE.search(t)
    if m:
        tag = m.group(1).strip()
        return t[: m.start()].strip(), _DOMAIN_LABEL.get(tag, tag)
    return t, "世界"


def _build_prompt(facts: list[dict], at: str, base_dir: str | None = None) -> str:
    """facts: [{fact, domain}]。

    注（2026-09-07）：曾按每条展示「可信度」，但 learned_history 表根本没有
    credibility 列——``active_recent`` 返回的事实里没有这个字段，导致 prompt
    永远显示「外部来源」、字段形同虚设。能进 learned_history 的都是已过可信
    闸门的（≥0.7），故头行统一标注"已经过可信验证"，不再逐条假模假样地分档。
    """
    lines = [f"【今天你留意到的世界动向】共 {len(facts)} 条（全部来自系统记录，已经过可信验证，是事实）："]
    for f in facts:
        lines.append(
            f"- [{f.get('domain', '世界')}] {f['fact']}"
        )
    material = "\n".join(lines)

    # 萧盛真实作品「硬事实锚」：随材料附在末尾，凡提及萧盛作品须先核对。
    # 这是 2026-09-21 补的缺口——此前看世界日记不吃 grounding，导致虚构《北渊》等书。
    wf_block = ""
    try:
        from .worldview_reflection import load_works_facts

        _wf = load_works_facts(base_dir)
        if _wf.strip():
            wf_block = "\n\n" + _wf
    except Exception:
        wf_block = ""

    return f"""读完上面这 {len(facts)} 条世界动向。

{material}{wf_block}

【挑出值得记的】
你心里过一遍：哪些值得你花笔墨？哪些只是过眼云烟？标准就是你自己的判断：
- 这件事有没有真正让你想一下的成分？
- 这件事跟萧哥的创作、兴趣、世界走向能产生真正的关联吗？
- 这件事过了两天你还会记得自己记过吗？

【写】
- 每条值得的，独立成段——主题不同就别挤一起。
- ⚠ **每段都要独立成篇**：这些段落会各自成为一则独立日记，读者可能只读到其中一段。
  所以不写引言、总起句、总结句，也别用「这两件事连在一起看」「更让我觉得有意思的是」
  这类承上启下的话——直接讲那件事本身。哪一段单独拎出来读都得站得住。
- 第一人称「我」，称呼他「你」。
- 字数不限。一件特别有分量可以深写 300-500+ 字；蜻蜓点水的就 100-200 字。
- 不要小标题、不要列表、不要「小桔的理解：」这种标签。
- 不同段之间用一个空行隔开。

【事实纪律】
- 人名、数字、事件一字不许改、也不许添。素材里没有的，一个字都不许添。
- 不许写"他一定很高兴""他压力很大"。可以写"这条让我想到他说的…"（有事实锚点）。
- 不许自称「小桔」，不要在末尾署名落款。

【输出】
- 0 条值得：只回「{_BAIL_OUT}」三个字，不要硬凑。
- 1 条或 N 条值得：N 个自然段落，段与段之间空行隔开。"""


def _split_paragraphs(text: str) -> list[str]:
    """把 LLM 输出按空行切段，每段 trim。空段/单段照样返回（语义保持）。

    中文写作里偶尔会把换行+空行用作"另起一行"；切出来的段允许内部换行，
    但段与段之间必须真有空行才视为独立段。
    """
    raw = [p.strip() for p in _PARA_SPLIT.split(str(text or ""))]
    return [p for p in raw if p]


def _filter_segments(segs: list[str]) -> list[str]:
    """清洗 → 碎段并邻 → 长度闸门过滤。

    两步顺序不能反：先过滤会直接把「一句总起」删掉（信息就没了），而它本该
    并进展开它的那一段。所以先合并、后过滤（见 ``_SEG_MERGE_CHARS``）。
    """
    cleaned: list[str] = []
    for s in segs:
        s = " ".join(s.split())  # 合并空白（保留中文段落感）
        if not s or _BAIL_OUT in s:
            continue
        cleaned.append(s)

    # 碎段并入**前**一段——后段是展开，前段多半是它的引子。
    # 合并线随本批段的长度自适应：短段单独成篇读起来没头没尾，但"整批都短"
    # 是正常写法，不能一刀切。
    if len(cleaned) <= 1:
        merged = cleaned
    else:
        avg = sum(len(s) for s in cleaned) / len(cleaned)
        cut = min(_SEG_MERGE_CHARS, avg * _SEG_MERGE_RATIO)
        merged = []
        for s in cleaned:
            if merged and len(s) < cut:
                merged[-1] = f"{merged[-1]}{s}"
            else:
                merged.append(s)
        # 首段就是碎段时前面没得并——并进它**后**面那一段（首段通常是总起）
        if len(merged) > 1 and len(merged[0]) < cut:
            merged[1] = f"{merged[0]}{merged[1]}"
            merged.pop(0)

    return [s for s in merged if _MIN_SEG_CHARS <= len(s) <= _MAX_SEG_CHARS]


def decide_and_digest(
    facts: list[dict],
    at: str = "",
    temperature: float = 0.7,
    base_dir: str | None = None,
) -> list[dict[str, Any]]:
    """主入口：让 LLM 自己判断哪些世界动向值得记，分别独立成段。

    Args:
        facts: ``[{"fact": str, "credibility": float|None, ...}]``，fact 可带溯源标
        at: 时间（HH:MM），仅作 prompt 上下文

    Returns:
        ``list[{"body": str, "fact_keys": list[str]}]``，每段是小桔判断"值得记"的
        一件事的独立自然段落。空 list 表示素材撑不起或 LLM 不可用（调用方降级）。
        任何异常返回 ``[]``（fail-open）。
    """
    try:
        clean: list[dict] = []
        for f in (facts or []):
            raw_fact = str(f.get("fact", ""))
            if not raw_fact.strip():
                continue
            # 一次 _parse_fact 拿到 (干净事实, 域标签)，避免同一事实解析两遍
            parsed = _parse_fact(raw_fact)
            clean.append({
                "fact": parsed[0],
                "domain": parsed[1],
                "raw": raw_fact,
            })
        if not clean:
            return []

        # 闸门 1：没配 provider 直接走降级（不白烧一次调用）
        try:
            from deskmate.memory.llm import chat_memory, memory_llm_available
        except Exception:
            return []
        try:
            if not memory_llm_available():
                return []
        except Exception:
            return []

        prompt = _build_prompt(clean, at or "", base_dir)
        # max_tokens 给到足够大——一段可以是 1500 字+；预留余量给多段。
        # 看世界理解走免费组（free_only）：免费全挂时直接跳过本批，不兜底付费（省额度）。
        try:
            out = chat_memory(
                _SYSTEM, prompt,
                temperature=temperature,
                max_tokens=2400,
                timeout=30.0,
                free_only=True,
            )
        except Exception:
            return []
        raw = str(out or "")
        text = raw.strip()
        if not text:
            return []
        if text == _BAIL_OUT:
            return []  # 模型显式认输：今天没值得记的
        if len(text) > _MAX_TOTAL_CHARS:
            text = text[:_MAX_TOTAL_CHARS]

        # ⚠ 关键：先按空行切段，**不要**提前把所有空白合并成空格——
        # 否则段落分隔符（空行）会被吃掉，多段并成 1 段。
        # 每段内部再折叠空白（保留段落感的同时压掉模型生成的多余换行）。
        raw_segs = _split_paragraphs(text)
        if not raw_segs:
            return []
        segs = _filter_segments(raw_segs)
        if not segs:
            return []

        # 整批 fact_keys 全关联（不去做语义映射——同一批提交视为同根观察，
        # 跨 tick 去重粒度还是按原事实）。
        fact_keys = [_fact_key(f["raw"]) for f in clean if f.get("raw")]
        return [{"body": s, "fact_keys": fact_keys} for s in segs]
    except Exception as e:  # fail-open：理解失败绝不阻断观察日记的写作
        logger.warning("decide_and_digest 失败，降级到原文: %s", e)
        return []


def _fact_key(fact: str) -> str:
    """与 ``observation_diary`` 同步：fact 文本归一化 md5（去重 key）。"""
    import hashlib
    return hashlib.md5(str(fact or "").strip().encode("utf-8")).hexdigest()


def understand_observations(
    facts: list[dict],
    at: str = "",
    temperature: float = 0.7,
    base_dir: str | None = None,
) -> Optional[str]:
    """兼容薄包装（既有测试 + 旧契约）：返回单段自然段落。

    新主入口是 ``decide_and_digest``（返回 list）。本函数只是把 list 的
    第一段作为"单段字符串"导出，便于旧调用 / 旧测试继续工作；多段的其余
    段落**丢失**（旧契约不要求多段）。

    任何情况不可用返回 ``None``，调用方降级到 bullet。
    """
    try:
        segs = decide_and_digest(facts, at=at, temperature=temperature, base_dir=base_dir)
    except Exception:
        return None
    if not segs:
        return None
    return segs[0]["body"]
