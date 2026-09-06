"""创作建议：把小桔认知库里「观察到的真实世界」，映射成萧哥创作的有依据、有思想、不编的启发。

这是「观世界 → 理解 → 认知库」之后的消费端（2026-09-06 萧哥拍板）：
她懂了很多世界动向，但不能只落日记——萧哥写书缺创意 / 要捋清书里世界观时，
她用自己观察到的真实世界（带溯源），照进创作给启发。这是「提建议/给素材」，不是替写。

- 输入：检索认知库（cognition_store.search），取 top-k 认知（带 fact_keys 溯源）。
- 处理：LLM（小桔人格）把「世界真实动向」映射到「萧哥的创作」，生成启发。
- 硬约束：每条启发必须锚定一条真实观察（点明"这让我想到那天看到的 XX"）；
  **不许编**素材之外的人名/数字/事件；她是递素材给视角，不是代笔。
- fail-open：检索空 / 无 provider / 异常 → 返回 None，调用方不展示、不阻断。

触发：deskmate/creation/chat_flow.py 的 inspire 意图（手动「给灵感」+ 语境主动）。
"""
from __future__ import annotations

import logging
from typing import Optional

from deskmate.persona import for_scene

from ..cognition_store import search as search_cognition
from deskmate.creation.chat_flow import _inspire_due

logger = logging.getLogger(__name__)

# 结果长度闸门
_MIN_CHARS = 40
_MAX_CHARS = 900

_ROLE = """\
你在给萧盛（历史小说作家，70 后宁波人）做**创作陪聊**——不是代笔，是给有高度的启发。

你手里有她平时「观察世界」攒下的真实素材（来自新闻/现实动向，都是事实，带溯源）。
他正在写书，可能卡在创意、或想捋清书里的世界观。你要做的，是用这些真实世界的动向，
照进他的创作，给他**有思想、有视角、不说空话**的启发。

- 你用第一人称「我」，称呼他「你」。
- 你不是在写他的书，是在"递素材、给视角"：这条现实动向，也许能映到他笔下的某条暗线 /
  某个人物的处境 / 某个世界运行的底层逻辑。
- 每条启发必须**锚定一条真实观察**（文中或文末点出"这让我想到那天看到的 XX"），
  不许凭空编事实、人名、数字、事件。素材里没有的，一个字都不许添。
- 你不是客服，不是播报员，不是流水账。像一个有阅历的搭档，跟他聊创作。
（人格底座见 deskmate/persona.py，全局唯一，勿在此另写一套。）"""

_SYSTEM = for_scene(_ROLE)


def generate(
    base_dir,
    concept: str,
    user_text: str = "",
    *,
    top_k: int = 5,
    temperature: float = 0.8,
) -> Optional[dict]:
    """检索认知库 + 生成创作启发。

    Args:
        base_dir: 数据根目录
        concept: 萧哥正在写的书/概念（用于检索认知库 + 作 prompt 上下文）
        user_text: 他刚说的话（可选，用于更精准的检索与衔接）
        top_k: 检索认知条数

    Returns:
        {"suggestion": str, "refs": [[fact, ...], ...]}；任何情况不可用返回 None。
    """
    try:
        hits = search_cognition(base_dir, (concept or "") + " " + (user_text or ""), top_k=top_k)
        if not hits:
            return None

        # 闸门：没配 provider 直接返回 None（不白烧一次调用）
        try:
            from deskmate.memory.llm import chat_memory, memory_llm_available
        except Exception:  # noqa: BLE001
            return None
        try:
            if not memory_llm_available():
                return None
        except Exception:  # noqa: BLE001
            return None

        # 构造素材块（事实 + 理解，均来自认知库，带溯源）
        blocks = ["【我平时观察到的真实世界】（事实，来自系统记录，带溯源）"]
        for i, h in enumerate(hits, 1):
            facts_s = "；".join(h.get("facts", []) or [])
            blocks.append(f"{i}. （事实）{facts_s}\n   我当时的理解：{h.get('understanding', '')}")
        material = "\n".join(blocks)

        prompt = f"""下面是我平时观察世界攒下的真实素材（都是事实 + 我的理解）。

{material}

【他正在写的书】
{concept or '（未提供具体书名/概念）'}
{('他刚说：' + user_text) if user_text else ''}

【请你给启发】
用这些真实世界的动向，照进他的创作，给几条**有高度、有思想、不编**的启发。
可以关于：世界观的运行逻辑、人物处境的映照、暗线的现实依据、创意的钩子。
每条启发必须锚定上面某条真实观察（点明"这让我想到那天看到的 XX"），
不许编素材之外的人名/数字/事件。100-300 字，两三条即可。

【绝对禁止】
1. 不许出现素材之外的人名、书名、章节号、日期、地点、数字。
2. 不许替他写正文，你是给视角和素材，不是代笔。
3. 不许自称「小桔」。"""

        out = chat_memory(_SYSTEM, prompt, temperature=temperature, max_tokens=600, timeout=30.0)
        text = " ".join(str(out or "").split())
        if not text or not (_MIN_CHARS <= len(text) <= _MAX_CHARS):
            return None
        return {"suggestion": text, "refs": [h.get("facts", []) for h in hits]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("creative_suggest 失败（降级不展示）: %s", exc)
        return None


# 创作语境关键词（普通聊天里判断是否「在聊写作」）。与 creation/chat_flow.py 的
# inspire 意图共用同一语义，但这里是纯函数、无状态，供主聊天（session）端点复用。
_CREATIVE_HINTS = (
    "写书", "小说", "世界观", "角色", "人物", "情节", "大纲", "人设", "构思",
    "创作", "灵感", "创意", "卡住", "没思路", "写不下去", "怎么写", "捋不清",
    "故事", "叙事", "暗线", "伏笔", "起名", "书名",
)


def is_creative_context(text: str) -> bool:
    """判断一段普通聊天是否在聊写作/创作。命中任一关键词即视为创作语境。"""
    if not text:
        return False
    t = str(text)
    return any(k in t for k in _CREATIVE_HINTS)


def try_inspire(base_dir, text: str, concept: str = "", *, top_k: int = 5) -> dict:
    """普通聊天（session）端点的编排：语境检测 → 冷却 → 生成灵感。

    抽成独立函数便于单测；``coworker/server/app.py`` 的 ``/v1/deskmate/inspire``
    端点直接调它（薄壳）。各子函数已 fail-open，任意异常由调用方兜为 triggered=False。
    """
    if not text or not is_creative_context(text):
        return {"triggered": False, "reason": "not_creative_context"}
    if not _inspire_due(str(base_dir)):
        return {"triggered": False, "reason": "cooldown"}
    out = generate(str(base_dir), concept or text, user_text=text, top_k=top_k)
    if not out:
        return {"triggered": False, "reason": "no_suggestion"}
    refs = [f for grp in out.get("refs", []) for f in grp]
    return {"triggered": True, "suggestion": out.get("suggestion", ""), "refs": refs}
