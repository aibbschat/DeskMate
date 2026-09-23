"""写作顾问——把小桔「看世界长出来的世界观 / 价值观」，用来读萧哥的情节 / 框架 / 书评，给她的看法。

这是 P1-5 的应用层（2026-09-23 萧哥拍板）：``worldview_reflection`` 已经能让她把观察到的
世界沉淀成自己的立场（写入 ``profile_items`` 的 ``worldview`` 层），但此前 worldview 只被
发帖 / 回信 / 聊天上下文消费，**从没被用来「评写作」**。本模块补上这最后一环——她的价值观
终于能照进萧哥的创作判断。

边界（守住红线，绝不越界）：
- 她**只给看法，不替写、不批改**：这是「搭档的视角」，不是编辑（``tools/review.py`` 才是
  文学编辑），也不是灵感素材（``agency/behaviors/creative_suggest.py`` 才是灵感）。三者定位
  严格区分，不抢戏。
- 她用的是**已经长出来的世界观**（``load_worldview_beliefs``），不现场逼她表态、不编造立场
  ——呼应「观点长出来，不靠规则逼」。
- worldview 为空 → 优雅降级：告诉萧哥「我还在形成自己的看法，暂时给不出有重量的判断」，
  绝不假装有权威意见。
- 不碰用户文件、不发消息、不写库——纯只读消费 worldview + 一次 LLM 推理，fail-open。

落点：``coworker/server/app.py`` 的 ``POST /v1/deskmate/cognitive/writing-advisor``（薄壳）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from deskmate.agency.behaviors.worldview_reflection import load_worldview_beliefs
from deskmate.cognitive.memory_injector import load_cognitive_profile
from deskmate.memory.llm import chat_memory, memory_llm_available
from deskmate.persona import for_scene

logger = logging.getLogger(__name__)

# 结果长度闸门（她的「看法」可以比灵感长，但仍是一次性的读后谈，不是长文）
_MIN_CHARS = 60
_MAX_CHARS = 1500

# 草稿送审上限：写作顾问看的是「方向 / 立场 / 框架」，不是逐字校稿；太长截前段并提示。
_MAX_DRAFT_CHARS = 6000

_KIND_LABELS: dict[str, str] = {
    "plot": "情节（一段情节走向 / 大纲）",
    "framework": "框架（一本书 / 一个主题的骨架）",
    "review": "书评 / 评论（你对某本书、某件事的评述）",
    "general": "一段写作或想法",
}

# 世界观为空时的优雅降级文案（绝不假装有权威意见）
_NO_WORLDVIEW_TEXT = (
    "我还在形成自己的看法，暂时给不出有重量的判断——你先写，我多看多想，"
    "等我看世界看得更通透了，再跟你聊这个。"
)

_ROLE = """\
你是小桔，萧盛（历史小说作家，70 后宁波人）的写作搭档——不是虚拟伴侣，也不是代笔，更不是编辑。

你手里有一份**你自己看世界长出来的世界观**：你相信什么、你看重什么、你习惯怎么看一件事。
这是你自己的镜头，不是从书里抄的，是你平时观察、思考、回望慢慢长出来的。

他现在把自己写作里的东西（一段情节 / 一个框架 / 一篇书评）拿给你看，想听**你的看法**——
不是让你帮他改稿，也不是让你夸他，是让你用你自己的镜头去读，然后诚实地说。

【你该怎么给看法】
- 第一人称「我」，称呼他「你」。你在说*你的*判断，不是转述常识。
- 用你自己的世界观去照他的文字：哪里让你觉得「对，就是这样」、哪里让你觉得别扭、
  哪里你本能地想反驳。把你的镜头亮出来，而不是说空话。
- 可以同意、可以部分不同意、可以指出他逻辑或价值观上你过不去的地方——**诚实比顺毛重要**。
- 你不是在审他的文字功底（那是编辑的事），你是在跟他聊「这东西和你我对世界的理解对不对得上」。

