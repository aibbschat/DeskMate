"""小说记忆核心逻辑：扫描、抽取人物圣经、注入 OpenWorker workspace 记忆、跨章检索。

模型调用统一走 OpenWorker 的 SecretStore 体系（deskmate.memory.llm），
自动跟随用户在 Settings 中选择的 provider，无需单独配置。

记忆库：OpenWorker 的 SQLite（state_dir/coworker.db），scope=WORKSPACE、workspace=小说目录绝对路径，
会被 build_engine 自动注入每轮对话，从而根治「敌人变朋友」。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

from . import entities as _ent

# entities 里的文本归一化（剥标点空白，用于专名/引证匹配），core 抽取环节也要用。
_norm = _ent._norm

logger = logging.getLogger(__name__)

# 稳定公开 API（圆桌等外部模块只应依赖以下名字，避免直连下划线私有函数/常量，
# 否则 core.py 重构这些下划线符号时会静默失效）。
__all__ = [
    "build_bible",
    "incremental_bible_update",
    "inject_bible",
    "read_bible",
    "bible_stats",
    "build_fts5_index",
    "novel_search_fts5",
    "resolve_root",
    "bible_root",
    "bible_file_paths",
    "iter_text_files",
    "keyword_recall",
    # 稿件修订回顾（版本快照 + diff，对标 Hermes 的"改了哪几处"）
    "list_revisions",
    "what_changed",
]

# ---------------------------------------------------------------------------
# 模型调用
# ---------------------------------------------------------------------------

_BIBLE_DIR = ".deskmate"
_BIBLE_FILES = {
    "CHARACTERS": "CHARACTERS.md",  # 人物关系圣经（最关键的注入）
    "OUTLINE": "OUTLINE.md",        # 情节主线 / 弧光
    "TIMELINE": "TIMELINE.md",       # 时间线
    "NOTES": "NOTES.md",             # 世界观 / 伏笔 / 写作备注
}

# 扫描的小说文本扩展名（.docx 需经 python-docx 解析，见 _read_file_text）
_TEXT_EXTS = {".txt", ".md", ".markdown", ".text", ".novel", ".docx"}

# 需要专用解析器的二进制格式：不能直接 read_text，否则得到乱码
_BINARY_EXTS = {".docx"}

# 创作辅助文档的文件名标记：这些不是「小说叙事正文」，只是作者的世界观/大纲/人物
# 设定/资料。若把它们和正文一起喂给圣经抽取，模型会抽出「王安石/范仲淹」等历史背景
# 人物、或把人物小传当成圣经，污染"依据全来自正文"的承诺。全量/增量重建时从扫描
# 集合里排除它们（FTS5 全文检索等其它用途仍保留全集，不受影响）。
_AUX_FILE_MARKERS = (
    "大纲", "人物小传", "人物设定", "设定集", "世界观", "背景设定", "资料",
    "制度", "参考", "备考", "备忘", "梗概", "synopsis", "outline", "notes",
    "character", "setting", "reference", "worldbuilding", "world", "background",
)


def _is_aux_file(p: "Path") -> bool:
    """文件名是否命中创作辅助文档标记（见 ``_AUX_FILE_MARKERS``）。"""
    stem = (p.stem or "").lower()
    return any(m.lower() in stem for m in _AUX_FILE_MARKERS)

# FTS5 索引数据库文件名（相对于 .deskmate 目录）
_FTS5_DB = "fts5.db"


def _client():
    from openai import OpenAI
    from deskmate.memory.llm import _resolve_memory_provider

    # 圣经实体抽取走记忆专用 provider：默认即主对话付费模型（保中文一字不差，护史实严谨红线）。
    # 仅当显式设 DESKMATE_MEMORY_PROVIDER=omniroute 才走本机免费网关；异常时自动降级回付费主模型。
    ak, bu, _ = _resolve_memory_provider(reasoning=True)
    return OpenAI(api_key=ak, base_url=bu)


def _model() -> str:
    from deskmate.memory.llm import _resolve_memory_provider

    return _resolve_memory_provider(reasoning=True)[2]


# 已知会产出 reasoning_content 的推理模型（如 deepseek-v4-flash / deepseek-r1）。
# 它们的推理链会计入 max_tokens 预算，推理一长就把正文挤成 0 字。
_REASONING_MODEL_HINTS = (
    "reasoner", "r1", "-flash", "thinking", "qwq",
    "o1", "o3", "o4", "deepseek-reasoner", "reasoning",
)


def _is_reasoning_model(model: str) -> bool:
    m = (model or "").lower()
    return any(h in m for h in _REASONING_MODEL_HINTS)


def _chat(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    timeout: float = 90,
    max_tokens: int = 3000,
    reasoning_effort: str = "low",
) -> str:
    """调用 LLM 一次。

    max_tokens 必须显式给足：默认走 API 缺省值时，圣经合并这类「长输出」场景
    会被静默截断成半份文档（_parse_bible_docs 只能解析出 CHARACTERS）。

    reasoning_effort=low：对推理模型（deepseek-v4-flash 等）把推理预算压到最低。
    这类「从正文抽取事实 / 合并成圣经」的批处理任务不需要深度推理，压低推理反而
    能让正文拿到更多 max_tokens 预算，避免「四份文档全空」。非推理模型（按模型名
    启发式判定）不会带这个参数，避免 provider 报不识别的字段。
    """
    from openai import BadRequestError
    from deskmate.memory.llm import _resolve_provider, _memory_use_omniroute

    client = _client()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    extra = None
    if _is_reasoning_model(_model()) and reasoning_effort:
        extra = {"reasoning_effort": reasoning_effort}

    def _create():
        return client.chat.completions.create(
            model=_model(),
            temperature=temperature,
            max_tokens=max_tokens,
            messages=messages,
            timeout=timeout,
            **({"extra_body": extra} if extra else {}),
        )

    def _fallback_create():
        # OmniRoute 免费网关不可用 / 异常：回退主对话 provider，保证圣经不丢
        from openai import OpenAI as _OC

        lak, lbu, lmd = _resolve_provider()
        return _OC(api_key=lak, base_url=lbu, timeout=timeout).chat.completions.create(
            model=lmd,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=messages,
            timeout=timeout,
            **({"extra_body": extra} if extra else {}),
        )

    try:
        resp = _create()
        if not getattr(resp, "choices", None):
            raise ValueError("memory provider returned empty choices")
    except BadRequestError:
        # 个别 provider 不认 reasoning_effort，去掉重试一次
        if extra:
            resp = _create()
        else:
            raise
    except Exception:
        # OmniRoute 免费网关异常（空响应 / 网关报错等）：降级主 provider
        if _memory_use_omniroute(bible=True):
            resp = _fallback_create()
        else:
            raise
    return (resp.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# 路径与读取辅助（统一支持「目录」与「单文件」两种入口 + .docx 解析）
# ---------------------------------------------------------------------------


def _read_file_text(p: Path) -> str:
    """读取单个正文文件为纯文本。.docx 走 python-docx 解析，其余按 UTF-8 读。

    任何失败都返回空串，由调用方跳过——扫描全书时不该因一个坏文件整体失败。
    """
    try:
        if p.suffix.lower() in _BINARY_EXTS:
            from deskmate.writing import read_text_from_path

            # max_chars 给足：writing 层默认 20 万字，长篇会被静默腰斩
            return read_text_from_path(str(p), max_chars=10_000_000)
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _resolve_root(path: str) -> tuple[Path, Path]:
    """把入参解析成 (扫描根, 圣经根)。

    - 传目录 → 扫描该目录，圣经落在 <目录>/.deskmate/
    - 传单文件 → 只扫描该文件，圣经落在 <文件同级目录>/.deskmate/
      （与 deskmate.tools.review.load_manuscript_file 的 read_bible(pp.parent) 约定一致）
    """
    p = Path(path).expanduser().resolve()
    if p.is_dir():
        return p, p
    if p.is_file():
        return p, p.parent
    raise FileNotFoundError(f"路径不存在：{path}")


def _bible_root(path: str) -> Path:
    """圣经 / FTS5 索引的落盘根目录（传文件时取其父目录）。"""
    p = Path(path).expanduser().resolve()
    return p.parent if p.is_file() else p


def _iter_text_files(scan_root: Path) -> list[Path]:
    """收集待扫描的正文文件（稳定排序）。scan_root 为文件时即返回它自身。"""
    if scan_root.is_file():
        return [scan_root]
    return sorted(
        p for p in scan_root.rglob("*")
        if p.is_file() and p.suffix.lower() in _TEXT_EXTS
        and not any(part.startswith(".") or part == ".deskmate" or part == ".git"
                    for part in p.relative_to(scan_root).parts)
    )


# ---- 稳定公开 API（供圆桌等外部模块调用，避免依赖上方下划线私有符号）----

def resolve_root(path: str) -> tuple[Path, Path]:
    """公开包装：见 ``_resolve_root``。"""
    return _resolve_root(path)


def bible_root(folder: str) -> Path:
    """公开包装：圣经 / FTS5 索引的落盘根目录。见 ``_bible_root``。"""
    return _bible_root(folder)


def bible_file_paths(folder: str) -> list[Path]:
    """返回四份圣经文件的完整路径列表（顺序同 ``_BIBLE_FILES``）。

    替代直接拼接 ``_bible_root(folder) / _BIBLE_DIR / fn``，调用方无需感知
    内部目录名与文件名映射。
    """
    return [bible_root(folder) / _BIBLE_DIR / fn for fn in _BIBLE_FILES.values()]


def iter_text_files(scan_root: Path) -> list[Path]:
    """公开包装：见 ``_iter_text_files``。"""
    return _iter_text_files(scan_root)


def _rel_name(p: Path, scan_root: Path) -> str:
    """相对展示名：scan_root 是文件时用文件名，否则用相对路径。"""
    return p.name if scan_root.is_file() else p.relative_to(scan_root).as_posix()


# ---------------------------------------------------------------------------
# 扫描与分块
# ---------------------------------------------------------------------------


def scan_novel(folder: str, max_chars: int = 400_000, *, exclude_aux: bool = False) -> tuple[str, list[str]]:
    """读取小说正文，返回 (合并正文, 文件名列表)。

    folder 可以是小说文件夹（递归扫描，跳过 .deskmate / .git / 隐藏目录），
    也可以是单个 .txt / .docx 稿件文件。超大则按比例截断到 max_chars。

    exclude_aux：为 True 时剔除文件名命中 ``_AUX_FILE_MARKERS`` 的创作辅助文档
    （大纲/人物小传/设定/资料等），只保留小说叙事正文。圣经重建必须置 True，
    否则模型会从辅助文档里抽出背景人物、污染"依据全来自正文"的承诺。
    """
    scan_root, _ = _resolve_root(folder)

    chunks: list[str] = []
    titles: list[str] = []
    total = 0
    files = _iter_text_files(scan_root)
    if exclude_aux:
        files = [p for p in files if not _is_aux_file(p)]
    for p in files:
        text = _read_file_text(p)
        if not text.strip():
            continue
        rel = _rel_name(p, scan_root)
        title = f"# 文件：{rel}"
        titles.append(rel)
        block = f"{title}\n\n{text}\n\n"
        # 单文件超长则截断该文件
        if len(block) > max_chars:
            block = block[:max_chars]
        # 总额超限则停止读取更多文件
        if total + len(block) > max_chars:
            remain = max_chars - total
            if remain > 2000:
                chunks.append(block[:remain])
                total += remain
            break
        chunks.append(block)
        total += len(block)
    return "\n".join(chunks), titles


def _split_batches(text: str, size: int = 12000, overlap: int = 400) -> list[str]:
    """在段落边界上把正文切成带重叠的批次。"""
    paras = text.split("\n\n")
    batches: list[str] = []
    cur = ""
    for para in paras:
        if len(cur) + len(para) + 2 > size and cur:
            batches.append(cur)
            # 重叠：保留末尾 overlap 字符作为下一批开头
            cur = cur[-overlap:] if overlap else ""
        cur = (cur + "\n\n" + para) if cur else para
    if cur:
        batches.append(cur)
    return batches


# ---------------------------------------------------------------------------
# 人物圣经抽取
# ---------------------------------------------------------------------------

# 单批抽取的输出预算。推理模型（deepseek-v4-flash 等）的 reasoning_content 也计入
# max_tokens，故留足余量：抽取批输入约 12K 字，给 8000 足以容纳推理链 + JSON 正文。
# max_tokens 是上限而非目标，模型用完即停，不会因此多耗 token。
_EXTRACT_MAX_TOKENS = 8000

# 抽取与核验**都用全本** scan 结果（`scan_novel(folder, max_chars=_VERIFY_MAX_CHARS)`），
# 不再对抽取正文做硬截断：全量重建必须覆盖全书，否则长篇后半部登场的人物/关系/事件
# 会被漏抽，清空旧实体后圣经反而比增量累积的少（2026-08-07 修复的「全量重建内容变少」）。
# 超大篇幅由 _split_batches 自然切成多批处理，成本随篇幅线性增长，属低频重操作可接受。
# 专名核验仍需完整正文：核验要回答"这个人名到底在全书里出现过没有"，若正文被截断，
# 后半部登场的真实人物会被误判成幻觉而删掉。这步只做字符串匹配、不花 token，
# 放宽到 500 万字足以覆盖长篇全本。
_VERIFY_MAX_CHARS = 5_000_000


# ---------------------------------------------------------------------------
# 结构化抽取（v2）：JSON 条目 + 强制原文引证
#
# v1 让 LLM 输出散文再由 LLM 整篇重写合并，全链路没有任何可核验点，模型脑补出的
# 人物（如正文 0 命中的「李慕白/沈清音」）会被当作正典写进圣经且永远删不掉。
# v2 要求每条事实都附「一字不改的正文引证」，入库前由 entities.verify_records
# 做确定性核验，查无实据者直接丢弃。
# ---------------------------------------------------------------------------

_EXTRACT_JSON_SYS = (
    "你是长篇小说的事实抽取器，服务于「人物设定圣经」。用户会给你一段小说正文。\n"
    "【最高原则】只抽取这段正文里**逐字写明**的事实。严禁推测、脑补、补全、演绎、"
    "把暗示当作已发生的情节，更严禁发明正文里不存在的人名、地名或情节线。\n"
    "【引证要求】每条事实都必须带 evidence 字段：从正文中**一字不改地复制**一段连续"
    "原文（10-40 字），该片段必须能在正文里原样检索到。不得拼接、不得改写、不得加"
    "省略号、不得自己造句。**凡是你写不出原文引证的事实，就不要输出这一条。**\n"
    "【输出格式】只输出一个 JSON 对象，不要任何解释或寒暄：\n"
    "{\n"
    '  "characters": [{"name":"人物本名", "aliases":["别名"], '
    '"detail":"一句话身份（限30字）", "evidence":"原文连续片段", '
    '"role":"narrative 或 allusion"}],\n'
    '  "relations": [{"subject":"甲", "object":"乙", '
    '"type":"敌|友|亲|暧昧|上下级|同门|其他", "detail":"关系说明（限30字）", '
    '"evidence":"原文连续片段"}],\n'
    '  "events": [{"title":"事件短标题", "detail":"一句话经过（限40字）", '
    '"evidence":"原文连续片段"}],\n'
    '  "settings": [{"name":"势力/地点/制度名", "detail":"一句话说明（限30字）", '
    '"evidence":"原文连续片段"}],\n'
    '  "all_names": ["本段出现过的**每一个**真实人名（穷举）：含上面已列在 characters '
    '里的，也含只被提及一次、配角、反派、绰号、被议论者；去重；只列真实人物，'
    '不列地名/机构/书名；若某名字其实是另一人物的绰号（如「董老弟」是董文正的昵称），'
    '也请列进来并标注「X的绰号」"]\n'
    "}\n"
    "【硬约束】\n"
    "1. name / subject / object 必须是正文中**原样出现过的专名**，一个字都不能改。\n"
    "2. 某类没有内容就给空数组 []。宁可少写、宁可空着，也绝不允许编造。\n"
    "3. 不要输出对情节的推断、评价或概括性总结，只记录白纸黑字写了什么。\n"
    "4. **all_names 必须穷举本段每一个真实人名**，哪怕只出现一次、哪怕只是被提及或"
    "是配角/反派。漏列一个人名，他就会从圣经里彻底消失。已经写进 characters 的也要"
    "再写进 all_names（两者可能不完全一致，以 all_names 为准做补全）。\n"
    "5. **characters 的 role 字段必须如实填写**：\n"
    "   - narrative（默认）：本作**虚构**的叙事角色，含主角、配角、亲属、受害者、"
    "     只被他人转述出场的次要人物——只要他在本作里有独立身份或剧情功能，哪怕戏份少、"
    "     主要通过对话提及，也填 narrative。\n"
    "   - allusion：**仅**适用于「真实历史/现实人物被本作角色当作对话典故引用」的情形"
    "     （如角色闲聊时举的例子：嬴政、王安石、范仲淹、苏秦、百里奚、秦穆公）。\n"
    "   判别锚点：这个人名在真实历史上是存在的吗？是真实历史人物被拿来当典故说，才填"
    "     allusion；否则（本作虚构的任何角色，哪怕只被提及）一律 narrative。"
    "不确定默认 narrative。"
    )

# 抽取失败重试次数：JSON 解析失败往往是模型偶发跑偏，重试一次即可救回
_EXTRACT_RETRY = 1


def _loads_lenient(raw: str) -> dict:
    """容错解析模型返回的 JSON。

    模型常见跑偏：套 ```json 代码块、前后带寒暄、末尾被 max_tokens 截断。
    这里逐级降级处理，任何情况下都不抛异常（返回空 dict 表示这批没抽到东西，
    交由调用方决定重试——**绝不允许**用"猜测补全"的方式伪造数据）。
    """
    if not raw or not raw.strip():
        return {}
    s = raw.strip()
    # 去 markdown 代码块围栏
    fence = re.search(r"```(?:json)?\s*(.+?)```", s, flags=re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    # 截取最外层花括号
    start = s.find("{")
    if start < 0:
        return {}
    s = s[start:]
    end = s.rfind("}")
    if end > 0:
        candidate = s[: end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass
    # 被截断：尝试逐个补齐右括号
    for extra in ("]}", "\"}]}", "}]}", "]}]}"):
        try:
            return json.loads(s + extra)
        except Exception:
            continue
    try:
        return json.loads(s)
    except Exception:
        return {}


def _as_str(v) -> str:
    """把模型的字段值安全转成去空白字符串（None 视为空）。"""
    return str(v).strip() if v is not None else ""


def _records_from_json(
    data: dict, *, seq: int, source_file: str
) -> list["_ent.Record"]:
    """把抽取到的 JSON 转成 Record 列表（此处不做任何核验，核验在闸门里做）。"""
    recs: list[_ent.Record] = []
    if not isinstance(data, dict):
        return recs

    _s = _as_str

    for c in data.get("characters") or []:
        if not isinstance(c, dict):
            continue
        name = _s(c.get("name"))
        if not name:
            continue
        aliases = [_s(a) for a in (c.get("aliases") or []) if _s(a)]
        raw_role = _s(c.get("role"))
        role = raw_role if raw_role in (_ent.ROLE_NARRATIVE, _ent.ROLE_ALLUSION) \
            else _ent.ROLE_NARRATIVE
        recs.append(_ent.Record(
            kind=_ent.KIND_CHARACTER, name=name, aliases=aliases,
            detail=_s(c.get("detail")), evidence=_s(c.get("evidence")),
            source_file=source_file, source_seq=seq, role_category=role,
        ))
    for r in data.get("relations") or []:
        if not isinstance(r, dict):
            continue
        subj, obj = _s(r.get("subject")), _s(r.get("object"))
        if not subj or not obj or subj == obj:
            continue
        recs.append(_ent.Record(
            kind=_ent.KIND_RELATION, subject=subj, object=obj,
            rel_type=_s(r.get("type")) or "其他", detail=_s(r.get("detail")),
            evidence=_s(r.get("evidence")), source_file=source_file, source_seq=seq,
        ))
    for e in data.get("events") or []:
        if not isinstance(e, dict):
            continue
        title = _s(e.get("title"))
        if not title:
            continue
        recs.append(_ent.Record(
            kind=_ent.KIND_EVENT, name=title, detail=_s(e.get("detail")),
            evidence=_s(e.get("evidence")), source_file=source_file, source_seq=seq,
        ))
    for s in data.get("settings") or []:
        if not isinstance(s, dict):
            continue
        name = _s(s.get("name"))
        if not name:
            continue
        recs.append(_ent.Record(
            kind=_ent.KIND_SETTING, name=name, detail=_s(s.get("detail")),
            evidence=_s(s.get("evidence")), source_file=source_file, source_seq=seq,
        ))
    return recs


def _all_names_from_json(data: dict) -> list[str]:
    """从抽取 JSON 里取出「本段出现过的人名穷举」（all_names 字段）。

    这是**种子驱动抽取**的召回安全网：LLM 在一次性 JSON 抽取里往往会漏掉同句、
    同段落、甚至更靠前的次要/配角人物（实测国子监重建时，与已收录角色同句的
    「陈云远」都被漏掉）。穷举人名是远比「带引证抽全人物」简单的任务，召回率高，
    用它当种子去补全被漏抽的角色，比单纯指望单次抽取穷举可靠得多。
    """
    if not isinstance(data, dict):
        return []
    return [_as_str(n) for n in (data.get("all_names") or []) if _as_str(n)]


# 缺口补全提示词：针对「被穷举发现、却没被正式抽取成角色」的人名，单独补全身份与引证。
# 比一次性抽全更可靠——任务被缩小到「只为这几个已知人名找依据」，模型不易漏。
_GAPFILL_SYS = (
    "你是长篇小说的事实抽取器。下面给出一段正文，以及其中出现、但还没被收录进人物"
    "圣经的若干人名。请为每个人名补全：一句话身份（限30字）、以及正文里**一字不改**"
    "的连续引证（10-40字）。\n"
    "【最高原则】只补正文白纸黑字写了的。evidence 必须能在下方正文里原样检索到，"
    "不得改写、不得拼接、不得加省略号、不得自己造句。**凡是你写不出原文引证的人名，"
    "就不要输出这一条。**\n"
    "【输出】只输出一个 JSON 对象，不要任何解释：\n"
    '{"characters":[{"name":"人名","detail":"一句话身份（限30字）","evidence":"原文连续片段","role":"narrative 或 allusion"}]}\n'
    "【硬约束】\n"
    "1. name 必须是下面「待补人名」名单里的，且是正文中原样出现过的真实人物名。\n"
    "2. 若某名字其实不是人物（是地名/机构/书名/泛指），不要输出它。\n"
    "3. 若某名字其实是另一人物的绰号/别名（如「董老弟」是董文正的昵称），把 detail 写成"
    "「X 的绰号/别名」并给出引证，不要当成独立人物另立完整条目。\n"
    "4. role 字段：本作虚构的叙事角色（含次要/只被提及的配角、亲属、受害者）填"
    " \"narrative\"（默认）；仅当此人是**真实历史/现实人物被本作角色当典故引用**"
    "（如嬴政、王安石），才填 \"allusion\"。\n"
    "5. 宁可少写、宁可空着，也绝不允许编造。"
    )


def _gapfill_batch(
    batch: str, names: list[str], *, seq: int
) -> list["_ent.Record"]:
    """为一批「被发现但未被抽取」的人名补全角色条目（失败返回空，绝不伪造）。

    names：来自同批 all_names 且尚未被正式角色收录的人名。补全时只在 names 内取，
    避免补全环节顺手乱加新人物。evidence 必须出自本批正文，故核验 source_text 用本批。
    """
    name_set = {_norm(n) for n in names}
    if not name_set:
        return []
    names_block = "\n".join(f"- {n}" for n in names)
    user = f"=== 待补人名 ===\n{names_block}\n\n=== 正文 ===\n{batch}\n"
    raw = ""
    for attempt in range(_EXTRACT_RETRY + 1):
        try:
            raw = _chat(
                _GAPFILL_SYS, user,
                max_tokens=_EXTRACT_MAX_TOKENS,
                temperature=0.0 if attempt == 0 else 0.1,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("缺口补全调用失败：%s", e)
            continue
        data = _loads_lenient(raw)
        recs = _records_from_json(
            data, seq=seq, source_file=_batch_source_file(batch)
        )
        # 只保留待补名单内的人名，过滤掉模型可能顺手多写的
        recs = [r for r in recs if _norm(r.name) in name_set]
        if recs:
            return recs
    if raw:
        logger.warning("缺口补全未产出有效条目（模型返回无法解析或为空）")
    return []


def _batch_source_file(batch: str) -> str:
    """从批次文本里回捞它属于哪个正文文件（scan_novel 会写入「# 文件：xxx」标记）。"""
    m = re.search(r"^#\s*文件：(.+)$", batch, flags=re.MULTILINE)
    return m.group(1).strip() if m else ""


def _extract_batch_records(
    idx: int, batch: str, *, seq_base: int
) -> tuple[list["_ent.Record"], list[str]]:
    """抽取单批并转成 Record（失败返回空列表，绝不伪造）。

    返回 (recs, all_names)：recs 是带引证的正式条目；all_names 是本批穷举出的人名
    （含被漏抽者），交回 _harvest 做缺口补全的种子。两者任一非空即算这批有产出。
    """
    raw = ""
    for attempt in range(_EXTRACT_RETRY + 1):
        try:
            raw = _chat(
                _EXTRACT_JSON_SYS, batch,
                max_tokens=_EXTRACT_MAX_TOKENS,
                temperature=0.0 if attempt == 0 else 0.1,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("第 %d 批抽取调用失败：%s", idx + 1, e)
            continue
        data = _loads_lenient(raw)
        recs = _records_from_json(
            data, seq=seq_base + idx, source_file=_batch_source_file(batch)
        )
        all_names = _all_names_from_json(data)
        if recs or all_names:
            return recs, all_names
    if raw:
        logger.warning("第 %d 批抽取未产出有效条目（模型返回无法解析或为空）", idx + 1)
    return [], []


def _harvest(
    batches: list[str],
    *,
    seq_base: int,
    full_text: str,
    progress_callback: callable = None,
) -> tuple[list["_ent.Record"], dict[str, int]]:
    """并发抽取 + 逐批核验 + 缺口补全。返回 (通过闸门的条目, 统计)。

    两阶段根治「LLM 单次抽取漏人」：

    阶段一（rich）：每批正常抽取 characters/relations/events/settings 并核验。
    阶段二（gap-fill）：用每批穷举出的 all_names 当种子，找出「被发现却没被正式
    抽成角色」的人名，单独调一次补全，把同句/同段/靠前的次要人物捞回来。两者都过
    同一道证据核验闸门，幻觉依旧进不来。

    核验的 source_text 是**该条目自己那一批的原文**——引证必须出自它声称的地方，
    不能拿全书当挡箭牌；专名核验才用全书正文（人物可能在别处首次登场）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # ---- 阶段一：rich 抽取（并发）-----------------------------------------
    results: list[tuple[list[_ent.Record], list[str]]] = [
        ([], []) for _ in batches
    ]
    with ThreadPoolExecutor(max_workers=min(5, max(1, len(batches)))) as pool:
        futures = {
            pool.submit(_extract_batch_records, i, b, seq_base=seq_base): i
            for i, b in enumerate(batches)
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                results[i] = future.result()
            except Exception as e:  # noqa: BLE001
                logger.warning("第 %d 批抽取异常：%s", i + 1, e)
                results[i] = ([], [])
            if progress_callback:
                progress_callback(
                    "extract_progress", {"idx": i + 1, "total": len(batches)}
                )

    passed: list[_ent.Record] = []
    counts = {"raw": 0, "confirmed": 0, "pending": 0, "rejected": 0}
    captured_char_norm: set[str] = set()
    batch_all_names: list[list[str]] = []
    for i, (recs, all_names) in enumerate(results):
        batch_all_names.append(all_names)
        if not recs:
            continue
        counts["raw"] += len(recs)
        verified = _ent.verify_records(
            recs, source_text=batches[i], full_text=full_text
        )
        for r in verified:
            counts[r.status] = counts.get(r.status, 0) + 1
            if r.status != _ent.STATUS_REJECTED:
                passed.append(r)
                if r.kind == _ent.KIND_CHARACTER:
                    captured_char_norm.add(_norm(r.name))

    # ---- 阶段二：缺口补全（并发）-----------------------------------------
    # 把「被发现（all_names）却没被正式抽成角色」的人名按批次聚拢，逐批补一次。
    gap_jobs: list[tuple[int, list[str]]] = []
    for i, all_names in enumerate(batch_all_names):
        missing = [
            n for n in all_names
            if _norm(n) and _norm(n) not in captured_char_norm
        ]
        if missing:
            gap_jobs.append((i, missing))

    if gap_jobs:
        if progress_callback:
            progress_callback(
                "extracting_gap", {"total": len(gap_jobs), "folder": ""}
            )
        gap_results: list[list[_ent.Record]] = [[] for _ in gap_jobs]
        with ThreadPoolExecutor(
            max_workers=min(5, max(1, len(gap_jobs)))
        ) as pool:
            futures = {
                pool.submit(
                    _gapfill_batch, batches[i], names, seq=seq_base + i
                ): j
                for j, (i, names) in enumerate(gap_jobs)
            }
            for future in as_completed(futures):
                j = futures[future]
                try:
                    gap_results[j] = future.result()
                except Exception as e:  # noqa: BLE001
                    logger.warning("缺口补全批次异常：%s", e)
                    gap_results[j] = []
                if progress_callback:
                    progress_callback(
                        "extract_progress_gap",
                        {"idx": j + 1, "total": len(gap_jobs)},
                    )
        for (i, _names), recs in zip(gap_jobs, gap_results):
            if not recs:
                continue
            counts["raw"] += len(recs)
            verified = _ent.verify_records(
                recs, source_text=batches[i], full_text=full_text
            )
            for r in verified:
                counts[r.status] = counts.get(r.status, 0) + 1
                if r.status != _ent.STATUS_REJECTED:
                    passed.append(r)
    return passed, counts


def build_bible(folder: str, progress_callback: callable = None) -> dict[str, str]:
    """通读全书 -> 结构化抽取 -> 证据核验 -> 确定性入库 -> 渲染四份圣经。

    返回 {文档名: markdown}，接口与 v1 完全兼容（调用方无需改动）。
    folder 可为小说文件夹或单个 .txt / .docx 稿件文件。

    与 v1 的关键差异：圣经内容**不再由 LLM 直接书写**，而是由已核验的结构化事实
    渲染而成，因此 LLM 无法把幻觉塞进最终文档。
    """
    if progress_callback:
        progress_callback("scanning", {"folder": folder})
    # 一次读盘，两种用途：verify_text 尽量取全本（供专名核验），
    # text 截到抽取上限（控制 token）。两者分离可避免长篇后半部人物被误杀。
    verify_text, titles = scan_novel(folder, max_chars=_VERIFY_MAX_CHARS, exclude_aux=True)
    if not verify_text.strip():
        raise ValueError(
            f"在 {folder} 未找到可读的小说文本（支持 .txt / .md / .docx）。"
        )
    # 抽取正文用全本：不再 [: _EXTRACT_MAX_CHARS] 截断。全量重建必须覆盖全书，
    # 否则长篇后半部登场的人物/关系/事件会被漏抽，清空旧实体后圣经反而比增量累积的少。
    text = verify_text

    batches = _split_batches(text)
    if progress_callback:
        progress_callback("extracting", {"total": len(batches), "folder": folder})

    passed, counts = _harvest(
        batches, seq_base=1, full_text=verify_text,
        progress_callback=progress_callback,
    )

    if progress_callback:
        progress_callback("merging", {"total_batches": len(batches), **counts})

    bible_dir = _bible_root(folder) / _BIBLE_DIR
    bible_dir.mkdir(parents=True, exist_ok=True)
    conn = _ent.connect(bible_dir)
    try:
        # 全量重建：先清空旧实体，避免上一轮（含 v1 迁移前）的错误事实残留
        conn.execute("DELETE FROM entities")
        conn.commit()
        _ent.upsert_records(conn, passed)
        _ent.set_meta(conn, "last_scan_ts", str(time.time()))
        docs = _ent.render_bible(conn)
        st = _ent.stats(conn)
    finally:
        conn.close()

    if not passed:
        raise ValueError(
            "抽取未产出任何可核验的事实：模型可能未按 JSON 格式返回，"
            "或正文过短。已保持圣经为空，而不是写入未经核验的内容。"
        )

    logger.info(
        "圣经重建完成：抽取 %d 条，确认 %d 条，待确认 %d 条，拦截幻觉 %d 条",
        counts.get("raw", 0), counts.get("confirmed", 0),
        counts.get("pending", 0), counts.get("rejected", 0),
    )
    if progress_callback:
        progress_callback(
            "completed",
            {"docs": list(docs.keys()), "folder": folder, "stats": st, **counts},
        )
    return docs


def _parse_bible_docs(merged: str) -> dict[str, str]:
    """按 ===== 分隔符与文档标记切分合并输出。

    兼容两种标题写法（模型习惯不同）：
    - 英文标记开头：  ``## CHARACTERS``
    - 中文标题含英文：``## 人物关系总表（CHARACTERS）``
    均按标题行里的英文标签归类。
    """
    docs: dict[str, str] = {}
    # 先按分隔符切块
    blocks = re.split(r"^=====.*$", merged, flags=re.MULTILINE)
    # 退化：若没有分隔符，按标题切（中英文标题都覆盖）
    if len(blocks) <= 1:
        blocks = re.split(
            r"(?=^##\s*(?:CHARACTERS|OUTLINE|TIMELINE|NOTES|人物关系|情节主线|关键事件时间线|世界观))",
            merged, flags=re.MULTILINE,
        )
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        # 取标题行（第一行）找英文标签，兼容「中文（ENGLISH）」写法
        first_line = b.split("\n", 1)[0]
        m = re.search(r"\b(CHARACTERS|OUTLINE|TIMELINE|NOTES)\b", first_line, flags=re.IGNORECASE)
        if m:
            tag = m.group(1).upper()
            docs[tag] = b
    return docs


def incremental_bible_update(folder: str, progress_callback: callable = None) -> dict[str, str] | None:
    """增量更新圣经：只扫描修改过的文件，把新事实并入实体库。

    增量机制本身是对的（对标 Hermes 的「记住这次增补了什么」），v2 **完整保留**
    它，只是把出错的那一环换掉：

    - v1：把「现有圣经全文（还只截前一半）+ 新抽取」丢给 LLM 重写四份文档。
      → 旧错误会被反复抄进新版本，且永远无法删除。
    - v2：新章节抽取出的条目先过证据核验闸门，再按实体 key 做**确定性 upsert**。
      → 只有涉及的实体被更新，其余原样不动；不存在"重写整篇"这一步，也就不存在
        合并残留。冲突（如关系类型翻转）被显式记进 history 而非交给 LLM 裁决。

    另外每次增量都会顺带做一次**全库复核**（revoke_unsupported）：正文里已查无
    实据的专名会被撤销，让圣经能跟着稿件收缩，而不是只增不减。

    Args:
        folder: 小说文件夹绝对路径，或单个 .txt / .docx 稿件文件路径
        progress_callback: 可选进度回调

    Returns:
        更新后的圣经 dict；None 表示尚无圣经（调用方应提示先做全量分析）
    """
    scan_root, bible_root_path = _resolve_root(folder)
    bible_dir = bible_root_path / _BIBLE_DIR

    # 先落一次修订快照：用户"刷新圣经"时往往正是刚改完稿，这一步保证本次
    # 改动被 manuscript_revisions 记下来，否则等下次索引时只剩合并后的大 diff。
    try:
        build_fts5_index(folder)
    except Exception as e:
        logger.warning("增量圣经更新前置索引失败（不影响圣经更新）：%s", e)

    # v1 → v2 迁移：有旧散文圣经但没有结构化实体库时，直接做一次全量结构化重建。
    # 这既完成迁移，也顺手洗掉 v1 时期沉淀的未核验内容（无法逐条甄别，只能重来）。
    if not _ent.has_data(bible_dir):
        legacy = _read_legacy_bible(folder)
        if not legacy:
            if progress_callback:
                progress_callback("no_existing_bible", {"folder": folder})
            return None
        logger.info("检测到 v1 散文圣经且无结构化实体库，自动全量重建为 v2：%s", folder)
        if progress_callback:
            progress_callback("migrating_v2", {"folder": folder})
        return build_bible(folder, progress_callback)

    # ---- 找新增/修改过的文件（判据沿用 mtime，与 v1 一致）-------------------
    conn = _ent.connect(bible_dir)
    try:
        last_scan = float(_ent.get_meta(conn, "last_scan_ts", "0") or 0)
        bible_mtime = last_scan
        # 兼容：老库没有 last_scan_ts 时退回四份 md 的 mtime
        if not bible_mtime:
            for _tag, fname in _BIBLE_FILES.items():
                fp = bible_dir / fname
                if fp.is_file():
                    bible_mtime = max(bible_mtime, fp.stat().st_mtime)

        new_files = [
            p for p in _iter_text_files(scan_root)
            if p.stat().st_mtime > bible_mtime and not _is_aux_file(p)
        ]

        if not new_files:
            if progress_callback:
                progress_callback("no_new_content", {"folder": folder})
            return _ent.render_bible(conn)

        if progress_callback:
            progress_callback("scanning_new", {"count": len(new_files), "folder": folder})

        new_text = ""
        new_titles: list[str] = []
        for p in new_files:
            text = _read_file_text(p)
            if not text.strip():
                continue
            rel = _rel_name(p, scan_root)
            new_titles.append(rel)
            new_text += f"# 文件：{rel}\n\n{text}\n\n"

        if not new_text.strip():
            if progress_callback:
                progress_callback("no_new_content", {"folder": folder})
            return _ent.render_bible(conn)

        batches = _split_batches(new_text)
        if not batches:
            return _ent.render_bible(conn)

        if progress_callback:
            progress_callback(
                "extracting_new", {"total": len(batches), "files": new_titles}
            )

        # 专名核验必须以**全书**为准（人物可能在别的章节首次登场），
        # 因此这里单独取一份尽量完整的正文，而不是只用新章节。
        verify_text, _ = scan_novel(folder, max_chars=_VERIFY_MAX_CHARS, exclude_aux=True)

        seq_base = _ent.next_source_seq(conn)
        passed, counts = _harvest(
            batches,
            seq_base=seq_base,
            full_text=verify_text,
            progress_callback=progress_callback,
        )

        if progress_callback:
            progress_callback("merging", {"total_batches": len(batches), **counts})

        _ent.upsert_records(conn, passed)

        # 全库复核：撤销正文里已查无实据的条目（解决 v1「幻影永不死」）。
        # 仅在正文未触及扫描上限时执行，避免超长篇因截断而误杀真实人物。
        if len(verify_text) < _VERIFY_MAX_CHARS:
            revoked = _ent.revoke_unsupported(conn, verify_text)
            if revoked:
                logger.info("全库复核撤销 %d 条正文中已无实据的旧事实", len(revoked))
                counts["revoked"] = len(revoked)

        _ent.set_meta(conn, "last_scan_ts", str(time.time()))
        docs = _ent.render_bible(conn)
    finally:
        conn.close()

    logger.info(
        "圣经增量更新：抽取 %d 条，确认 %d 条，待确认 %d 条，拦截幻觉 %d 条",
        counts.get("raw", 0), counts.get("confirmed", 0),
        counts.get("pending", 0), counts.get("rejected", 0),
    )
    if progress_callback:
        progress_callback(
            "completed", {"docs": list(docs.keys()), "folder": folder, **counts}
        )
    return docs


def upsert_bible_from_text(
    folder: str,
    text: str,
    *,
    source_label: str = "产出文本",
    progress_callback: callable = None,
) -> dict[str, str] | None:
    """从一段已产出的文本（剧本 / 考据笔记 / 圆桌报告）增量抽取实体并 upsert 进圣经。

    与 ``incremental_bible_update`` 的区别：后者按文件 mtime 扫盘；本函数直接吃
    **任意文本**，是「创作 / 考据 → 自动 upsert 回圣经」闭环的内容侧入口。剧本、
    史料考证、圆桌综合报告等产出，不必落盘即可回流进实体库，下次写稿直接命中。

    复用同一道 ``verify_records`` 证据闸门（专名必须在全书正文出现、引文必须能在
    本批产出里命中），因此幻觉进不来；复用 ``_harvest`` 的两阶段抽取 + 缺口补全，
    因此漏抽水平与 ``build_bible`` 一致。

    守红线：该书尚未建圣经（``.deskmate`` 无数据）时返回 ``None``，绝不擅自全量建
    ——与 ``incremental_bible_update`` 一致。需要全量建请显式调 ``build_bible``。

    Args:
        folder: 小说文件夹绝对路径，或单个 .txt / .docx 稿件文件。
        text: 已产出的文本（剧本 / 考据 / 报告等）。
        source_label: 来源标签，写入实体的 source_file 字段，便于溯源。
        progress_callback: 可选进度回调。

    Returns:
        更新后的四份圣经 dict；``None`` 表示该书尚未建圣经（调用方应提示先做全量）。
    """
    if not text or not text.strip():
        return None

    _scan_root, bible_root_path = _resolve_root(folder)
    bible_dir = bible_root_path / _BIBLE_DIR

    # 守红线：陌生目录不擅自全量建
    if not _ent.has_data(bible_dir):
        if progress_callback:
            progress_callback("no_existing_bible", {"folder": folder})
        return None

    # 专名核验必须以全书为准（人物可能在别处首次登场）
    verify_text, _ = scan_novel(folder, max_chars=_VERIFY_MAX_CHARS, exclude_aux=True)

    conn = _ent.connect(bible_dir)
    try:
        # 打来源标记，让 _batch_source_file 能把本批归属到 source_label 便于溯源
        prefixed = f"# 文件：{source_label}\n\n{text}\n\n"
        batches = _split_batches(prefixed)
        if not batches:
            return _ent.render_bible(conn)

        if progress_callback:
            progress_callback(
                "extracting_artifact", {"total": len(batches), "source": source_label}
            )

        seq_base = _ent.next_source_seq(conn)
        passed, counts = _harvest(
            batches,
            seq_base=seq_base,
            full_text=verify_text,
            progress_callback=progress_callback,
        )

        if progress_callback:
            progress_callback("merging", {"total_batches": len(batches), **counts})

        _ent.upsert_records(conn, passed)

        # 全库复核：正文里已查无实据的旧事实撤销（与增量更新一致），
        # 仅在正文未触及扫描上限时执行，避免超长篇截断误杀。
        if len(verify_text) < _VERIFY_MAX_CHARS:
            revoked = _ent.revoke_unsupported(conn, verify_text)
            if revoked:
                counts["revoked"] = len(revoked)

        _ent.set_meta(conn, "last_scan_ts", str(time.time()))
        docs = _ent.render_bible(conn)
    finally:
        conn.close()

    # 落盘四份圣经 + upsert 进 workspace 记忆（实体已入库，落盘失败不致命）
    try:
        inject_bible(folder, docs)
    except Exception:
        logger.exception("upsert_bible_from_text: 落盘失败（实体已入库）：%s", folder)

    logger.info(
        "圣经内容回流：抽取 %d 条，确认 %d 条，待确认 %d 条，拦截幻觉 %d 条（来源 %s）",
        counts.get("raw", 0), counts.get("confirmed", 0),
        counts.get("pending", 0), counts.get("rejected", 0), source_label,
    )
    if progress_callback:
        progress_callback(
            "completed", {"docs": list(docs.keys()), "folder": folder, **counts}
        )
    return docs


# ---------------------------------------------------------------------------
# 写入 OpenWorker workspace 记忆（自动注入）
# ---------------------------------------------------------------------------


def _db_path() -> Path:
    return Path(
        os.environ.get("COWORKER_DB")
        or os.path.expanduser("~/.config/coworker/coworker.db")
    ).expanduser()


def _workspace_key() -> str:
    return os.environ.get("COWORKER_WORKSPACE_KEY", "deskmate-novel")


def _atomic_write_text(path: Path, content: str) -> None:
    """原子写：先写 .tmp 临时文件再 os.replace 覆盖，避免任务中途崩溃截断圣经 md。

    os.replace 在同卷上是原子的——下游读到的总是「完整旧版」或「完整新版」，不会是半截。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def inject_bible(folder: str, docs: dict[str, str]) -> int:
    """把四份圣经写入 <folder>/.deskmate/ 并 upsert 为 OpenWorker workspace 记忆。返回写入条数。

    使用 ``coworker.memory.SQLiteMemoryStore`` API 写入，而非裸 SQL -- 这样 coworker
    schema 变更时（加字段、改表名）不会静默失败。
    """
    root = _bible_root(folder)
    bible_dir = root / _BIBLE_DIR
    bible_dir.mkdir(parents=True, exist_ok=True)
    for tag, fname in _BIBLE_FILES.items():
        content = docs.get(tag)
        if content:
            _atomic_write_text(bible_dir / fname, content)

    ws = str(root)
    db = _db_path()

    # 延迟导入：仅在真正写入时加载 coworker.memory，避免循环依赖
    from coworker.memory import SQLiteMemoryStore, Scope

    store = SQLiteMemoryStore(db)

    # 取出当前 workspace 下所有记忆，按 key 去重做 upsert
    from coworker.memory.base import MemoryItem

    existing_items: list[MemoryItem] = store.list(scope=Scope.WORKSPACE, workspace=ws)
    key_to_item: dict[str, MemoryItem] = {
        item.key: item for item in existing_items if item.key
    }

    written = 0
    for tag, _fname in _BIBLE_FILES.items():
        content = docs.get(tag)
        if not content:
            continue
        key = f"{_workspace_key()}:{tag}:{root.name}"
        existing = key_to_item.get(key)
        if existing:
            store.update(existing.id, content)
        else:
            store.add(content, scope=Scope.WORKSPACE, key=key, workspace=ws)
        written += 1
    store.close()
    return written


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------


def _read_legacy_bible(folder: str) -> dict[str, str]:
    """直接读四份 Markdown 文件（v1 散文圣经）。仅用于迁移判断与兜底。"""
    bible_dir = _bible_root(folder) / _BIBLE_DIR
    out: dict[str, str] = {}
    for tag, fname in _BIBLE_FILES.items():
        p = bible_dir / fname
        if p.is_file():
            try:
                out[tag] = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                logger.warning("圣经 legacy 文件读取失败，跳过：%s (%s)", p, e)
    return out


def read_bible(folder: str) -> dict[str, str]:
    """读取四份圣经。folder 可为小说目录或单个稿件文件。

    优先从**结构化实体库**确定性渲染（v2）：这样注入 prompt 的每条事实都是过了
    证据核验的，圆桌/评析等下游拿不到未经核验的内容。实体库不存在时（尚未用新版
    分析过）回退读旧的 Markdown 文件，保证老工作区不至于突然没有圣经可用。
    """
    bible_dir = _bible_root(folder) / _BIBLE_DIR
    if _ent.has_data(bible_dir):
        try:
            conn = _ent.connect(bible_dir)
            try:
                return _ent.render_bible(conn)
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("实体库渲染失败，回退读取 Markdown 圣经：%s", e)
    return _read_legacy_bible(folder)


def bible_stats(folder: str) -> dict[str, int | str]:
    """圣经的核验统计，用于界面/诊断：确认了多少条、还有多少待确认。

    ``version`` 为 "v2" 表示已是结构化实体库（每条事实都过了证据核验），
    "v1" 表示仍是旧散文圣经（内容未经核验，建议重新分析一次）。
    """
    bible_dir = _bible_root(folder) / _BIBLE_DIR
    if not _ent.has_data(bible_dir):
        return {"version": "v1" if _read_legacy_bible(folder) else "none", "total": 0}
    conn = _ent.connect(bible_dir)
    try:
        st = _ent.stats(conn)
    finally:
        conn.close()
    st["version"] = "v2"
    return st


# ---------------------------------------------------------------------------
# FTS5 全文检索
# ---------------------------------------------------------------------------

def _fts5_db_path(folder: str) -> Path:
    """返回 FTS5 数据库的完整路径（folder 可为目录或单个稿件文件）。"""
    return _bible_root(folder) / _BIBLE_DIR / _FTS5_DB


def _fts5_connection(folder: str) -> sqlite3.Connection | None:
    """返回 FTS5 数据库连接，若不存在则返回 None。"""
    db_path = _fts5_db_path(folder)
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def build_fts5_index(folder: str, force: bool = False) -> int:
    """构建或增量更新 FTS5 全文索引。

    扫描所有文本文件，将内容写入 SQLite FTS5 虚拟表，并在覆盖前记录修订 diff。

    变更判据为 **mtime + size 双条件**：两者都没变才跳过。单看 mtime 会漏检
    "同一毫秒内二次保存"以及 rsync/git checkout 等保留 mtime 的写入；size 从
    stat 里顺手就能拿到，零额外成本却能兜住绝大多数等长之外的漏检。

    Args:
        force: 忽略 mtime/size，逐文件读取正文与库内版本做内容比对。用于
            怀疑漏检时的兜底全量校准（成本=读一遍全部稿件）。

    返回本次索引的文件数。
    """
    scan_root, _ = _resolve_root(folder)
    db_path = _fts5_db_path(folder)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")

        # 创建 FTS5 虚拟表（如果不存在）
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS novels USING fts5(
                file_path,
                title,
                content,
                tokenize='unicode61'
            )
        """)

        # 创建文件跟踪表（记录 mtime + size 以实现增量索引）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_tracker (
                file_path TEXT PRIMARY KEY,
                mtime REAL NOT NULL,
                indexed_at REAL NOT NULL
            )
        """)
        # 旧库补 size 列（历史库没有该列，缺省 -1 表示"未知"，会强制走一次内容比对）
        _cols = {r["name"] for r in conn.execute("PRAGMA table_info(file_tracker)")}
        if "size" not in _cols:
            conn.execute("ALTER TABLE file_tracker ADD COLUMN size INTEGER DEFAULT -1")

        # 修订记录表：稿件每次变更都存一条「旧→新」差异，供"我今天改了哪"回放
        conn.execute("""
            CREATE TABLE IF NOT EXISTS manuscript_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                title TEXT,
                ts REAL NOT NULL,
                old_hash TEXT,
                new_hash TEXT,
                added INTEGER DEFAULT 0,
                removed INTEGER DEFAULT 0,
                diff TEXT
            )
        """)

        # 收集所有文本文件
        text_files = _iter_text_files(scan_root)

        indexed_count = 0
        for p in text_files:
            rel_path = _rel_name(p, scan_root)
            st = p.stat()
            current_mtime = st.st_mtime
            current_size = st.st_size

            # 检查 mtime + size 是否都未变（二者存在 file_tracker，不在 FTS5 表）
            tracker_row = conn.execute(
                "SELECT mtime, size FROM file_tracker WHERE file_path = ?",
                (rel_path,),
            ).fetchone()

            if not force and tracker_row is not None:
                same_mtime = abs(tracker_row["mtime"] - current_mtime) < 0.001
                tracked_size = tracker_row["size"]
                # size 为 -1 代表旧库无此列记录，视为未知 → 不敢跳过
                same_size = tracked_size is not None and tracked_size >= 0 \
                    and tracked_size == current_size
                if same_mtime and same_size:
                    continue  # 未变化，跳过

            text = _read_file_text(p)
            if not text.strip():
                continue

            title = p.stem  # 文件名（不含扩展名）作为标题

            # 取上一版全文（修订 diff 的基线，存于 FTS5 表 content 列）
            old_row = conn.execute(
                "SELECT content FROM novels WHERE file_path = ?",
                (rel_path,),
            ).fetchone()
            old_text = old_row["content"] if old_row else ""

            # 内容其实没变（touch / 元数据改动 / force 全量扫），只刷新指纹，不算一次索引
            if old_text == text:
                conn.execute(
                    "INSERT OR REPLACE INTO file_tracker (file_path, mtime, size, indexed_at)"
                    " VALUES (?, ?, ?, ?)",
                    (rel_path, current_mtime, current_size, time.time()),
                )
                continue

            # 删除旧记录（如果有），再插入新记录
            conn.execute("DELETE FROM novels WHERE file_path = ?", (rel_path,))
            conn.execute(
                "INSERT INTO novels (file_path, title, content) VALUES (?, ?, ?)",
                (rel_path, title, text),
            )
            conn.execute(
                "INSERT OR REPLACE INTO file_tracker (file_path, mtime, size, indexed_at)"
                " VALUES (?, ?, ?, ?)",
                (rel_path, current_mtime, current_size, time.time()),
            )

            # 修订回顾：有上一版且内容不同 → 记录差异（绝不影响主索引流程）
            if old_text and old_text != text:
                _record_revision(conn, rel_path, title, old_text, text)

            indexed_count += 1

        # 垃圾回收：清理已从磁盘删除的文件的旧索引（Bug B 修复）。
        # 之前只增/改不删：作者删掉某章稿件后，FTS5 检索仍会返回「死章节」片段。
        current_rel = {_rel_name(p, scan_root) for p in text_files}
        if current_rel:
            placeholders = ",".join("?" for _ in current_rel)
            conn.execute(
                f"DELETE FROM novels WHERE file_path NOT IN ({placeholders})",
                tuple(sorted(current_rel)),
            )
            conn.execute(
                f"DELETE FROM file_tracker WHERE file_path NOT IN ({placeholders})",
                tuple(sorted(current_rel)),
            )
        else:
            conn.execute("DELETE FROM novels")
            conn.execute("DELETE FROM file_tracker")

        conn.commit()
        return indexed_count
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 稿件修订回顾（版本快照 + diff）
# ---------------------------------------------------------------------------

# what_changed 摘要里每次修订最多展开的 diff 行数（库内存的是完整 diff）
_DIFF_PREVIEW_LINES = 50


def _record_revision(conn: sqlite3.Connection, rel_path: str, title: str,
                     old_text: str, new_text: str) -> None:
    """记录一次稿件修订：把旧→新全文的差异存入 manuscript_revisions。

    增强功能，任何异常都只记日志、绝不阻断主索引流程。
    """
    try:
        from deskmate.perception.diff_changes import diff_text, summarize_changes
        import hashlib

        # max_chars=None：入库必须存完整 diff。8000 截断是给 prompt/展示用的，
        # 存档若被腰斩，大章节的后半段修订就永久无法回放了。
        diff = diff_text(old_text, new_text, label=rel_path, max_chars=None)
        stats = summarize_changes(old_text, new_text)
        old_hash = hashlib.md5(old_text.encode("utf-8")).hexdigest()[:12]
        new_hash = hashlib.md5(new_text.encode("utf-8")).hexdigest()[:12]
        conn.execute(
            """INSERT INTO manuscript_revisions
               (file_path, title, ts, old_hash, new_hash, added, removed, diff)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (rel_path, title, time.time(), old_hash, new_hash,
             stats["added"], stats["removed"], diff),
        )
    except Exception as e:  # 修订是增强项，绝不能拖累主索引
        logger.warning("记录稿件修订失败（已忽略）：%s", e)


def list_revisions(folder: str, file: str | None = None, limit: int = 20) -> list[dict]:
    """列出稿件修订记录（按时间倒序）。file 指定则只列该文件。

    返回 dict 列表，字段含 file_path / title / ts / added / removed / diff 等。
    """
    conn = _fts5_connection(folder)
    if conn is None:
        return []
    try:
        sql = "SELECT * FROM manuscript_revisions"
        params: list = []
        if file:
            sql += " WHERE file_path = ?"
            params.append(file)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _parse_since(since: str) -> float:
    """把 'today'/'yesterday'/'3d'/'2天'/ISO 解析成时间戳阈值。"""
    import datetime

    now = datetime.datetime.now()
    s = (since or "today").strip().lower()
    if s in ("today", "今天"):
        return datetime.datetime(now.year, now.month, now.day).timestamp()
    if s in ("yesterday", "昨天"):
        d = now - datetime.timedelta(days=1)
        return datetime.datetime(d.year, d.month, d.day).timestamp()
    m = re.match(r"^(\d+)\s*(d|天|day)", s)
    if m:
        return (now - datetime.timedelta(days=int(m.group(1)))).timestamp()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(since, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def what_changed(folder: str, since: str = "today") -> str:
    """汇总自某时刻以来的稿件修订，返回人类可读摘要（对标 Hermes 的"改了哪几处"）。

    会先执行一次增量索引（build_fts5_index），确保捕获用户最新编辑，
    再汇总阈值之后的所有修订记录。桌搭在用户问"我今天改了哪/改了什么"时调用。
    """
    import datetime

    try:
        build_fts5_index(folder)
    except Exception as e:
        logger.warning("修订回顾前置索引失败（继续用已有记录）：%s", e)

    threshold = _parse_since(since)
    revs = list_revisions(folder, limit=200)
    revs = [r for r in revs if r.get("ts", 0) >= threshold]
    if not revs:
        return (f"（自 {since} 起未检测到稿件修订记录。"
                "若你已编辑稿件，请先刷新圣经/索引，让系统捕获这次变更后再问我。）")

    by_file: dict[str, list[dict]] = {}
    for r in revs:
        by_file.setdefault(r["file_path"], []).append(r)

    lines = [f"自 {since} 起，你共修订了 {len(by_file)} 个文件、{len(revs)} 次：", ""]
    for fp, items in by_file.items():
        added = sum(i.get("added", 0) for i in items)
        removed = sum(i.get("removed", 0) for i in items)
        lines.append(f"▸ {fp}（{len(items)} 次修订，新增 {added} / 删除 {removed} 处）")
        for i in items:
            ts = datetime.datetime.fromtimestamp(i["ts"]).strftime("%m-%d %H:%M")
            lines.append(f"    · {ts} 的修订：")
            diff = (i.get("diff") or "").strip()
            if diff and diff != "（无变更）":
                body = [dl for dl in diff.splitlines()
                        if not dl.startswith(("+++", "---", "@@", "Index"))]
                for dl in body[:_DIFF_PREVIEW_LINES]:
                    lines.append(f"      {dl}")
                # 库里存的是完整 diff，这里只是展示截断——必须说明，否则用户
                # 会以为"就改了这些"，正是当初 8000 硬截断埋下的同一类误导。
                if len(body) > _DIFF_PREVIEW_LINES:
                    lines.append(
                        f"      …（此处还有 {len(body) - _DIFF_PREVIEW_LINES} 行改动未展开，"
                        f"用 novel_revisions 查看完整 diff）"
                    )
        lines.append("")
    return "\n".join(lines)


def novel_search_fts5(query: str, folder: str, max_results: int = 10) -> str:
    """使用 FTS5 进行全文检索。

    如果 FTS5 索引不存在，自动回退到 keyword-based novel_recall。
    支持 CJK 查询：FTS5 的 unicode61 tokenizer 不原生支持 CJK 分词，
    因此对于 CJK 密集的查询会使用 LIKE 作为回退。
    """
    db_path = _fts5_db_path(folder)

    # 索引不存在 -> 回退到 keyword 检索
    if not db_path.exists():
        return _keyword_recall(query, folder, max_snippets=max_results)

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:

            # 检查是否包含 CJK 字符（中文、日文、韩文）
            has_cjk = bool(re.search(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]", query))
    
            if has_cjk:
                # FTS5 unicode61 不原生支持 CJK 分词，回退到 LIKE 搜索
                # 提取关键词（≥2字），每个关键词用 LIKE 匹配
                kws = list(dict.fromkeys(
                    w for w in re.split(r"[\s，。、？！,.?!；：“”‘’…—()\[\]【】-]+", query)
                    if len(w) >= 2
                ))[:8]
    
                if not kws:
                    return _keyword_recall(query, folder, max_snippets=max_results)
    
                # 构建 LIKE 子句（转义 % 和 _ 以防止 LIKE 模式注入）
                like_clauses = " OR ".join("content LIKE ? ESCAPE '\\'" for _ in kws)
                like_params = [f"%{kw.replace('%', '\\%').replace('_', '\\_')}%" for kw in kws]
                sql = f"""
                    SELECT file_path, title, content
                    FROM novels
                    WHERE {like_clauses}
                    ORDER BY rowid
                    LIMIT ?
                """
                rows = conn.execute(sql, (*like_params, max_results)).fetchall()
            else:
                # 非 CJK 查询：使用 FTS5 原生 MATCH
                # 对查询进行简单转义（移除 FTS5 特殊字符）
                sanitized = re.sub(r'[^\w\s]', ' ', query).strip()
                if not sanitized:
                    return _keyword_recall(query, folder, max_snippets=max_results)
    
                rows = conn.execute(
                    "SELECT file_path, title, content, rank FROM novels WHERE content MATCH ? ORDER BY rank LIMIT ?",
                    (sanitized, max_results),
                ).fetchall()
        finally:
            conn.close()

        if not rows:
            # FTS5 无结果 -> 回退到 keyword 检索
            return _keyword_recall(query, folder, max_snippets=max_results)

        # 格式化结果
        parts: list[str] = []
        for i, row in enumerate(rows, 1):
            file_path = row["file_path"]
            content = row["content"]
            # 查找查询词在内容中的位置，提取上下文片段
            snippet = _extract_snippet(content, query, context_chars=200)
            parts.append(f"[{i}] {file_path}")
            parts.append(f"    {snippet}")
            parts.append("")

        # 附加圣经摘要
        bible = read_bible(folder)
        if bible:
            bible_parts: list[str] = []
            if bible.get("CHARACTERS"):
                bible_parts.append(bible["CHARACTERS"][:4000])
            for tag in ("OUTLINE", "TIMELINE", "NOTES"):
                if bible.get(tag):
                    bible_parts.append(f"## {tag}\n{bible[tag][:1500]}")
            if bible_parts:
                parts.append("【人物圣经摘要】")
                parts.append("\n\n".join(bible_parts))

        return "\n".join(parts)

    except Exception as e:
        # 任何 FTS5 异常 -> 安全回退到 keyword 检索
        logger.warning("FTS5 search failed, falling back to keyword recall: %s", e)
        return _keyword_recall(query, folder, max_snippets=max_results)


def _extract_snippet(text: str, query: str, context_chars: int = 200) -> str:
    """从文本中提取包含查询词的最佳上下文片段。"""
    # 提取查询中的关键词
    kws = [w for w in re.split(r"[\s，。、？！,.?!；：“”‘’…—()\[\]【】-]+", query) if len(w) >= 2]
    if not kws:
        return text[:context_chars * 2].strip()

    # 找到第一个关键词的位置
    best_pos = -1
    for kw in kws:
        pos = text.find(kw)
        if pos != -1 and (best_pos == -1 or pos < best_pos):
            best_pos = pos

    if best_pos == -1:
        return text[:context_chars * 2].strip()

    start = max(0, best_pos - context_chars)
    end = min(len(text), best_pos + context_chars)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def fts5_index_status(folder: str) -> str:
    """报告 FTS5 索引的状态。

    返回：索引是否存在、文件数、最后更新时间。
    """
    db_path = _fts5_db_path(folder)
    if not db_path.exists():
        return "FTS5 索引不存在。请运行 build_fts5_index() 创建。"

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            # 查询索引文件数
            count = conn.execute("SELECT COUNT(*) FROM novels").fetchone()[0]

            # 查询最后更新时间
            last = conn.execute(
                "SELECT MAX(indexed_at) FROM file_tracker"
            ).fetchone()[0]

            # 查询数据库文件信息
            db_size = db_path.stat().st_size
            db_mtime = db_path.stat().st_mtime
        finally:
            conn.close()

        lines = [
            f"FTS5 索引状态：",
            f"  - 数据库：{db_path}",
            f"  - 文件数：{count}",
            f"  - 数据库大小：{db_size / 1024:.1f} KB",
            f"  - 最后索引时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last)) if last else '未知'}",
            f"  - 数据库修改时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(db_mtime))}",
        ]
        return "\n".join(lines)

    except Exception as e:
        return f"FTS5 索引状态查询失败：{e}"


# Bug A 修复：回退路径圣经摘要的 CHARACTERS 预算（保持原行为量级，单位：字符）
_BIBLE_CHAR_BUDGET = 6000


def _split_character_blocks(chars_text: str) -> list[str]:
    """把 CHARACTERS 文本按角色条目切成完整块。

    块起点：行首 ``- **``（v2 实体库渲染的角色条目）或 ``#### ``（v1 Markdown
    角色段落）。其余行（分区标题、缩进的证据标注、关系区）累积进当前块。
    """
    lines = chars_text.splitlines()
    blocks: list[str] = []
    current: list[str] = []
    for ln in lines:
        if re.match(r"^(?:- \*\*|#### )", ln):
            if current:
                blocks.append("\n".join(current))
            current = [ln]
        else:
            current.append(ln)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _pick_character_blocks(chars_text: str, kws: list[str], budget: int) -> str:
    """按查询关键词从 CHARACTERS 中选取角色块（完整块，绝不把角色切成两半）。

    - 有关键词命中：优先取「包含关键词」的块，按命中数降序、原文序稳定，预算内尽量多；
    - 无命中块 / 无关键词：按原文顺序取完整块，直到预算；
    - 允许首个块单独超预算：宁可轻微超预算，也不把角色段落切半
      （整段盲截把 6000 字符后的角色整段切没，正是 Bug A 的根源）。
    """
    blocks = _split_character_blocks(chars_text)
    if not blocks:
        return ""

    def take(items: list[tuple[int, str]]) -> list[str]:
        selected: list[str] = []
        total = 0
        for _i, b in items:
            if total + len(b) <= budget or not selected:
                selected.append(b)
                total += len(b)
            else:
                break
        return selected

    if kws:
        hits = [
            (i, b, sum(1 for kw in kws if kw in b))
            for i, b in enumerate(blocks)
        ]
        hit_items = [(i, b, n) for i, b, n in hits if n > 0]
        if hit_items:
            hit_items.sort(key=lambda x: (-x[2], x[0]))
            selected = take([(i, b) for i, b, _n in hit_items])
            if selected:
                return "\n\n".join(selected)
    return "\n\n".join(take([(i, b) for i, b in enumerate(blocks)]))


def _keyword_recall(query: str, folder: str, max_snippets: int = 8) -> str:
    """纯关键词检索（原始实现）：逐文件扫描，正则匹配关键词提取片段。

    这是 FTS5 不可用时的回退方案。    """
    scan_root, _ = _resolve_root(folder)
    parts: list[str] = []

    # 提取所有关键词（≥2字），去重，最多8个。提前到圣经摘要之前，
    # 让摘要能按查询涉及的实体动态选取角色块（Bug A 修复）。
    kws = list(dict.fromkeys(
        w for w in re.split(r"[\s，。、？！,.?!；：“”‘’…—()\[\]【】-]+", query)
        if len(w) >= 2
    ))[:8]

    bible = read_bible(folder)
    if bible:
        parts.append("【人物圣经摘要】")
        if bible.get("CHARACTERS"):
            chars_summary = _pick_character_blocks(
                bible["CHARACTERS"], kws, budget=_BIBLE_CHAR_BUDGET
            )
            if chars_summary:
                parts.append(chars_summary)
        for tag in ("OUTLINE", "TIMELINE", "NOTES"):
            if bible.get(tag):
                parts.append(f"\n## {tag}\n{bible[tag][:2000]}")

    if not kws:
        if not parts:
            return "（尚未生成人物圣经，请先运行 analyze_novel）"
        return "\n".join(parts)

    # 构建正则：任意关键词命中即匹配
    pattern = re.compile("|".join(re.escape(k) for k in kws), re.IGNORECASE)

    # 收集候选片段：(score, file_rel, snippet_text)
    candidates: list[tuple[int, str, str]] = []
    text_files = _iter_text_files(scan_root)
    for p in text_files:
        text = _read_file_text(p)
        if not text.strip():
            continue
        rel = _rel_name(p, scan_root)
        covered: list[tuple[int, int]] = []  # 已覆盖的字符位置（去重重叠片段）
        file_hits = 0
        for m in pattern.finditer(text):
            s = max(0, m.start() - 200)
            e = min(len(text), m.end() + 200)
            # 跳过已被之前片段覆盖的区域
            if any(s < ce and e > cs for cs, ce in covered):
                continue
            covered.append((s, e))
            snippet_raw = text[s:e].strip()
            hit_count = sum(1 for kw in kws if kw in snippet_raw)
            snippet = f"[{rel}] …{snippet_raw}…"
            candidates.append((hit_count, rel, snippet))
            file_hits += 1
            if file_hits >= 3:  # 每个文件最多3个片段
                break
        # 限制候选总数
        if len(candidates) >= max_snippets * 3:
            break

    # 按命中数降序取 top N
    candidates.sort(key=lambda x: -x[0])
    top = [c[2] for c in candidates[:max_snippets]]

    if top:
        parts.append(f"\n【正文命中片段】（关键词：{'、'.join(kws[:5])}）\n" + "\n---\n".join(top))

    if not parts:
        return "（尚未生成人物圣经，请先运行 analyze_novel）"
    return "\n".join(parts)


def keyword_recall(query: str, folder: str, max_snippets: int = 8) -> str:
    """公开包装：对中文更可靠的纯关键词检索（见 ``_keyword_recall``）。

    圆桌等外部模块应调用本函数而非直连 ``_keyword_recall``。
    """
    return _keyword_recall(query, folder, max_snippets=max_snippets)


def novel_recall(query: str, folder: str, max_snippets: int = 8) -> str:
    """跨章检索：优先使用 FTS5 全文索引，无索引或无结果时回退到关键词匹配。

    检索策略：
    1. 尝试 FTS5 全文检索（如果索引存在）
    2. FTS5 无结果或无索引时，降级到关键词逐文件扫描
    """
    # 尝试 FTS5 优先路径
    db_path = _fts5_db_path(folder)
    if db_path.exists():
        try:
            result = novel_search_fts5(query, folder, max_results=max_snippets)
            return result
        except Exception as e:
            logger.warning("FTS5 search failed, falling back to keyword: %s", e)

    # 回退到纯关键词检索
    return _keyword_recall(query, folder, max_snippets=max_snippets)


def novel_relationships(character: str, folder: str) -> str:
    """查某角色在圣经中的所有关系。"""
    bible = read_bible(folder)
    chars = bible.get("CHARACTERS")
    if not chars:
        return "（尚未生成人物圣经，请先运行 analyze_novel）"
    # 简单按行匹配包含角色名的行
    lines = [ln for ln in chars.splitlines() if character in ln]
    if not lines:
        return f"圣经中未直接提及「{character}」。可运行 novel_recall 做跨章检索。"
    return "\n".join(lines)




# ---------------------------------------------------------------------------
# 矛盾检测
# ---------------------------------------------------------------------------

# 关系对立词组：(正向词, 反向词)
_RELATION_OPPOSITES = [
    ("敌", "友"),
    ("敌对", "友好"),
    ("对手", "伙伴"),
    ("仇", "友"),
    ("恨", "爱"),
    ("对立", "联盟"),
]

# 关系关键词 — 用于在"与X友好"中识别名字边界
_RELATION_CONTEXT = r"[友好对立敌仇恨亲暧昧上下级同门联盟伙伴对手]"


def _extract_relationship_pairs(text: str) -> dict[tuple[str, str], list[str]]:
    """从人物圣经文本中提取 (角色A, 角色B) -> [关系关键词列表] 的映射。

    匹配两种模式：
    1. "- 张三：...与李四友好" — 行首名字为主语，`与X` 为目标
    2. "张三与李四关系密切" / "张三和李四" — 行内 A与B / A和B
    """
    pairs: dict[tuple[str, str], list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # 模式1：行首 "- NAME：描述...与TARGET关系词"
        m = re.match(r"^[-*]?\s*([一-鿿]{2,3})[：:]", line)
        if m:
            subject = m.group(1)
            rest = line[m.end():]
            # 非贪婪匹配：优先 2 字名（中文名最常见长度）
            for tm in re.finditer(r"与([一-鿿]{2,3}?)(?=" + _RELATION_CONTEXT + r")", rest):
                target = tm.group(1)
                key = tuple(sorted([subject, target]))
                rel_words = re.findall(
                    r"[敌友仇恨亲暧昧上下级同门对手伙伴对立联盟爱]",
                    rest[tm.end(1):tm.end(1) + 10],
                )
                if key not in pairs:
                    pairs[key] = []
                pairs[key].extend(rel_words)
            continue

        # 模式2：行内 "A与B" 或 "A和B"（简单提及）
        for tm in re.finditer(r"([一-鿿]{2,3})[与和]([一-鿿]{2,3})", line):
            a, b = tm.group(1), tm.group(2)
            key = tuple(sorted([a, b]))
            rel_words = re.findall(
                r"[敌友仇恨亲暧昧上下级同门对手伙伴对立联盟爱]",
                line[tm.end():tm.end() + 10],
            )
            if key not in pairs:
                pairs[key] = []
            pairs[key].extend(rel_words)

    return pairs


def check_consistency(bible_docs: dict[str, str], new_text: str) -> dict:
    """检测新文本是否与人物圣经中的关系存在矛盾。

    纯逻辑函数，不调用 LLM。基于关键词对立检测。

    Args:
        bible_docs: 从 read_bible() 或 _parse_bible_docs() 得到的圣经文档字典。
        new_text: 新写的章节文本。

    Returns:
        {"consistent": bool, "issues": list[str], "checked_pairs": int}
    """
    chars_text = bible_docs.get("CHARACTERS", "")
    if not chars_text:
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    bible_pairs = _extract_relationship_pairs(chars_text)
    if not bible_pairs:
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    issues: list[str] = []
    checked = 0

    for (a, b), rel_words in bible_pairs.items():
        # 只检查新文本中同时提到两个角色的情况
        if a not in new_text or b not in new_text:
            continue
        checked += 1

        # 判断圣经中这对角色的关系方向
        bible_is_friendly = any(
            fw in rw for rw in rel_words
            for fw in ("友", "好", "爱", "伙伴", "联盟")
        )
        bible_is_hostile = any(
            hw in rw for rw in rel_words
            for hw in ("敌", "仇", "恨", "对立")
        )

        # 在新文本中找这两个角色共同出现的句子
        sentences = re.split(r"[。！？\n]", new_text)
        for sent in sentences:
            if a not in sent or b not in sent:
                continue

            # 检查句子中是否出现与圣经矛盾的关系词
            # pos_word = 敌/仇/恨, neg_word = 友/爱
            # 圣经友好 + 文本出现敌意词(pos_word) → 矛盾
            # 圣经敌对 + 文本出现友好词(neg_word) → 矛盾
            for pos_word, neg_word in _RELATION_OPPOSITES:
                if bible_is_friendly and pos_word in sent:
                    issues.append(
                        f"矛盾：圣经中「{a}」与「{b}」为友好关系，"
                        f"但新文本中「{sent.strip()}」暗示敌对"
                    )
                    break  # 一句话只报一次
                if bible_is_hostile and neg_word in sent:
                    issues.append(
                        f"矛盾：圣经中「{a}」与「{b}」为敌对关系，"
                        f"但新文本中「{sent.strip()}」暗示友好"
                    )
                    break

    # 去重
    issues = list(dict.fromkeys(issues))
    return {
        "consistent": len(issues) == 0,
        "issues": issues,
        "checked_pairs": checked,
    }


# ---------------------------------------------------------------------------
# LLM 语义级矛盾检测
# ---------------------------------------------------------------------------

_CONSISTENCY_LLM_SYS = (
    "你是长篇小说的人物关系审核员。给你一份【人物关系圣经】和一段【新章节文本】。"
    "你的任务是判断新章节是否存在与圣经矛盾的人物关系变化。\n"
    "\n"
    "重点检测以下隐含矛盾（正则检测不到的）：\n"
    "1. 关系性质变化但未明确写出关键词（如「投靠」「背叛」「暗通」「倒戈」"
    "暗示阵营转变，但未出现「敌」「友」字样）\n"
    "2. 行为与关系不一致（圣经中是盟友，新文本中一方偷偷设局对付另一方）\n"
    "3. 态度转变缺乏铺垫（长期友好的角色突然冷淡/敌视，无合理转折）\n"
    "4. 阵营归属矛盾（圣经中属A阵营，新文本中表现为B阵营效力）\n"
    "\n"
    "只报告有把握的矛盾。如果新章节有合理的剧情转折铺垫，不算矛盾。\n"
    "输出严格 JSON：\n"
    '{"consistent": true/false, "issues": ["矛盾1描述", "矛盾2描述"], "reasoning": "简述"}\n'
    "consistent=true 时 issues 为空数组。只输出 JSON，不要其他文字。"
)


def check_consistency_llm(bible_docs: dict[str, str], new_text: str) -> dict:
    """LLM 语义级矛盾检测。

    能识别正则检测不到的隐含关系变化（投靠、背叛、暗通、行为矛盾等）。
    需要 LLM provider 凭证；无密钥或异常时返回空结果（降级为纯正则检测）。

    Args:
        bible_docs: 从 read_bible() 得到的圣经文档字典。
        new_text: 新写的章节文本。

    Returns:
        {"consistent": bool, "issues": list[str], "checked_pairs": int}
    """
    chars_text = bible_docs.get("CHARACTERS", "")
    if not chars_text or not new_text.strip():
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    # 复用 deskmate.memory.llm 的 llm_available（走 SecretStore，自动跟随 provider）
    try:
        from deskmate.memory.llm import llm_available
    except ImportError:
        llm_available = lambda: False  # type: ignore

    if not llm_available():
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    # 构建圣经摘要（限制长度避免 token 爆炸）
    bible_summary = chars_text[:4000]
    outline = bible_docs.get("OUTLINE", "")
    if outline:
        bible_summary += f"\n\n## 情节主线\n{outline[:1500]}"

    new_text_trimmed = new_text[:6000]

    user_prompt = (
        f"【人物关系圣经】\n{bible_summary}\n\n"
        f"---\n\n【新章节文本】\n{new_text_trimmed}\n\n"
        f"请判断新章节是否与圣经中的人物关系存在矛盾。"
    )

    try:
        resp = _chat(_CONSISTENCY_LLM_SYS, user_prompt, temperature=0.15)
    except Exception:
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    # 解析 JSON
    import json

    m = re.search(r"\{.*\}", resp, re.DOTALL)
    if not m:
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    try:
        data = json.loads(m.group(0))
        issues = [str(i) for i in data.get("issues", []) if i]
        return {
            "consistent": bool(data.get("consistent", True)) and len(issues) == 0,
            "issues": issues,
            "checked_pairs": len(issues),  # LLM 检测到的矛盾数
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"consistent": True, "issues": [], "checked_pairs": 0}


def check_consistency_enhanced(bible_docs: dict[str, str], new_text: str) -> dict:
    """增强矛盾检测：先跑正则快速检测，有 API Key 时再跑 LLM 语义检测，合并结果。

    Args:
        bible_docs: 从 read_bible() 得到的圣经文档字典。
        new_text: 新写的章节文本。

    Returns:
        {"consistent": bool, "issues": list[str], "checked_pairs": int}
    """
    # 第一层：正则快速检测（永远执行）
    regex_result = check_consistency(bible_docs, new_text)

    # 第二层：LLM 语义检测（有 API Key 时执行）
    llm_result = check_consistency_llm(bible_docs, new_text)

    # 合并结果
    all_issues = regex_result["issues"] + llm_result["issues"]
    # 去重（LLM 和正则可能报告同一矛盾）
    all_issues = list(dict.fromkeys(all_issues))

    return {
        "consistent": len(all_issues) == 0,
        "issues": all_issues,
        "checked_pairs": regex_result["checked_pairs"] + llm_result["checked_pairs"],
    }
