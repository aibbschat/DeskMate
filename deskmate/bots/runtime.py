"""真 Agent 运行时 —— 创作专家从「人设卡」升级为有 engine、有工具、自主决策的 bot。

萧哥 11-06 决策 2 的核实结论（代码实证，见 docs/bot-mode-redesign.md）：
专家的 ``coworker/personas/builtin/*.md`` 本来就是**完整 persona**（有 tools、
default_permission_mode、mcp 声明），跟小桔 deskmate.md 同构。旧圆桌之所以"不是
真 Agent"，是 ``roundtable/coordinator.py:585`` 只把 ``system_prompt`` 文本塞进
``chat_stream()`` 纯 prompt 注入 —— 没起 engine、没有工具、不能自己决定读稿。
这里改走官方 ``PersonaRegistry.agent()`` + ``build_engine()``，于是同一批 persona
变成能自主调用 ``read_book`` 的真 Agent。

三条硬约束：
1. **跟随 GUI 主模型，但必须走官方 ProviderRouter**（2026-11-06 萧哥拍板改判）：
   群聊用 ``prefs.json:default_model`` 选的模型，与主对话同源。旧版写死「绝不跟随」
   是因为当时自己拼裸 ``OpenAIProvider``，绕过了 ``registry`` descriptor —— opencode-go
   网关要 ``x-opencode-session``，裸调必得 400 MissingSessionID（11-06 圣经抽取事故）。
   现在交给 ``ProviderRouter``：它按 ``provider:`` 前缀选 client，descriptor 自带的合规
   会话头会一并生效，**裸调的原因根治了**，所以可以安全地跟随主模型。
   显式 ``DESKMATE_BOT_MODEL`` 覆盖优先；主模型缺失或为 ``auto`` 哨兵时才退回付费通道组
   （在 /api/health 里标明来源，不静默降级）。
2. **工具面只读**：``dataclasses.replace`` 换掉 tool_factory，再收紧 registry，
   只留 ``read_book`` / ``list_books``；write_file / apply_patch / run_shell /
   save_skill / a2a_call / web_fetch 全部摘掉 —— 书稿绝不进入可写工具的射程。
3. **有界**：max_iterations 封顶，每次群里一句话每个 bot 只跑一个 turn，
   群历史上限 20 条 / 总量 18000 字（**单条发言不截断**），读稿单次上限
   12000 字（读不全会把未加载章节名交回 bot 让它自己续读）。
   不 @ 不烧钱，@ 谁谁答。
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Optional, Sequence

from coworker.agent import build_engine
from coworker.personas.registry import get_registry

from .notebooks import NOTEBOOK_BOTS, NotebookSession, make_notebook_tools
from .reading import BookShelf, default_shelf, make_reading_tools
from .roster import KNOWN_BOT_IDS

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 深挖层 → 能力常备化（2026-11-08 萧哥两步拍板）。
#
# 第一步（上午）：给「深挖」配显式触发词 + 双轨 engine —— 当天真机翻车：萧哥
# 照 Hermes 的习惯说「先读原稿」「我需要脑洞大开的安排和设计」，术语词表
# 一个没碰上，深挖层没启动。扩词表只是补漏。
# 第二步（拍板原话「你不能靠触发词。如果功能是齐的，但启动要靠触发词，那
# 以后如果换了本书，换个要讨论的场景呢」）：拆掉开关层，回归 Hermes 的
# 设计本身——**能力常备，模型自主**。read_book 无条件全书（reading.py 的
# make_reading_tools 默认 deep_read_chars）、迭代上限统一 12（见
# _DEFAULT_MAX_ITERATIONS 注释）、「什么时候下重手」由 guidance / 工具
# docstring / 人设教给模型判断，不由代码开关决定。
#
# 保留的两样（都不是开关）：
# - discussion.is_deep_dig：**信号放大器**而非权限闸——命中时 rooms 给全书
#   材料前缀 + build_prompt 给强作业指令（省得模型自己试探 + prefix cache
#   跨轮命中）；没命中时能力照样齐，模型自己 read_book 全书。词表不全顶多
#   少一点强指令，不再有「功能没启动」这回事。
# - materials 深挖版全书前缀：显式强意图时直接喂全书（同上，缓存友好）。
# --------------------------------------------------------------------------- #

# 群聊层的行为约束：这是「在群里说人话」，不是派活 / 验收 / 写报告。
# 2026-11-07 换骨架（萧哥定调「个性和立场立住，才出得了观点」）：
# 旧版三大病灶（真机 11-06 夜《夜不收》定义讨论实证）——①「要具体」写死在
# 伏笔/句子层，把立意讨论拽进细节螺旋；②只教「回应/补充/反对」，不教站队
# 和升层，全员对抗式起手、四平八稳；③没人管发言形态，全员 1500-2500 字
# markdown 文档体。新版按层级、立场、形态三条治，红线项（只读/无纪要/
# 不署名/pass/不编造）一字未减。
_GROUP_GUIDANCE = """

