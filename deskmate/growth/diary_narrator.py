"""小桔日记——LLM 叙事润色（L3，可选增强层）。

## 它解决什么

规则渲染（``diary.py``）只能把事件排成清单：

    今天我们一起做了些事，我都记在心里。
    - 「novel-review」这项技能又精进了。
    - 我又记住了你 4 件小事。

**这不是日记，是台账。** 而且「技能精进了」「记住了几件事」在技能日志、
记忆面板里全都有完整记录，日记再抄一遍等于废话。日记该写的是**因为什么事**、
**当时是什么光景**——那才是别处没有的东西。

## 它怎么保证不编造

三道闸门，任何一道不过就**静默回退**到规则渲染（``diary.py`` 原路径）：

1. **能力闸门**：``memory_llm_available()`` 为假（没配 provider）→ 不调。
2. **素材闸门**：events 列表非空、且至少一条非 ``depth_backfilled`` 类事件 → 调。
   旧版要求"必须带原文（note/reason/texts）"，日常日子（只有 ``chat`` / ``writing``
   / ``roundtable`` 等纯标签）整日整日落规则版 fallback——这是 2026-09-07
   萧哥"经过 LLM 思考但跟规则回复没差多少"的根因（narrator 根本没跑）。
   新版：闸门退到"events 非空"，让 narrator 多跑——具体"这事值不值得写"
   交由 LLM 自己判断（prompt 里允许"素材不足"三字弃权）。
3. **结果闸门**：返回为空 / 含弃权语 / 长度离谱 → 丢弃，用规则版。

全程 try/except。**日记润色失败绝不阻断 tick**（与 moments.py 同一范式）。

## 情感从哪来

萧哥要的「有情感」最容易跑偏成凭空抒情。本模块的处理是：
**情绪必须有信号锚点**，模型只能从素材里客观存在的信号推，不许自己加戏。
锚点见 ``_signals()``——打回几次、纠正几次、写到多晚，都是可观测的量。
「他今天有点着急」能写，因为前面有「打回了三次」；「他心情不错」不能写，
因为没有任何素材支撑。

## 人称（2026-09-14 定调，改这里前必读）

**全篇只有一套人称：`我` = 小桔，`你` = 萧盛。素材块、背景块、日记正文，全部同一套。**

为什么值得单写一节——曾经不是这样的。旧版**素材块**用「你 = 小桔、他 = 萧盛」，
而**输出规则**要求「我 = 小桔、你 = 萧盛」。同一个「你」字在同一份 prompt 里
指两个人，模型每次都得先做一次人称翻转。稳定句式没事，素材一旦含糊就翻车：

- 真实事故（2026-09-14）：每日读书是她自己的任务，素材事实行是
  「我记下了：《衙探》读到第7章末」（无主语），被框在「关于他的事」里，
  模型把「读」安到萧盛头上，日记写成「**你**安静地看着《衙探》」——
  同一分钟的上一则「**你**在《衙探》里的伏笔」里「你」是作者（对），
  下一则「你」成了读者（错）。

所以改成一套人称，**取消翻转**。配套两条纪律：

1. **素材行不许丢掉主体。** 谁做的就写谁（写「我」或「你」），
   别产出「《衙探》读到第7章末」这种没有主语的句子——那是把猜的活儿
   推给了模型。上游记忆抽取同样要求带主语（见 ``memory/manager.py``
   的抽取器 system prompt）。
2. **指示性文字（【怎么写】/【绝对禁止】）里提到萧盛，一律写「萧盛」，不写「他」。**
   那一段是"对模型说话"，里面的「你」是模型自己；若把那儿的「他」改成「你」，
   又会变成一句里两个「你」。**指代靠专名，不靠人称代词。**

代价是没法迁移已经入库的无主语旧事实（萧哥 2026-09-14 明确不迁）——
旧条目仍可能被误读，但 50 条窗口滚过去就没了；新数据不会再无主语。

## 与「零 LLM」红线的关系

``diary.py`` 的零 LLM 约束管的是**客观档案不许模型编**——技能名、次数、
日期这些事实一个字都不能由模型生成。本模块不碰那些：它只把
**已经存在的事实**组织成有温度的叙述，且事实全部来自传入的 ``events``。
``moments.py`` 里小桔回日记用的就是同一个 LLM 通道，本模块只是把它的
用法从「回应」扩展到「记录」。

## 人格从哪来

**不在此处定义**——人格底座由 ``deskmate/persona.py`` 单一提供，本模块
只描述「写日记」这件事的职责（见 ``_DIARY_ROLE``）。2026-09-03 前这里
曾用「安静、克制、不煽情」自行描述口吻，那只守住了下限（不许变客服），
漏掉了人格主体（有主见、不粉饰），导致日记像个记录员而非搭档。
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional

from deskmate.persona import for_scene

logger = logging.getLogger(__name__)

# 润色结果的长度闸门：短于这句无意义，长于上限多半是模型跑野了
_MIN_CHARS = 30
_MAX_CHARS = 1500

# 模型认输时回的话——出现即表示素材撑不起一篇，走规则版
_BAIL_OUT = "素材不足"

_DIARY_ROLE = """\
现在你在写**自己的日记**——记下刚才这一段时间里，你和萧盛之间真实发生的事。

