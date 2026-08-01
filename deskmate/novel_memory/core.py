"""小说记忆核心逻辑：扫描、抽取人物圣经、注入 OpenWorker workspace 记忆、跨章检索。

模型调用统一走 OpenWorker 的 SecretStore 体系（deskmate.memory.llm），
自动跟随用户在 Settings 中选择的 provider，无需单独配置。

记忆库：OpenWorker 的 SQLite（state_dir/coworker.db），scope=WORKSPACE、workspace=小说目录绝对路径，
会被 build_engine 自动注入每轮对话，从而根治「敌人变朋友」。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

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

# 扫描的小说文本扩展名
_TEXT_EXTS = {".txt", ".md", ".markdown", ".text", ".novel"}


def _client():
    from openai import OpenAI
    from deskmate.memory.llm import _api_key, _base_url

    return OpenAI(api_key=_api_key(), base_url=_base_url())


def _model() -> str:
    from deskmate.memory.llm import _model as _llm_model
    return _llm_model()


def _chat(system: str, user: str, *, temperature: float = 0.2, timeout: float = 90) -> str:
    client = _client()
    resp = client.chat.completions.create(
        model=_model(),
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        timeout=timeout,
    )
    return (resp.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# 扫描与分块
# ---------------------------------------------------------------------------


def scan_novel(folder: str, max_chars: int = 400_000) -> tuple[str, list[str]]:
    """递归读取小说文件夹里的正文文本，返回 (合并正文, 章节标题列表)。

    跳过 .deskmate / .git / 隐藏目录。超大则按比例截断到 max_chars。
    """
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"不是目录：{folder}")

    chunks: list[str] = []
    titles: list[str] = []
    total = 0
    # 稳定顺序：按路径排序，保证章节顺序一致
    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in _TEXT_EXTS
        and not any(part.startswith(".") or part == ".deskmate" or part == ".git"
                    for part in p.relative_to(root).parts)
    )
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not text.strip():
            continue
        rel = p.relative_to(root).as_posix()
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

_EXTRACT_SYS = (
    "你是长篇小说的关系分析师。用户会给你一段小说正文（可能只是一部分）。"
    "请只基于【这段正文里明确写出的内容】抽取事实，严禁臆测或脑补。"
    "输出 Markdown，分四节：\n"
    "## 人物\n- 列出本段出现的新人物（姓名/别名），一句话身份。\n"
    "## 关系\n- 逐条列出人物间关系，必须标注关系类型（敌 / 友 / 亲 / 暧昧 / 上下级 / 同门 / 其他）"
    "并尽量注明依据（如「第X章 两人对峙」）。\n"
    "## 事件\n- 本段关键情节节点（推动关系变化的事件）。\n"
    "## 设定\n- 本段出现的世界观 / 势力 / 地点设定。\n"
    "如果某节无内容就写「（无）」。只输出这四节，不要寒暄。"
)

_MERGE_SYS = (
    "你是长篇小说的关系总编。下面是一本书各段落的分析片段（可能重复、冲突）。"
    "请综合成一份【权威人物关系圣经】，作为后续所有人物讨论的唯一准绳。规则：\n"
    "1. 合并同名人物，去重；关系冲突时以「更靠后章节的明确描写」为准，并注明分歧。\n"
    "2. 关系必须带类型（敌/友/亲/暧昧/上下级/同门/其他）和关键依据（章节或情节）。\n"
    "3. 禁止写入任何正文未明确支持的关系；存疑的放进「待确认」。\n"
    "输出四份独立 Markdown 文档，用分隔符 ===== 隔开，顺序固定为：\n"
    "CHARACTERS（人物关系总表，核心）\nOUTLINE（情节主线与弧光）\n"
    "TIMELINE（关键事件时间线）\nNOTES（世界观/势力/伏笔/待确认）。\n"
    "每份以「## 标题」开头。只输出内容，不要解释。"
)

_DOC_TAGS = list(_BIBLE_FILES.keys())


def build_bible(folder: str) -> dict[str, str]:
    """通读全书 -> 抽取 -> 合并成四份圣经文档。返回 {文档名: markdown}。"""
    text, _ = scan_novel(folder)
    if not text.strip():
        raise ValueError(f"在 {folder} 未找到可读的小说文本（.txt/.md 等）。")

    # 1) 逐批抽取（并发，大幅缩短总耗时）
    batches = _split_batches(text)
    extracts: list[str] = [""] * len(batches)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _extract_one(idx: int, batch: str) -> tuple[int, str]:
        try:
            part = _chat(_EXTRACT_SYS, batch)
        except Exception as e:
            part = f"（第 {idx + 1} 批抽取失败：{e}）"
        return idx, part

    with ThreadPoolExecutor(max_workers=min(5, len(batches))) as pool:
        futures = {
            pool.submit(_extract_one, i, batch): i
            for i, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            idx, part = future.result()
            extracts[idx] = f"===== 第 {idx + 1} 批 =====\n{part}"

    # 2) 合并成圣经
    merged = _chat(_MERGE_SYS, "\n\n".join(extracts), timeout=120)

    # 3) 解析成四份文档
    docs = _parse_bible_docs(merged)
    # 兜底：若解析失败，整体存为 CHARACTERS
    if not docs.get("CHARACTERS"):
        docs["CHARACTERS"] = merged
    return docs


def _parse_bible_docs(merged: str) -> dict[str, str]:
    """按 ===== 分隔符与文档标记切分合并输出。"""
    docs: dict[str, str] = {}
    # 先按分隔符切块
    blocks = re.split(r"^=====.*$", merged, flags=re.MULTILINE)
    # 退化：若没有分隔符，尝试按标题切
    if len(blocks) <= 1:
        blocks = re.split(r"(?=^## (?:CHARACTERS|OUTLINE|TIMELINE|NOTES))", merged, flags=re.MULTILINE)
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        # 找到文档标记
        m = re.match(r"^##\s*(CHARACTERS|OUTLINE|TIMELINE|NOTES)\b", b, flags=re.IGNORECASE)
        if m:
            tag = m.group(1).upper()
            docs[tag] = b
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


def inject_bible(folder: str, docs: dict[str, str]) -> int:
    """把四份圣经写入 <folder>/.deskmate/ 并 upsert 为 OpenWorker workspace 记忆。返回写入条数。

    使用 ``coworker.memory.SQLiteMemoryStore`` API 写入，而非裸 SQL -- 这样 coworker
    schema 变更时（加字段、改表名）不会静默失败。
    """
    root = Path(folder).expanduser().resolve()
    bible_dir = root / _BIBLE_DIR
    bible_dir.mkdir(parents=True, exist_ok=True)
    for tag, fname in _BIBLE_FILES.items():
        content = docs.get(tag)
        if content:
            (bible_dir / fname).write_text(content, encoding="utf-8")

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


def read_bible(folder: str) -> dict[str, str]:
    bible_dir = Path(folder).expanduser().resolve() / _BIBLE_DIR
    out: dict[str, str] = {}
    for tag, fname in _BIBLE_FILES.items():
        p = bible_dir / fname
        if p.is_file():
            out[tag] = p.read_text(encoding="utf-8")
    return out


def novel_recall(query: str, folder: str, max_snippets: int = 8) -> str:
    """跨章检索：圣经相关段落 + 正文中命中关键词的片段。

    检索策略：提取查询中所有关键词（≥2字），逐文件扫描，
    按命中关键词数量排序，优先返回多关键词命中的片段。
    """
    root = Path(folder).expanduser().resolve()
    parts: list[str] = []

    bible = read_bible(str(root))
    if bible:
        parts.append("【人物圣经摘要】")
        if bible.get("CHARACTERS"):
            parts.append(bible["CHARACTERS"][:6000])
        for tag in ("OUTLINE", "TIMELINE", "NOTES"):
            if bible.get(tag):
                parts.append(f"\n## {tag}\n{bible[tag][:2000]}")

    # 提取所有关键词（≥2字），去重，最多8个
    kws = list(dict.fromkeys(
        w for w in re.split(r"[\s，。、？！,.?!；：“”‘’…—()\[\]【】-]+", query)
        if len(w) >= 2
    ))[:8]
    if not kws:
        if not parts:
            return "（尚未生成人物圣经，请先运行 analyze_novel）"
        return "\n".join(parts)

    # 构建正则：任意关键词命中即匹配
    pattern = re.compile("|".join(re.escape(k) for k in kws), re.IGNORECASE)

    # 收集候选片段：(score, file_rel, snippet_text)
    candidates: list[tuple[int, str, str]] = []
    text_files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in _TEXT_EXTS
        and not any(part.startswith(".") or part == ".deskmate" or part == ".git"
                    for part in p.relative_to(root).parts)
    )
    for p in text_files:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = str(p.relative_to(root))
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