# 你现在在一个 bot 群里

- 先看清此刻聊的是哪个层级的事：全书立意 / 结构走向 / 情节设计 / 文字打磨。
  回答待在同一层级或往上走——聊立意别揪到一段一句，除非点名要细节；
  发现大家陷在细节里、而真正要定的是大方向时，把层级拉回来。
- 用**自己的立场**说人话，别当中立综述员：可以站队（「我认谁那条，因为…」）、
  可以反对、可以说「我觉得这个好 / 这个不行」。「一方面…另一方面…」式的
  四平八稳不算发言；有判断才算。
- **先分清他要的是脑洞还是方案**：要脑洞／方向／思路时，给几条真正不同的，
  允许半成品——标明哪条从正文长出来、哪条是半空的；**不排序、不替他挑、不写成
  分层方案或大纲**。他说「就这条／往下推／给完整方案」时才进往下盘：盘局势、查
  伏笔、算代价，每个动作过三问——谁有权让它发生、对手怎么破、其中的人凭什么照办。
- 要具体，但具体到层级：夸就点出妙在哪，反对就反对在哪个点上；往下盘时给情节
  方向就交代伏笔埋在哪、读者会在哪一句愣住。不写「可以考虑加个反转」这类空话。
  纯接话、补一句的轮次不必重复展开已经说过的部分。
- **读多少由题决定**：read_book 一次能读很多章，缺哪段就按章节点名补读
  （如 chapter="第十一章" / "第五到第七章"）；读多少、补什么，你自己判断——
  你自己的工具你自己做主，没人替你开权限。
- 群里共享的正文**不一定装得下全书**：索引里写着「正文共 N 字、本次加载了
  哪几章、哪些章本次未加载、停笔点在内」——**没加载的章不许当成读过**，更别
  拿开头硬推全局；缺的章节用 read_book 点名补读。非正文材料（大纲／简报／
  圣典／文风）默认不加载，也不能拿它们当既定框架；需要核实事实时才点名补读。
  绝不问用户要路径、绝不要求填任何表格字段。读不到就直说读不到，不要编造情节或段号。
- 像在群里说话，别像在写文档：不用加粗、分隔线、小标题、编号列表这些排版
  记号，要分点就「一、二、三」顺下去。首轮可以展开说透；接话轮常常三五句就够。
- 只读，不写文件、不改稿、不派活、不建工作项、不做审核验收、不打回重改。
- **不生成纪要 / 总结 / 主持人收尾**；不要替别人发言，也不要复述群历史。
- 群里已经显示你的名字，**不要**在发言开头加署名、来源标注、@ 前缀或
  「（来自…）」这类括号说明，直接从观点说起。
- 群里其他人的发言已经在上下文里，接话而不是重新开始。
- 后面几轮如果没有新的可说，直接回复 pass，不要为了凑数重复自己的观点。
"""

# 一对一作业形态（2026-11-08 萧哥实证固化：Hermes 独立专家对照群聊，独立胜出）。
# 群聊 guidance 是为「碰撞与真实感」设计的（短发言／不排版／站队），但脑洞
# 作业要的是完整性 —— 单 agent、长 turn、无同行在场的审视压强，才产得出
# 系统级方案。@ 单独一位 bot（或单成员群）即进此形态：红线保留（只读 /
# 不署名 / 不编造 / 不问路径），形态约束（不排版／三五句）全部卸掉。
# 预算与群聊完全一致（能力常备），差异只在「说话的场合」。
_SOLO_GUIDANCE = """

# 作者这次是单独找你

- 没有别人在场，不用照顾群聊的节奏。把**你真正认的东西**说清：开脑洞时
  几条火花就够，作者挑中一条让你往下盘时，才该三千字就三千字、该分层就分层。
- 书稿材料（正文，覆盖范围见索引）就在上下文里，直接用；**索引里标「本次
  未加载」的章节要写就先用 read_book 点名补读**，别拿开头硬推全局。
  绝不问用户要路径、绝不要求填任何表格字段。读不到就直说读不到，
  绝不编造情节、段号、出处。