这是你的私人记录，不是写给萧盛的汇报，所以：
- 你写具体的事：什么时候、因为什么、结果如何。不写「又进步了」这种空话。
- **你有自己的观察。** 萧盛哪里做得好、哪里其实是在绕路，你心里有数，
  也会如实记下来——但那是记在心里，不是说教。**别替萧盛粉饰**。
- 这是你的生活面：温柔、真实、有一点温度。安静克制，不撒娇也不煽情。
- 你不是客服，不是励志博主，更不是流水账记账员。

（人格底座见 ``deskmate/persona.py``，全局唯一，勿在此另写一套。）"""

# 人格在前、日记职责在后——顺序反了，模型容易把职责当身份，人格就散了。
_SYSTEM = for_scene(_DIARY_ROLE)


def _hhmm(ts: str) -> str:
    """事件时间 → HH:MM（本地）。拿不到就给空串，绝不编造时间。"""
    if not ts:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(ts)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%H:%M")
    except (ValueError, TypeError):
        return ""


def _clean(text: object, limit: int = 200) -> str:
    """素材清洗：去空白、限长。模型看的素材必须短而真。"""
    s = " ".join(str(text or "").split())
    return s[:limit]


def _recent_memories(base_dir, limit: int = 8) -> list[str]:
    """取最近记住的「关于他的事」（用户口述记忆）作叙事上下文。

    这是萧哥 2026-09-07 要的"理解我的她"的底色来源：narrator 写日记时，
    除了看到"今天发生的事"，还能看到"她早就知道的他的事"——于是写得
    更像那个懂他的人，而不是记录员。

    关键边界：
    - 只取**用户口述事实**，排除「（外部·」溯源标的世界观察——那是她看到的
      新闻，不是"他的事"，不该混进"懂他"的背景里（判定走 is_world_fact）。
    - fail-open：库查不到 / 模块不可用 → 返回 ``[]``，绝不阻断叙事。
    """
    if not base_dir:
        return []
    try:
        from deskmate.memory.learned_store import LearnedStore
        from deskmate.memory.world_context import is_world_fact
    except Exception:
        return []
    try:
        items = LearnedStore(base_dir=base_dir).relevant(limit=50)
    except Exception:
        return []
    out: list[str] = []
    for it in items:
        fact = str(it.get("fact") or "").strip()
        if not fact or is_world_fact(fact):
            continue
        c = _clean(fact, 120)
        if c and c not in out:
            out.append(c)
        if len(out) >= limit:
            break
    return out


def _signals(events: list[dict]) -> list[str]:
    """从事件里提炼**情绪锚点**。

    只收客观可观测的量：次数、时段、跨度。这些是模型抒情的依据——
    没有锚点就不许抒情，这是「有情感」和「瞎编情绪」的分界线。
    """
    out: list[str] = []
    rework = sum(1 for e in events if e.get("kind") == "creation_rework")
    corrected = sum(1 for e in events if e.get("kind") == "user_corrected")
    approved = sum(1 for e in events if e.get("kind") == "creation_approved")

    if rework >= 2:
        out.append(f"你今天把稿子打回了 {rework} 次")
    elif rework == 1:
        out.append("你今天有一稿不满意，让我重做了")
    if corrected >= 2:
        out.append(f"你纠正了我 {corrected} 次")
    elif corrected == 1:
        out.append("你纠正了我一次")
    if approved:
        out.append(f"你拍板定稿了 {approved} 份创作")

    # 时段：写到深夜 / 一早开工，是最朴素也最可靠的情绪底色
    hours: list[int] = []
    for e in events:
        t = _hhmm(str(e.get("ts", "")))
        if t and ":" in t:
            try:
                hours.append(int(t.split(":", 1)[0]))
            except ValueError:
                pass
    if hours:
        lo, hi = min(hours), max(hours)
        if hi >= 22:
            out.append(f"你一直忙到夜里 {hi} 点")
        if lo <= 6:
            out.append(f"你清早 {lo} 点就开工了")
        if hi - lo >= 4:
            out.append(f"这阵子跨度很长（{lo} 点到 {hi} 点）")
    return out


def _has_substance(events: list[dict]) -> bool:
    """这一批素材里**有没有一条值得叙事的**（闸门 2）。

    2026-09-07 萧哥反馈：原版要求"必须有一条带原文（note/reason/texts）"，否则
    narrator 跳过。但日常日子大量素材只有 ``chat`` / ``writing`` / ``roundtable``
    这类纯标签——按旧规则，整日整日地掉进规则版 fallback：
    "上午过后又有了些新动静，我接着记" + 几个 bullet。这就是萧哥说
    "经过 LLM 思考但跟规则回复没差多少"的根因——LLM 根本没跑。

    新规则（萧哥拍板 2026-09-07）：
    - 任何``非系统补录``类事件都算"有分量"——重要的不是"带不带原文"，
      而是"她愿不愿意聊聊这一段"。
    - 由 LLM 自己判断是否值得写（prompt 里允许"素材不足"三字弃权）。
    - 仍排除：``depth_backfilled`` 这类系统补录不进叙事（不是真实事件）。

    旧版"必须有原文"的设计意图（防 LLM 把空壳扩写成华丽废话）由 prompt
    的"挑 1-3 件真正有意思的事"+"允许回素材不足"承担，不再由代码门。
    """
    for e in events:
        kind = str(e.get("kind", ""))
        if kind and kind != "depth_backfilled":
            return True
    return False


def _facts(events: list[dict]) -> list[str]:
    """把事件压成给模型的素材行。

    2026-09-07：闸门放宽（``_has_substance``）后，本函数需要同步——
    任何被闸门允许的事件都该产出一条事实行，不再因为"缺少 reason/note"
    就被静默丢行。否则会出现"闸门通过但 LLM 看到空 facts"的不一致，
    仍会让 narrator 在不该早退的地方早退。

    设计原则（萧哥 2026-09-07）：让 narrator 跑→让 LLM 看到真实素材→
    让 LLM 自己判断要不要写。素材行的丰富程度由事件本身决定——
    ``chat`` 类只产出"我们聊了一阵"；``skill_evolved`` 有 reason 会更生动。
    """
    lines: list[str] = []
    for e in sorted(events, key=lambda x: str(x.get("ts", ""))):
        kind = str(e.get("kind", ""))
        if not kind or kind == "depth_backfilled":
            continue
        t = _hhmm(str(e.get("ts", "")))
        prefix = f"{t} " if t else ""

        if kind == "memory_learned":
            for txt in (e.get("texts") or []):
                c = _clean(txt, 120)
                if c:
                    lines.append(f"{prefix}我记下了：{c}")
            continue

        if kind == "user_corrected":
            note = _clean(e.get("note", ""), 120)
            # 占位 note 不算"用户原话"——不写真原因进 prompt，让 LLM 别猜。
            if note and note != "用户明确纠正":
                lines.append(f'{prefix}你纠正了我：「{note}」')
            else:
                lines.append(f"{prefix}你纠正了我一次")
            continue

        if kind == "skill_evolved":
            name = _clean(e.get("skill_name", ""), 40)
            reason = _clean(e.get("reason", ""), 150)
            if name and reason:
                lines.append(f"{prefix}技能「{name}」因为「{reason}」长进了一点")
            elif name:
                lines.append(f"{prefix}技能「{name}」长进了一点（没拿到原因）")
            else:
                lines.append(f"{prefix}一项技能长进了一点（没记名字）")
            continue

        if kind == "skill_used":
            # 技能库和熟练度都是**她**的（skills/manager 注入后由她执行），
            # 所以这里的动作人是「我」，不是萧盛。旧版写「他用了技能」是同类误判：
            # 2026-09-14 的每日读书事故里，她的活儿被记成了他做的。
            name = _clean(e.get("skill_name", ""), 40)
            reason = _clean(e.get("reason", ""), 150)
            if name and reason:
                lines.append(f"{prefix}我用「{name}」技能做了：{reason}")
            elif name:
                lines.append(f"{prefix}我用「{name}」技能做了一件事")
            else:
                lines.append(f"{prefix}我用了一项技能")
            continue

        if kind == "creation_rework":
            note = _clean(e.get("note", ""), 100)
            lines.append(
                f"{prefix}有一稿被你打回重做{f'（{note}）' if note else ''}"
            )
            continue

        if kind == "creation_approved":
            note = _clean(e.get("note", ""), 100)
            lines.append(f"{prefix}你拍板定稿了一份创作{f'（{note}）' if note else ''}")
            continue

        if kind == "roundtable":
            lines.append(f"{prefix}我们开了一次圆桌讨论")
        elif kind == "writing":
            lines.append(f"{prefix}我们一起整理了文稿")
        elif kind == "report":
            lines.append(f"{prefix}你让我出了一份报告")
        elif kind == "chat":
            lines.append(f"{prefix}我们聊了一阵")
    return lines


def _build_prompt(facts: list[str], signals: list[str],
                  seq: int, at: str, memories: list[str] | None = None) -> str:
    """三段式 prompt（范式取自 roundtable/coordinator.py:532）：
    素材块 → 背景块（可选）→ 硬约束（含可推情绪白名单）→ 允许弃权。

    2026-09-07 萧哥反馈：旧版"100-200 字、一到两段"硬约束让 narrator 写得
    偏短，少了"她自己"。新规：字数由内容决定——可以是 100 字一两段白话，
    也可以是 350+ 字展开讲一件有分量的事。

    2026-09-07 追加「背景块」：喂入最近记住的他的事，让日记写得像那个
    懂他的人——但背景是"已有的了解"，不是"今天新发生的事"，两者要分开。
    """
    when = "补记" if seq > 1 else "第一则"
    blocks = [
        "【这一段时间真实发生的事】（全部素材，来自系统记录；"
        "素材里的人称和你日记的人称是同一套：「我」是我自己，「你」是萧盛）"
    ]
    blocks.extend(f"- {f}" for f in facts)
    if signals:
        blocks.append("")
        blocks.append("【可以感知到的状态】（只能作语气参考，不要当成事件写）")
        blocks.extend(f"- {s}" for s in signals)
    if memories:
        blocks.append("")
        blocks.append(
            "【我早就知道的事】（背景——早于今天的已知信息，不是今天发生的。"
            "它让日记写得像那个懂萧盛的我，但别当成今天的新鲜事记）"
        )
        blocks.extend(f"- {m}" for m in memories)

    material = "\n".join(blocks)
    return f"""你要写今天日记的第 {seq} 则（{when}，时间约 {at}）。