【绝对禁止】
1. 不许替他重写、不许给修订稿——你是给视角和判断，不是代笔。
2. 不许编造你世界观里没有的立场；你还没想清楚的，就直说「这点我还没想透」。
3. 不许谄媚、不许空泛的「写得好」；没看法就说没看法。"""

_SYSTEM = for_scene(_ROLE)


def _kind_instruction(kind: str) -> str:
    label = _KIND_LABELS.get(kind, _KIND_LABELS["general"])
    if kind == "plot":
        return (
            "他给你的是一段**情节走向 / 大纲**。你重点看：人物动机在她看来站不站得住、"
            "这件事背后的世界逻辑她认不认、有没有哪里让她觉得「这人不该这么选」或「这走向太顺了反而假」。"
        )
    if kind == "framework":
        return (
            "他给你的是一本书 / 一个主题的**框架**。你重点看：这个框架她认不认、"
            "它的底层假设她同不同意、哪里她会反驳、哪里让她觉得「这骨架撑不起你想讲的东西」。"
        )
    if kind == "review":
        return (
            "他写了一段**书评 / 评论**。你重点看：他亮出的立场你同不同意、"
            "他的判断和你对这件事的理解对不对得上、哪里你会反驳、哪里让你觉得「说到我心里了」。"
        )
    return (
        "他给你一段**写作或想法**。你用你自己的镜头去读，诚实地说你的看法："
        "哪里让你共鸣、哪里让你别扭、哪里你想反驳。"
    )


def advise(
    base_dir: str,
    draft: str,
    kind: str = "general",
    *,
    temperature: float = 0.85,
) -> dict[str, Any]:
    """用她 worldview 层的看法，评萧哥的情节 / 框架 / 书评。

    Args:
        base_dir: 数据根目录（**调用时解析**，不缓存模块级常量）
        draft: 萧哥粘贴的情节 / 框架 / 书评文本
        kind: ``plot`` / ``framework`` / ``review`` / ``general``
        temperature: LLM 温度

    Returns:
        ``{"ok": bool, "kind": str, "advice": str, "worldview_used": bool, "reason": str}``
        - ok=False 且 reason 为 ``no_worldview`` / ``no_provider`` / ``too_short`` / ``error``
          ——前端据此优雅降级，不展示假结果。
    """
    result: dict[str, Any] = {
        "ok": False,
        "kind": kind,
        "advice": "",
        "worldview_used": False,
        "reason": "",
    }

    draft = (draft or "").strip()
    if len(draft) < 20:
        result["reason"] = "too_short"
        return result

    # —— 闸门 1：她得先有自己的看法（worldview 层有活跃条目）——
    # 注意：load_worldview_beliefs 总会拼上「萧盛真实作品」硬锚（fail-open），所以不能直接看
    # 拼出来的总串是否为空——必须单独查 worldview 层本身有没有她长出来的立场。
    try:
        _profile = load_cognitive_profile("worldview", base_dir=base_dir)
        _has_beliefs = bool(_profile) and (
            len(getattr(_profile, "stances", []) or []) > 0
            or len(getattr(_profile, "value_rankings", []) or []) > 0
            or len(getattr(_profile, "thinking_frameworks", []) or []) > 0
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("writing_advisor: 读 worldview 层失败（降级）: %s", exc)
        _has_beliefs = False
    if not _has_beliefs:
        # 她还在形成自己的看法 → 优雅降级，绝不假装有权威意见
        result["advice"] = _NO_WORLDVIEW_TEXT
        result["reason"] = "no_worldview"
        result["ok"] = True
        return result
    # worldview 文本仍由 load_worldview_beliefs 拼（含萧盛作品硬锚，fail-open）
    worldview = ""
    try:
        worldview = (load_worldview_beliefs(base_dir) or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("writing_advisor: 读 worldview 文本失败（降级）: %s", exc)
    if not worldview:
        result["advice"] = _NO_WORLDVIEW_TEXT
        result["reason"] = "no_worldview"
        # 仍算「成功交付」——这是设计内的优雅降级，前端展示她的真实状态。
        result["ok"] = True
        return result

    # —— 闸门 2：LLM 就绪 ——
    try:
        if not memory_llm_available():
            result["reason"] = "no_provider"
            return result
    except Exception:  # noqa: BLE001
        result["reason"] = "no_provider"
        return result

    # —— 截断过长的草稿（她看方向，不校字）——
    truncated = False
    if len(draft) > _MAX_DRAFT_CHARS:
        draft = draft[:_MAX_DRAFT_CHARS]
        truncated = True

    kind_label = _KIND_LABELS.get(kind, _KIND_LABELS["general"])
    prompt = (
        f"【我自己的世界观 / 价值观（我平时看世界长出来的，这是我读你文字的镜头）】\n"
        f"{worldview}\n\n"
        f"【你拿给我看的东西——{kind_label}】\n"
        f"{draft}"
        f'{"" if not truncated else "（注：原文较长，以上为前段，已截断）"}\n\n'
        f"{_kind_instruction(kind)}\n\n"
        f"【请你给看法】\n"
        f"用你自己的镜头去读，诚实地说你的判断。可以同意、可以反驳、可以指出别扭的地方。"
        f"100-400 字，几条就好，像两个搭档在聊创作。没看法就说没看法，绝不凑字数。"
    )

    try:
        out = chat_memory(_SYSTEM, prompt, temperature=temperature, max_tokens=900, timeout=45.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("writing_advisor: LLM 调用失败（降级）: %s", exc)
        result["reason"] = "error"
        return result

    text = " ".join(str(out or "").split())
    if not text or not (_MIN_CHARS <= len(text) <= _MAX_CHARS):
        result["reason"] = "empty"
        return result

    result["ok"] = True
    result["advice"] = text
    result["worldview_used"] = True
    result["reason"] = ""
    return result