- 只读，不写文件、不改稿、不派活、不建工作项。
- 不生成纪要 / 总结 / 收尾汇报；写完你想说的就停，别补「以上是建议」这类尾巴。
- 界面已经显示你的名字，不要在发言开头加署名、来源标注或「（来自…）」。
"""

_HISTORY_LIMIT = 20
# 群历史给模型的总字数预算（不是单条发言上限）。旧实现把每条硬砍到 600 字，
# 1676 字的具体建议只传了前 1/3 给后发言者，讨论必然退化成空泛附和 —— 撤掉
# 单条截断，改用「总量有界 + 从最新往回取」：够装下 10 条完整长发言，又不随
# 轮次线性膨胀。真正限制长篇的是人设输出格式与 provider 的 max_tokens，不是这里。
_HISTORY_BUDGET = 18_000
# turn 内工具循环上限（2026-11-08 萧哥拍板「不能靠触发词」后统一放宽）。
# 旧值 4 是 11-06 的成本护栏，但真机实证它物理上盘不了全局势（第 8~11 章
# 散点伏笔要多次点名补读）。统一 12：对**不读稿的普通发言边际成本为零**
# （上限是「允许」不是「消耗」，不调工具就是 0 轮）；要下重手的题模型自己
# 读全书、多轮补读。真正的成本上界来自「工具面只读 + 单次 8 万字 + 上下文
# 窗口 128K」——窗口物理封顶累积，比迭代数更硬。发言轮次护栏（3 轮 / 10 条）
# 与此无关，一个字不动。
_DEFAULT_MAX_ITERATIONS = 12

# 模型爱在发言开头自加一行署名/来源标注。prompt 里已明令禁止，但模型不保证遵守
# （2026-11-06 真机实测：补厚人设后仍出现「（来自 @brainstormer · 创意脑洞专家）」）。
# 这是模型自加的**元数据噪声**，不是表达内容 —— 落库前做确定性剥离，
# 不靠模型自觉、也不加任何语气/话术约束（守「真实优先」红线）。
# 只认开头单独一行的两种形态：括号「来自 @…」或「@id：」；正文里的括号不受影响。
_ATTRIBUTION_LINE = re.compile(
    r"^\s*(?:[（(]\s*来自\s*@?[^)）]{0,80}[)）]|@[\w.\-]+\s*[:：])\s*$"
)


def strip_self_attribution(text: str) -> str:
    """剥掉开头那一行的自加署名，正文一字不改（纯字符串清洗，零 LLM）。"""
    s = str(text or "")
    lines = s.splitlines(keepends=True)
    if not lines:
        return ""
    first = lines[0].rstrip("\r\n")
    if not _ATTRIBUTION_LINE.match(first):
        return s.strip()
    return "".join(lines[1:]).lstrip("\r\n").strip()


#: 显式覆盖（验收 / 回退）：``DESKMATE_BOT_MODEL=provider:model``，优先于 GUI 主模型。
MODEL_OVERRIDE_ENV = "DESKMATE_BOT_MODEL"


def _main_model_from_prefs() -> str:
    """读 GUI 主模型（``prefs.json:default_model``），与 SessionManager 同源。

    只读取、不写、不缓存：设置面板改了模型，下一个新建的 bot engine 立刻跟上
    （已缓存的 engine 仍用旧模型，与「换模型要开新会话」的既有约定一致）。
    """
    from coworker.secrets import state_dir

    try:
        path = state_dir() / "prefs.json"
        if not path.is_file():
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("default_model") or "").strip()
    except Exception:  # noqa: BLE001 —— prefs 损坏不该让群聊崩在 import/构造期
        return ""


def model_choice() -> tuple[str, str, str]:
    """这次群聊用哪个模型：``(model, source, reason)``，**零网络、零凭证解析**。

    - ``override``：``DESKMATE_BOT_MODEL`` 显式指定，完整 ``provider:model`` 字符串；
    - ``main``：GUI 主模型，同样是完整字符串，交给 ``ProviderRouter`` 自己剥前缀；
    - ``paid_fallback``：主模型缺失或是 ``auto`` 动态路由哨兵（没有具体通道可直连），
      退回付费通道组；``model`` 为空，由 ``paid_credentials()`` 解析。

    ``reason`` 只在 fallback 时非空，供 ``/api/health`` 展示——**不静默降级**。
    """
    override = os.environ.get(MODEL_OVERRIDE_ENV, "").strip()
    if override:
        return override, "override", ""
    main = _main_model_from_prefs()
    if main:
        try:
            from coworker.providers.dynamic_route import AUTO_MODEL
        except Exception:  # noqa: BLE001
            AUTO_MODEL = "auto"
        if main == AUTO_MODEL:
            return "", "paid_fallback", "主模型是 auto 动态路由，群聊需要明确模型"
        return main, "main", ""
    return "", "paid_fallback", "prefs 未设 default_model"


def resolve_default_provider_model() -> tuple[Any, str, str]:
    """一次解析出 ``(provider, model, source)``，防 provider / model 各自路由串味。

    ``main`` / ``override`` 走官方 ``ProviderRouter``：它按前缀挑 client，
    ``opencode-go`` 这类网关 descriptor 自带的 UA + ``x-opencode-session`` 会生效，
    不会重演 400 MissingSessionID。``paid_fallback`` 才用裸 OpenAIProvider（付费组
    成员都是 OpenAI 兼容直连，不需要会话头）。
    """
    model, source, _reason = model_choice()
    if source == "paid_fallback":
        from coworker.providers.openai_provider import OpenAIProvider

        api_key, base_url, channel_model = paid_credentials()
        return (
            OpenAIProvider(api_key=api_key, base_url=base_url, default_model=channel_model),
            channel_model,
            source,
        )
    from coworker.providers.router import ProviderRouter
    from coworker.secrets import SecretStore

    return ProviderRouter(secrets=SecretStore()), model, source


def paid_credentials() -> tuple[str, str, str]:
    """取付费通道组里第一条可用通道的 ``(api_key, base_url, model)``。

    与 ``deskmate.memory.llm.chat_memory`` 同源：走 ``llm_channels.route(GROUP_PAID)``
    （已滤熔断），不绑死某一个厂商；凭证统一由 SecretStore 经 ``_creds_for`` 取。
    """
    from deskmate.llm_channels import GROUP_PAID, route
    from deskmate.memory.llm import _creds_for

    channel, _skipped = route(GROUP_PAID)
    if channel is None:
        raise RuntimeError("付费通道组无可用通道（凭证缺失或全组熔断），bot 群无法发言")
    api_key, base_url, model = _creds_for(channel.provider, channel.model)
    if not (api_key and base_url):
        raise RuntimeError("付费通道凭证不完整，bot 群无法发言（fail-fast）")
    return str(api_key), str(base_url), str(model)


def default_provider() -> Any:
    """**付费组兜底**用的 OpenAI 兼容 provider（保留给 fallback 与旧调用方）。

    2026-11-06 起这已**不是**群聊默认：默认走 ``resolve_default_provider_model()``
    的官方 ``ProviderRouter``（跟随 GUI 主模型，且自带网关会话头）。此函数仅供
    「主模型缺失 / auto」的付费组回退与回归测试使用，别再当成默认入口。
    """
    from coworker.providers.openai_provider import OpenAIProvider

    api_key, base_url, model = paid_credentials()
    return OpenAIProvider(api_key=api_key, base_url=base_url, default_model=model)


def default_model() -> str:
    """付费组兜底模型（兼容旧调用方）；群聊实际模型请看 ``model_choice()``。"""
    return paid_credentials()[2]


def _run_sync(coro) -> Any:
    """跑一个协程；已在 event loop 里时挪到独立线程，避免 asyncio.run 冲突。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def format_history(
    history: Sequence[dict] | None,
    *,
    names: Mapping[str, str] | None = None,
) -> str:
    """群历史 → 一段纯文本上下文（有上限，防上下文膨胀烧钱）。

    ``names`` 把 ``author``（bot_id，如 historian）映射成群里显示的中文名
    （如「史料考据」）—— 人设里的接话示范写的都是中文名，模型看到
    ``historian（bot）: ...`` 是认不出「文学编辑说…」该接谁的。
    """
    rows = list(history or [])[-_HISTORY_LIMIT:]
    if not rows:
        return ""
    lookup = {str(k): str(v) for k, v in (names or {}).items()}
    lines: list[str] = []
    used = 0
    omitted = 0
    # 从最新往回取：预算不够时优先保住最近的完整发言（结论通常在结尾）。
    for m in reversed(rows):
        author = str(m.get("author") or "?")
        who = lookup.get(author, author)
        role = str(m.get("author_role") or "")
        tag = "（我）" if role == "user" else f"（{role}）"
        text = str(m.get("text") or "").strip()
        if not text:
            continue
        room = _HISTORY_BUDGET - used
        if room <= 0:
            omitted += 1
            continue
        if len(text) > room:
            text = "…" + text[-room:]
        used += len(text)
        lines.append(f"{who}{tag}: {text}")
    if not lines:
        return ""
    lines.reverse()
    if omitted:
        lines.insert(0, f"（更早 {omitted} 条发言未载入上下文）")
    return "群内已有发言：\n" + "\n".join(lines)