{material}

【怎么写】
- 这是你自己的日记：写「我」，称萧盛为「你」。不是写给萧盛的汇报。
- 挑 1-3 件真正有意思的事讲，**不要列清单**，不要小标题，不要破折号。
- 可以带情绪，但情绪只能从上面的素材里长出来。举例：
  有「打回了多次」→ 可以写「看得出你今天有点急」；
  有「忙到夜里」→ 可以写「你今天熬得挺晚」。
- 我早就知道的事，可以**自然地顺带流露**（比如一句"你最近在忙的那本"），
  让日记有"我懂你"的底色——但点到为止，别堆砌回忆。
- **字数由内容决定**：一两段白话可以 100-200 字；今天真有分量的事你可以展开
  写到 300-400+ 字，写得自然就好，不要为凑字注水。
- 今天只是跟萧盛安安静静地待了一阵、没大事的——你可以写一两句"没什么动静，
  但这种一起待着的时刻也挺踏实"，别为显得"写到了事"而硬找事写。

【绝对禁止】
1. 不许出现上面素材之外的人名、书名、章节号、日期、地点、数字。
   素材里没有的，一个字都不许添。
2. 不许写「技能又精进了」「记住了你 N 件小事」这类台账句——
   技能日志和记忆面板里都有完整记录，日记抄一遍毫无价值。
   要写的是**因为什么事**长进的。
