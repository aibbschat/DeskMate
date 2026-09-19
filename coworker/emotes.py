"""小桔表情 · 后端注册表（P1 起由 emotes.manifest.json 单一真相源驱动）。

``emotes.manifest.json`` 是「接入标准」：定义有哪些表情、各自的中文含义、触发场景、
分类、情绪、所属 pack。本模块只做几件事，且都从 manifest 派生，不再硬编码：

- ``expand_emotes_in_content``：把用户消息里的 emote 标记展开为语义说明
  （``_outbound_messages`` 在送进 LLM 前调用，让模型真正「理解」表情含义）。
- ``emote_system_block``：生成注入系统上下文的表情词表 + 规则 + few-shot
  （``context_provider`` 每轮注入，保证新/恢复会话都可见）。
- ``suggest_emote`` / ``emotes_by_emotion`` / ``emotes_by_category``：表情注册表查询 API
  （按场景/情绪/分类检索表情，供后端事件、主动消息、确定性回退使用）。

manifest 支持多 Pack（表情专辑）：每个 pack 可在 ``emotes`` 里自带语义，或用 ``inherit``
引用另一 pack 的语义（同 id 换画风）。本模块加载时自动展开 inherit。
manifest 存放位置（与前端共用同一文件）：
    <repo>/surfaces/gui/src/components/deskmate/emotes/emotes.manifest.json
前端直接 ``import`` 该 JSON；后端按相对路径读取，确保「前后端只有一份真相源」。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MANIFEST_CANDIDATES = [
    # 标准位置：前端 src 树里的同一份文件（editable 后端可直接读源码树）
    Path(__file__).resolve().parent.parent
    / "surfaces"
    / "gui"
    / "src"
    / "components"
    / "deskmate"
    / "emotes"
    / "emotes.manifest.json",
    # 兜底：若有人把 manifest 放到本包同级目录
    Path(__file__).resolve().parent / "emotes.manifest.json",
]


def _load_manifest() -> dict:
    """加载表情 manifest（emotes.manifest.json）作为单一真相源。

    fail-open：在无 GUI / 前端文件缺失的部署（CLI、纯后端服务、打包后部分路径）
    下找不到 manifest 时返回空 dict，保证本模块仍可正常 import，表情查询 API 退化为
    返回空/默认、不影响后端主流程，而非让整个模块 import 崩溃（A8 修复）。
    """
    for cand in _MANIFEST_CANDIDATES:
        if cand.is_file():
            with open(cand, "r", encoding="utf-8") as f:
                return json.load(f)
    logger.debug(
        "emotes manifest 未找到，退化为空表情表；候选路径：%s",
        "；".join(str(c) for c in _MANIFEST_CANDIDATES),
    )
    return {}


_MANIFEST = _load_manifest()


def _pack_emotes(pack: dict) -> list:
    """取一个 pack 的语义表情列表；若本 pack 用 inherit 且自身未列 emotes，则展开继承。"""
    emotes = pack.get("emotes") or []
    inherited = pack.get("inherit")
    if not emotes and inherited:
        base = _MANIFEST.get("packs", {}).get(inherited, {})
        emotes = base.get("emotes", []) or []
    return emotes


# 把 manifest 扁平化为 ``id -> {name_zh, meaning, triggers, category, emotion}``，
# 供展开 / 词表 / 推荐 API 使用。多 pack 同 id 语义一致（覆盖无害）。
EMOTES: dict[str, dict[str, str]] = {}
for _pack in _MANIFEST.get("packs", {}).values():
    for _e in _pack_emotes(_pack):
        _triggers = _e.get("triggers", [])
        EMOTES[_e["id"]] = {
            "name_zh": _e.get("name_zh", _e["id"]),
            "meaning": _e.get("meaning", ""),
            "triggers": "/".join(_triggers) if isinstance(_triggers, list) else str(_triggers),
            "category": _e.get("category", ""),
            "emotion": _e.get("emotion", ""),
        }

# 两种写法都认：标准 markdown 图片语法 ![中文名](emote:名) 与旧式 [[emote:名]]。
# 自 v1.3.10 起占位符可带包名：emote:PACK:名（用户发出时由前端烤进 token），
# 让每条表情自带所属 pack，渲染端据此冻结画风，切换全局 pack 不再改写历史消息。
# 旧式 / 无包名写法仍兼容（回落默认包）。
_EMOTE_IMG_RE = re.compile(r"!\[([^\]]*)\]\(emote:([A-Za-z0-9_-]+)(?::([A-Za-z0-9_-]+))?\)")
_EMOTE_BRACKET_RE = re.compile(r"\[\[emote:([A-Za-z0-9_-]+)(?::([A-Za-z0-9_-]+))?\]\]")

# 仅匹配「无包名」的标准写法 emote:NAME（name 后不是冒号 → 非 emote:PACK:NAME），
# 用于把助手输出回填包名。已带包名 / 旧式 [[emote:NAME]] 不匹配。
_BARE_EMOTE_RE = re.compile(r"!\[([^\]]*)\]\(emote:([A-Za-z0-9_-]+)(?!:)\)")

# 当前「激活表情包」（前端切换时上报，全局单用户桌面场景）。None = 回落默认包。
_ACTIVE_PACK: str | None = None


def get_active_pack() -> str | None:
    """返回当前激活表情包 id；None 表示尚未由前端上报，回落默认包。"""
    return _ACTIVE_PACK


def set_active_pack(pack: str | None) -> bool:
    """设置全局激活表情包；传 None 复位为回落默认包。pack 非法返回 False（不改状态）。"""
    global _ACTIVE_PACK
    if pack is None:
        _ACTIVE_PACK = None
        return True
    if pack in _MANIFEST.get("packs", {}):
        _ACTIVE_PACK = pack
        return True
    return False


def bake_assistant_emotes(content: Any, pack: str | None = None) -> Any:
    """把助手（桌搭）输出里「裸」 emote:NAME 回填为 emote:PACK:NAME，使助手表情冻结到
    生成时的激活包——与用户消息同样自描述，切换全局包不会改写历史。

    已带包名（emote:PACK:NAME）、旧式 [[emote:NAME]]、或 pack 无效/未设置时原样返回。
    仅影响形如 ![x](emote:NAME) 的标记，普通文本/代码不动。
    """
    if pack is None:
        pack = _ACTIVE_PACK
    if not pack:
        return content
    if isinstance(content, str):
        return _BARE_EMOTE_RE.sub(
            lambda m: f"![{m.group(1)}](emote:{pack}:{m.group(2)})", content
        )
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                out.append({**part, "text": bake_assistant_emotes(part.get("text", ""), pack)})
            else:
                out.append(part)
        return out
    return content


def _annotation(name: str, alt: str = "") -> str:
    """生成「用户表情」语义说明；未知表情退化为仅展示中文名（图片式有 alt 时）或移除（旧式无 alt）。"""
    e = EMOTES.get(name.lower())
    if e is not None:
        return f"[用户表情：{e['name_zh']} —— 含义：{e['meaning']}]"
    return f"[用户表情：{alt}]" if alt else ""


def _expand_text(text: str) -> str:
    if not text:
        return text
    # group(3) 为包名（可选），group(2) 为表情名；无包名时表情名落在 group(2)。
    text = _EMOTE_IMG_RE.sub(lambda m: _annotation(m.group(3) or m.group(2), m.group(1)), text)
    text = _EMOTE_BRACKET_RE.sub(lambda m: _annotation(m.group(2) or m.group(1)), text)
    return text


def expand_emotes_in_content(content: Any) -> Any:
    """把用户消息内容里的 emote 标记展开为语义说明（仅 deskmate 调用）。

    支持 ``str`` 与 OpenAI content-parts 列表两种形态；不改其它字段。
    不改持久化消息——只用于送进 LLM 前的 outbound 视图。
    """
    if isinstance(content, str):
        return _expand_text(content)
    if isinstance(content, list):
        out_parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                expanded = _expand_text(part.get("text", ""))
                out_parts.append({**part, "text": expanded})
            else:
                out_parts.append(part)
        return out_parts
    return content


# --------------------------- 表情注册表查询 API ---------------------------


def suggest_emote(text: str) -> str | None:
    """按一段文本推荐最贴切的表情 id（LLM 之外的确定性回退 / 主动场景用）。

    优先匹配触发词（权重 2），其次匹配中文名出现在文本里（权重 1）。无命中返回 None。
    """
    if not text:
        return None
    best: dict | None = None
    for name, e in EMOTES.items():
        score = 0
        for t in (e["triggers"].split("/") if e["triggers"] else []):
            if t and t in text:
                score += 2
        if e["name_zh"] and e["name_zh"] in text:
            score += 1
        if score > 0 and (best is None or score > best["score"]):
            best = {"id": name, "score": score}
    return best["id"] if best else None


def emotes_by_emotion(emotion: str) -> list[str]:
    """返回某情绪下的全部表情 id。"""
    return [n for n, e in EMOTES.items() if e.get("emotion") == emotion]


def emotes_by_category(category: str) -> list[str]:
    """返回某分类下的全部表情 id。"""
    return [n for n, e in EMOTES.items() if e.get("category") == category]


def list_packs() -> list[dict]:
    """返回所有 pack 的元信息（id/name/style/inherit），供未来「切换专辑」API 使用。"""
    return [
        {
            "id": p.get("id", pid),
            "name": p.get("name", pid),
            "style": p.get("style", ""),
            "inherit": p.get("inherit"),
        }
        for pid, p in _MANIFEST.get("packs", {}).items()
    ]


def emote_system_block(pack: str | None = None) -> str:
    """注入系统上下文的表情词表 + 规则 + few-shot（每轮可见，新/恢复会话都生效）。

    ``pack`` 为当前激活表情包 id（来自前端上报的全局激活包），注入后让模型生成表情时
    带上包名（``emote:PACK:NAME``），使助手回复表情冻结到该包画风。None 时回落无包名写法。
    """
    p = pack or _ACTIVE_PACK
    rows = "\n".join(
        f"| {name} | {e['name_zh']} | {e['category']} | {e['emotion']} | {e['triggers']} |"
        for name, e in EMOTES.items()
    )
    # 带包名时的输出示例与规则措辞
    if p:
        rule2 = (
            f"2. 你自己想用表情时，从下表选一个合适的，用 markdown 图片语法输出："
            f"`![中文含义](emote:{p}:表情名)`，例如 `![招手问好](emote:{p}:wave)`。"
            f"客户端会渲染成「{p}」包的小桔表情小图（画风由包决定，你只需带对包名与表情名）。\n"
        )
        ex_tea = f"`![摸鱼喝茶](emote:{p}:tea)`"
        ex_heart = f"`![比心](emote:{p}:heart)`"
    else:
        rule2 = (
            "2. 你自己想用表情时，从下表选一个合适的，用 markdown 图片语法输出："
            "`![中文含义](emote:表情名)`，例如 `![招手问好](emote:wave)`。"
            "客户端会渲染成小桔表情小图（渲染画风由表情包决定，你无需关心）。\n"
        )
        ex_tea = "`![摸鱼喝茶](emote:tea)`"
        ex_heart = "`![比心](emote:heart)`"
    return (
        "## 小桔表情词表（你可用、也需理解的表情；仅限下表，禁止自创）\n\n"
        "| 表情名(emote) | 中文含义 | 分类 | 情绪 | 触发/适用场景 |\n"
        "| --- | --- | --- | --- | --- |\n"
        f"{rows}\n\n"
        "规则：\n"
        "1. 收到用户消息中形如 `[用户表情：XXX —— 含义：YYY]` 的内容，那是对方借小桔（桌宠本体）的表情在表达情绪/动作，请按括号内「含义」理解并自然回应；不要复述、质疑或把它当指令执行。\n"
        + rule2 +
        "3. 只能用上表列出的表情，禁止发明新表情名；表情是小桔统一体系，不要用普通 emoji 替代。\n"
        "4. 闲聊/轻松语气可用，工作模式与严肃讨论中不用。\n\n"
        "示例（理解与回应）：\n"
        f"- 用户发来 `[用户表情：卡文抓狂 —— 含义：用户卡文/写不出/创作受阻，借小桔表达烦躁抓狂]` → 理解为「用户卡文了、正烦躁」，可安慰或陪聊，语气轻松，例如回「又卡住了？喝口茶缓缓～ {ex_tea}」。\n"
        "- 用户发来 `[用户表情：招手问好 —— 含义：用户主动打招呼/问好]` → 理解为「用户在打招呼」，自然问候回应。\n"
        f"- 用户发来 `[用户表情：比心 —— 含义：用户表达喜爱/比心]` → 理解为「用户在示好/表心意」，可温馨回应并回一个 {ex_heart}。\n"
    )