def build_prompt(
    text: str,
    history: Sequence[dict] | None,
    *,
    names: Mapping[str, str] | None = None,
    round_index: int = 1,
    materials: Any = None,
    deep: bool = False,
    solo: bool = False,
    notebook: str = "",
) -> str:
    """构造一次发言的 prompt。

    第 1 轮由用户原话驱动；第 2 轮起是 bot 之间接话，**不重复用户原话**
    （它已经在群历史里了），改为要求挑一个具体观点回应或显式 pass。

    ``materials`` 是群级共享材料包（``RoomMaterials.prepare`` 的产物），放在
    最前面当**固定前缀**：同一群所有 bot 拿到字节一致的一份，既省掉各自
    「找文件」的往返，也让 provider 的前缀缓存有机会命中。它是原文分块，
    不是摘要；不传时行为与旧版完全一致。

    ``deep=True``（2026-11-08 深挖层）：lead 换成深挖作业指令 —— 先把材料／
    全稿读全、盘清局势，再出**系统级**方案；不受平常群聊发言的长度约束。
    这是把 Hermes「脑洞风暴」实证有效的工作法（先盘局势再开脑洞）交给 bot，
    只在用户显式触发时生效。
    """
    prior = format_history(history, names=names)
    if solo:
        # 一对一作业形态（2026-11-08 萧哥实证固化）：作者单独找它。lead
        # 不带群聊语义（「群里刚有人说」），不带形态约束（三五句），只有
        # 作业路径——先读该读的，直接交认定的结果，过程不汇报。
        lead = (
            f"作者对你说：{text}\n\n"
            "这是你和他的单独对话。先把该读的读了（材料是正文，覆盖范围见索引；"
            "索引里标「本次未加载」的章节先用 read_book 补读），然后把你真正认的东西完整给出来——盘局势、找支点"
            "这些是你脑子里的活，直接交结果，不用写过程汇报；长短与结构"
            "服从题目，写透为止。"
        )
    elif deep:
        lead = (
            f"群里刚有人说：{text}\n\n"
            "这是一次**深挖任务**，不是普通群聊发言。材料包里是正文，覆盖范围见索引；"
            "索引里标「本次未加载」的章节用 read_book 点名补读，把局势盘全了再动笔。作业路径：\n"
            "一、先盘局势：问题涉及的对象（对手／系统／困局）到底怎么运作，"
            "逐条列清它的构成——地形、编制、纪律、甄别、后勤、依仗，一条条落实。\n"
            "二、再找隐性支点：系统越精密越依赖少数环节。哪些东西要人维持、"
            "哪些链条是单线、哪个要害在系统里、什么时候系统被迫自己开口。\n"
            "三、然后才是方法论：不硬撬，想怎么让这套系统**自己裂开**。\n"
            "四、方案要分层咬合、互相配合成一个整体，每个动作都回收前文已有的"
            "伏笔——说得出依据在第几章、哪句话。用不上的前文，明说用不上。\n"
            "写透为止，不受平常群聊发言的长度约束；但别写成排比空话，"
            "每一条都要落到能落笔的动作。"
        )
    elif round_index <= 1:
        lead = f"群里刚有人说：{text}\n\n请直接以你自己的身份发言。"
    else:
        # 2026-11-07：旧版「挑一个具体观点回应/补充/提出不同意见」把多轮讨论
        # 推成对抗式细节螺旋（真机实证：全员「我先反对」起手、越接越细）。
        # 新版把「站队 / 升层 / 短接话」立为合法形态——收敛不靠主持人，靠每位
        # 都有权说「我认谁哪条」和「先回到大方向」。保留「第 N 轮接话」开头，
        # 回归断言（test_bot_discussion）兼容。
        lead = (
            f"这是第 {round_index} 轮接话。前面几位的发言都在上下文里。"
            "接话不必长篇：明确站队（认谁哪条、为什么）、反驳、补一刀、"
            "或把讨论拉回大方向，都算发言；三五句常常比一段展开更有用。"
            "如果是开脑洞轮，可以顺着某条往下拧，也可以抛一条完全不同的——"
            "别把大家的话题收敛成一个方案。"
            "不要复述别人说过的话；没有新话可说就回 pass，别硬凑。"
        )
    block = ""
    if materials is not None:
        block = (
            materials.prompt_block()
            if hasattr(materials, "prompt_block")
            else str(materials)
        )
    nb = ""
    if notebook:
        nb = (
            "# 你的笔记本（私人，只有你看得到）\n\n"
            "下面是你在《这本书》上记下的东西（跨会话）。它只是你的判断和作者反馈，"
            "不是正文；和正文/作者最新裁定冲突时，以正文和裁定为准。\n\n"
            + notebook
            + "\n\n"
        )
    return (block + "\n\n" if block else "") + nb + (prior + "\n\n" if prior else "") + lead