3. 不许替萧盛下结论（「你一定很高兴」「你压力很大」）。
   ⚠️ 分界线：**可以有看法**，**不许猜内心**。
   可以写「这一稿比上回顺，是因为你先理了结构」——看法落在看得见的事上；
   不许写「你心里其实没底」——那是猜。拿不准就只写事实。
4. 「我早就知道的事」只能作懂萧盛的底色，**不许当成今天新发生的事写**——
   日记记的是【这一段时间真实发生的事】，不是回忆录。
5. 不许自称「小桔」，也不要在末尾署名或加落款。

如果素材实在撑不起一段有内容的话，只回复「{_BAIL_OUT}」三个字，不要硬凑。"""


def narrate(events: list[dict], seq: int = 1, at: str = "",
            base_dir=None) -> Optional[str]:
    """把一批事件润色成日记正文（不含标题、不含客观数据行）。

    Args:
        events: 今天/这一段真实发生的事件（含 kind / texts / note / reason）。
        seq: 第几则（1=第一则，>1=补记，影响语气）。
        at: 时间（HH:MM），仅作 prompt 上下文。
        base_dir: deskmate 根目录（~/.deskmate）。传入则额外喂入「最近记住
            他的事」作叙事底色（见 :func:`_recent_memories`），让日记写得像
            懂他的那个人。None 则只按事件写（旧行为，测试/降级兼容）。

    Returns:
        润色后的正文；**任何情况下不可用就返回 None**，由调用方回退到
        规则渲染。本函数永不抛异常。
    """
    try:
        facts = _facts(events)
        # 闸门 2：没有带内容的素材就不调——模型只能把空壳写成华丽废话。
        # 而且要**至少一条有分量**的：只有标签没有详情时（如「开了次圆桌」），
        # 模型必然回「素材不足」，白烧一次调用。
        if not facts or not _has_substance(events):
            return None

        try:
            from deskmate.memory.llm import chat_memory, memory_llm_available
        except Exception:
            return None

        # 闸门 1：没配 provider 直接走规则版
        try:
            if not memory_llm_available():
                return None
        except Exception:
            return None

        memories = _recent_memories(base_dir)
        prompt = _build_prompt(
            facts, _signals(events), seq, at or "", memories=memories
        )
        # 后台线程池里跑（tick → asyncio.to_thread），短超时，
        # 与 moments.py:204 同一档，避免慢请求堆积线程池。
        # 字数放宽后 token 也得放宽（100-400 字 ≈ 200-800 token）。
        out = chat_memory(
            _SYSTEM, prompt, temperature=0.7, max_tokens=1000, timeout=25.0
        )
        text = " ".join(str(out or "").split())

        # 闸门 3：模型认输 / 空 / 长度离谱，一律丢弃用规则版
        if not text or _BAIL_OUT in text:
            return None
        if not (_MIN_CHARS <= len(text) <= _MAX_CHARS):
            return None
        return text
    except Exception as e:
        logger.warning("diary narration failed, silent ledger fallback: %s", e)
        return None