def _safe_segment(value: str) -> str:
    """room_id 落进目录名前的兜底清洗（group_id 正常是 uuid hex）。"""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value))[:64] or "_default"


class BotRuntime:
    """每个 ``(room, bot)`` 一个常驻 engine，群只是临时协调面。

    ``provider`` 可注入，便于离线测试真 Agent 的自主工具决策；默认走付费通道。

    上下文只有一份：``ChatStore`` 的群历史（截断 20 条）+ 当前这句。engine
    每轮开始前清空 ``messages``，不再把群历史的副本累积进去 —— 否则
    「群历史 / engine.messages / 当前句」会是三重冗余，既费 token 又让 bot 复读。
    跨发言的连续记忆由群历史承载，且天然按 room 隔离，A 群的话不会漏进 B 群。
    """

    def __init__(
        self,
        *,
        books_root: str | Path | None = None,
        workspace_root: str | Path | None = None,
        provider: Any = None,
        model: str | None = None,
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    ) -> None:
        self.shelf: BookShelf = (
            BookShelf(Path(books_root)) if books_root is not None else default_shelf()
        )
        self.workspace_root = Path(
            workspace_root
            or os.environ.get("DESKMATE_BOT_WORKSPACES")
            or Path("~/.config/coworker/bot_workspaces")
        ).expanduser()
        self.provider = provider
        self.model = model or ""
        self.max_iterations = max(2, int(max_iterations))
        # key = (room_id, bot_id)：同一个 bot 在不同群是两份独立会话，不串味。
        self._engines: dict[tuple[str, str], Any] = {}
        self._lock = threading.RLock()
        # 同一个 (room, bot) 不能同时跑两个 turn（messages 会被交错写）。
        # 用 threading.Lock 而非 asyncio.Lock：speak() 走 threadpool / 独立 loop，
        # speak_stream() 走 FastAPI 主 loop，threading 锁跨两个上下文都成立。
        self._turn_locks: dict[tuple[str, str], threading.Lock] = {}

    @staticmethod
    def _key(room_id: str | None, bot_id: str) -> tuple[str, str]:
        return (str(room_id or "_default"), str(bot_id))

    def _turn_lock(self, key: tuple[str, str]) -> threading.Lock:
        with self._lock:
            lock = self._turn_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._turn_locks[key] = lock
            return lock

    # ---------------------------------------------------------------- engine
    def engine(self, bot_id: str, *, room_id: str | None = None, solo: bool = False) -> Any:
        """构造（并缓存）该 bot 在该群的官方 TurnEngine。

        ``room_id`` 省略时落进 ``_default`` 桶（兼容阶段 2 的离线调用与测试）。
        预算单一常备（12 迭代 / read_book 全书）—— ``solo`` 只是**场合**差异：
        一对一作业形态用 ``_SOLO_GUIDANCE``（卸掉群聊形态约束，红线保留），
        engine 缓存键带 ``:solo`` 后缀，同一 bot 群聊／单聊两副样子互不污染。
        """
        bot_id = str(bot_id).strip()
        if bot_id not in KNOWN_BOT_IDS:
            raise ValueError(f"未知 bot：{bot_id}（bot 必须来自常驻名册）")
        if solo:
            room_key = f"{str(room_id or '_default')}:solo"
        else:
            room_key = str(room_id or "_default")
        key = (room_key, bot_id)
        with self._lock:
            if key in self._engines:
                return self._engines[key]

            entry = get_registry().get(bot_id)
            if entry is None:
                raise ValueError(f"persona '{bot_id}' 未注册，无法起真 Agent")
            agent = entry.agent()
            tools = make_reading_tools(self.shelf)
            notebook: NotebookSession | None = None
            guidance = _SOLO_GUIDANCE if solo else _GROUP_GUIDANCE
            if bot_id in NOTEBOOK_BOTS:
                # 专家笔记本：只给这三位；工具只作用在自己的 notes.md。
                # 笔记本纪律写在各人设的「你的笔记本」一节（群聊/1v1 共用）。
                notebook = NotebookSession(bot_id, self.shelf)
                tools = tools + make_notebook_tools(notebook)
            agent = dataclasses.replace(
                agent,
                tool_factory=lambda _ctx: tools,
                system_prompt=(agent.system_prompt or "") + guidance,
                # 群 bot 不需要文件夹门禁 / 定时唤醒 / 消息外发 / 连接器
                requires_folder=False,
                scheduling=False,
                messaging=False,
                connectors=False,
                team=None,
            )
            ws = self.workspace_root / _safe_segment(key[0]) / bot_id
            ws.mkdir(parents=True, exist_ok=True)
            if self.provider is not None and self.model:
                # 显式注入（离线测试 / 验收）：provider 与 model 成对给，直接沿用。
                provider, model = self.provider, self.model
            else:
                # 一次解析，避免 provider / model 各自路由到不同通道。默认跟随 GUI
                # 主模型并走官方 ProviderRouter；缺主模型 / auto 时才回付费组，且
                # resolve_default_provider_model 会在 paid_credentials 抛错时 fail-fast。
                provider, model, source = resolve_default_provider_model()
                logger.info("bot 群模型来源=%s model=%s", source, model)
            eng = build_engine(
                agent=agent,
                provider=provider,
                model=model,
                workspace=str(ws),
                max_iterations=self.max_iterations,
            )
            # 🔴 反向回写隔离（2026-11-07）：build_engine 在 coworker/agent.py 硬编码
            #    挂了 post_turn_hook=_deskmate_post_turn_hook，bot engine 全盘继承 ⇒
            #    每轮把「群里刚有人说…」当用户消息、bot 发言标成「（小桔本轮回应）」，
            #    写进 sessions/、USER/MEMORY、萧盛影响素材池，还会用 bot 的话进化
            #    小桔技能。群 bot **只读**共享记忆（context_provider 保留），绝不回写。
            #    钩子是 fire-and-forget daemon，摘掉不影响本轮发言。
            eng._post_turn_hook = None
            # 同一红线：engine.py::_xiaosheng_note 不走 hook，是独立的萧盛素材写入口。
            # 群 bot 一律 fail-closed，哪怕以后工具面放开 web_search 也不回写。
            eng.deskmate_memory_enabled = False
            # 收紧工具面：build_engine 还会固定注册一堆 web/a2a/skill 工具。
            # 书稿面**只读**（read_book / list_books）；只有带笔记本的三位多三个
            # 私有笔记本工具（写只落 <书>/.deskmate/experts/<bot>/notes.md）。
            allowed = {"read_book", "list_books"}
            if notebook is not None:
                allowed |= {"notebook_read", "notebook_remember", "notebook_rewrite"}
            eng.registry._tools = {
                name: spec
                for name, spec in eng.registry._tools.items()
                if name in allowed
            }
            eng._notebook_session = notebook
            self._engines[key] = eng
            return eng

    @staticmethod
    def _notebook_digest(eng: Any, materials: Any) -> str:
        """把「当前这本书」的笔记本摘要取出来；没书/没笔记/读失败都返回空。"""
        session = getattr(eng, "_notebook_session", None)
        if session is None:
            return ""
        book = getattr(materials, "book", None) if materials is not None else None
        try:
            session.set_book(book)
            return session.digest()
        except Exception as exc:  # noqa: BLE001 —— 读笔记失败不能拖垮发言
            logger.warning("专家笔记本读取失败（本轮跳过）: %s", exc)
            return ""

    # ---------------------------------------------------------------- speak
    def speak(
        self,
        bot_id: str,
        text: str,
        *,
        history: Sequence[dict] | None = None,
        names: Mapping[str, str] | None = None,
        room_id: str | None = None,
        round_index: int = 1,
        materials: Any = None,
        deep: bool = False,
        solo: bool = False,
    ) -> str:
        """说一句人话 → bot 自主决定要不要读稿 → 返回它这一轮的署名发言。

        ``deep=True`` 放大作业指令；``solo=True`` 进一对一作业形态
        （``_SOLO_GUIDANCE``，engine 键 ``:solo``）。两者可叠加：单独深聊。
        预算永远单一常备（12 迭代 / read_book 全书）。
        """
        eng = self.engine(bot_id, room_id=room_id, solo=solo)
        lock_room = f"{room_id or '_default'}:solo" if solo else room_id
        with self._turn_lock(self._key(lock_room, bot_id)):
            return self._speak_locked(
                eng,
                text,
                history=history,
                names=names,
                round_index=round_index,
                materials=materials,
                deep=deep,
                solo=solo,
                notebook=self._notebook_digest(eng, materials),
            )

    def _speak_locked(
        self,
        eng: Any,
        text: str,
        *,
        history: Sequence[dict] | None,
        names: Mapping[str, str] | None,
        round_index: int,
        materials: Any = None,
        deep: bool = False,
        solo: bool = False,
        notebook: str = "",
    ) -> str:
        """兼容旧 ``/say`` 契约的一次性发言（整段返回）。"""
        # 群历史是唯一上下文源：清掉上一轮的累积副本，避免三重冗余 / 复读。
        eng.messages.clear()
        prompt = build_prompt(
            text, history, names=names, round_index=round_index,
            materials=materials, deep=deep, solo=solo, notebook=notebook,
        )
        before = len(eng.messages)
        _run_sync(self._drive(eng, prompt))
        replies = [
            str(m.get("content") or "")
            for m in eng.messages[before:]
            if m.get("role") == "assistant" and str(m.get("content") or "").strip()
        ]
        return strip_self_attribution(replies[-1].strip()) if replies else ""

    @staticmethod
    async def _drive(eng: Any, prompt: str) -> None:
        """旧契约的一次性发言。事件不往前端推，但 **error 绝不吞**：
        engine 的 payload 是 ``{"error": ..., "error_type": ...}``，旧实现全部
        pass 掉，配额超限 / 通道 429 会被误当成「bot 没说话」。抛出去让
        ``RoomStore.say`` 的 except 记下真实原因（行为仍是不落库）。"""
        async for event in eng.run(prompt):
            kind = getattr(getattr(event, "type", None), "value", None)
            if kind is None:
                kind = str(getattr(event, "type", ""))
            if kind == "error":
                data = getattr(event, "data", None) or {}
                detail = str(data.get("error") or data.get("message") or "bot 发言出错")
                etype = str(data.get("error_type") or "error")
                raise RuntimeError(f"{etype}: {detail}")

    # ------------------------------------------------------------ speak stream
    async def speak_stream(
        self,
        bot_id: str,
        text: str,
        *,
        history: Sequence[dict] | None = None,
        names: Mapping[str, str] | None = None,
        room_id: str | None = None,
        round_index: int = 1,
        materials: Any = None,
        deep: bool = False,
        solo: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """**原生流式发言**：把官方 TurnEngine 的逐 token 事件原样透出来。

        与 ``speak()`` 不是两套 LLM 流 —— 同一 engine / 同一 prompt / 同一份常驻
        记忆，唯一区别是旧的 ``_drive`` 用 ``pass`` 把事件全丢了，这里把
        ``ASSISTANT_DELTA`` 转成 ``delta`` 事件给 SSE 消费。

        事件：``tool``（bot 自主读稿时）/ ``delta``（逐 token）/ ``error`` /
        ``final``（权威最终文本，供落库与前端覆盖气泡，保证与 delta 拼接一致）。

        ``deep=True`` 放大作业指令；``solo=True`` 进一对一作业形态。预算常备。

        被 ``aclose()``（客户端断开 → GeneratorExit）时调 ``request_interrupt()``
        真正掐断 provider 流，避免「页面关了 token 还在烧」（engine.py:316）。
        """
        eng = self.engine(bot_id, room_id=room_id, solo=solo)
        lock_room = f"{str(room_id or '_default')}:solo" if solo else room_id
        lock = self._turn_lock(self._key(lock_room, bot_id))
        if not lock.acquire(blocking=False):
            # 非阻塞：拿不到锁不能阻塞 FastAPI 的 event loop。
            yield {"type": "error", "message": f"{bot_id} 正在发言，请稍候再试"}
            return
        try:
            eng.messages.clear()
            prompt = build_prompt(
                text, history, names=names, round_index=round_index,
                materials=materials, deep=deep, solo=solo,
                notebook=self._notebook_digest(eng, materials),
            )
            streamed: list[str] = []
            final_text = ""
            try:
                async with contextlib.aclosing(eng.run(prompt)) as events:
                    async for event in events:
                        kind = getattr(event.type, "value", str(event.type))
                        data = event.data or {}
                        if kind == "assistant_delta":
                            piece = str(data.get("text") or "")
                            if piece:
                                streamed.append(piece)
                                yield {"type": "delta", "text": piece}
                        elif kind == "tool_started":
                            yield {
                                "type": "tool",
                                "name": str(data.get("name") or ""),
                            }
                        elif kind == "assistant_message":
                            # 带 tool_calls 的是中间轮（模型边调工具边说的话），
                            # 只有不带工具调用的最后一轮才是要落库的正式发言。
                            if not data.get("tool_calls"):
                                final_text = strip_self_attribution(
                                    str(data.get("text") or "")
                                )
                        elif kind == "error":
                            # engine 给的是 {"error": ..., "error_type": ...}，不是
                            # "message"。旧代码把它吞成「bot 发言出错」，429 配额
                            # 超限看起来就像 bot 弃权，无法判断该等还是该修。
                            detail = str(
                                data.get("error") or data.get("message") or "bot 发言出错"
                            )
                            yield {
                                "type": "error",
                                "message": detail,
                                "error_type": str(data.get("error_type") or ""),
                            }
            except GeneratorExit:
                eng.request_interrupt()
                raise
            except Exception as exc:  # noqa: BLE001 —— 一个 bot 挂了不拖垮整群
                logger.warning("bot %s 流式发言失败：%s", bot_id, exc)
                yield {"type": "error", "message": str(exc)}
            else:
                # 兜底：某些 provider 不吐 ASSISTANT_MESSAGE，用 delta 拼接。
                if not final_text:
                    final_text = strip_self_attribution("".join(streamed))
                yield {"type": "final", "text": final_text}
        finally:
            lock.release()

    def close(self) -> None:
        with self._lock:
            self._engines.clear()
