"""原著索引与改编圣经（阶段 5 · 长文本 I/O 输入侧）。

解决「长篇原著只看到前 6000 字、后面 99% 情节 AI 全看不到」的问题：

- **一次性建索引**：读全文 → 切章 → 生成「改编圣经」（人物表 / 情节线 / 主题 /
  关键场景）缓存到 ``~/.deskmate/novels/<hash>/``，可复用，不重复烧 token；
- **按需取章**：``pick_chapters(topic)`` 用中文 bigram 匹配章节标题与开头，
  命中章节再读正文——写某一幕时只喂相关章节，全局（圣经）+ 细节（分章）兼顾。

设计决策（见 docs §阶段5，2026-08-27 萧哥拍板「摘要+分章混合」）：
- 不引 RAG / 向量库；圣经生成输入 = 每章「标题 + 开头采样」，总量限 ~2 万字符，
  百万字原著也只需一次 LLM 调用；
- LLM 不可用时自动降级为「章节索引圣经」（各章标题 + 开头采样拼接），
  保证 stub / 无凭证环境链路可跑通；
- 缓存写盘一律原子写（临时文件 + os.replace），避免中断留下半截文件。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

def default_cache_root() -> Path:
    """缓存根目录默认值——**调用时**解析（可被参数 ``cache_root`` 覆盖）。

    禁模块级常量（2026-09-22 收口）：import 期绑定会让隔离失效——测试会读到
    真实 ``~/.deskmate/novels`` 下的索引，造成假通过 / 假失败。
    """
    from deskmate.paths import deskmate_base_dir

    return deskmate_base_dir() / "novels"

# 圣经生成输入采样上限（每章「标题+开头」拼接后的总字符上限）
BIBLE_SAMPLE_CHARS = 24_000
# 每章三点采样每段下限（按章节数动态收缩，见 _sample_chapters）
HEAD_MIN = 200
# 降级圣经（无 LLM）的拼接上限
FALLBACK_BIBLE_CHARS = 20_000
# 圣经 LLM 输出上限
BIBLE_MAX_TOKENS = 3000


class NovelIndexError(Exception):
    """原著索引失败（文件不可读 / 缓存损坏等）。"""


def _cache_key(ref: str) -> str:
    """按 绝对路径 + 文件大小 + mtime 计算缓存目录名——原著文件一改，hash 即变，
    自然走新缓存，不会误用旧圣经。"""
    p = Path(ref).expanduser().resolve()
    try:
        st = p.stat()
        seed = f"{p}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        seed = str(p)
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _is_error(text: str) -> bool:
    """判定读取返回是否为错误说明（与 file_reader 约定一致：以（ 开头且无换行）。"""
    return bool(text) and text.startswith("（") and "\n" not in text


def _topic_grams(topic: str) -> set[str]:
    """把主题词拆成匹配单元：原词 + 中文 bigram。如「李慕白」→ {李慕白, 李慕, 慕白}。"""
    t = topic.strip()
    if not t:
        return set()
    grams = {t}
    for i in range(len(t) - 1):
        two = t[i : i + 2]
        if re.search(r"[\u4e00-\u9fff]", two):
            grams.add(two)
    return grams


def _sample_chapters(
    chapters: list[tuple[str, str]], limit: int = BIBLE_SAMPLE_CHARS
) -> str:
    """每章多点采样：开头 + 中段 + 结尾，短文本直接全量返回。

    背景：人物动机、阴谋、冤案、弧光转折往往埋在章节中后段，旧逻辑只取
    「标题+开头」会系统性丢信息（见《大明水师》圣经事故：通倭/越狱/栖凤村
    血仇/灭口等核心动机全部被采样过滤）。故：
      - 全文在预算内 → 直接全量喂，零信息损失；
      - 长篇 → 每章按「开头 / 中段 / 结尾」三点各取一段，覆盖关键转折与真相。
    """
    n = len(chapters)
    if not n:
        return ""
    total = sum(len(body) for _, body in chapters)
    # 短文本：全文在采样预算内，直接全量返回（信息最保真）
    if total <= limit:
        return "\n\n".join(f"【{title}】\n{body}" for title, body in chapters)
    # 长文本：每章「开头 + 中段 + 结尾」三点采样
    per = max(HEAD_MIN, limit // max(n, 1))  # 每章预算，动态收缩
    seg = max(1, per // 3)  # 每段（头/中/尾）长度
    parts: list[str] = []
    budget = limit
    for title, body in chapters:
        lenb = len(body)
        if lenb <= per:
            piece = f"【{title}】\n{body}"
        else:
            mid_start = lenb // 2 - seg // 2
            piece = (
                f"【{title}】（开头）\n{body[:seg]}\n\n"
                f"（中段）\n{body[mid_start:mid_start + seg]}\n\n"
                f"（结尾）\n{body[-seg:]}"
            )
        if budget - len(piece) < 0 and parts:
            break
        parts.append(piece)
        budget -= len(piece)
    return "\n\n".join(parts)


class NovelIndex:
    """一个原著文件对应的索引（圣经 + 章节目录），按 ref 缓存可复用。"""

    def __init__(self, ref: str, *, cache_root: Path | None = None) -> None:
        self.ref = str(ref).strip()
        if not self.ref:
            raise NovelIndexError("原著路径为空")
        self.cache_root = Path(cache_root) if cache_root else default_cache_root()
        self.cache_dir = self.cache_root / _cache_key(self.ref)
        self.bible_path = self.cache_dir / "bible.md"
        self.chapters_path = self.cache_dir / "chapters.json"

    # -- 建索引 ----------------------------------------------------------
    def build(self, force: bool = False, degrade: bool = False) -> dict:
        """建立（或复用）索引。返回 meta：{bible, chapters, total_chars, n, degraded}。

        degraded=True 表示圣经为降级（LLM 未配置）；False 为 LLM 精炼版。

        参数 degrade：True=强制生成降级圣经（零 LLM，stub/离线场景不烧 token）；
        False=尽量生成 LLM 精炼圣经（LLM 不可用则自动降级）。
        缓存命中规则：精炼缓存直接复用；降级缓存仅在「本次也接受降级」或
        「LLM 不可用」时复用，否则重建精炼版——stub 先建降级、llm 后自动升级。
        """
        cached = None
        if not force and self.bible_path.exists() and self.chapters_path.exists():
            cached = self._load()
        if cached is not None:
            accept = (not cached["degraded"]) or degrade or not self._llm_ok()
            if accept:
                logger.info("novel_index 命中缓存: %s (%d 章)", self.ref, cached["n"])
                return cached
            logger.info("novel_index 降级缓存将升级为 LLM 精炼圣经: %s", self.ref)

        full = self._read_full()
        if _is_error(full):
            raise NovelIndexError(full)
        total = len(full)
        chapters = self._split(full)
        if not chapters:
            raise NovelIndexError(f"原著为空或无法切章: {self.ref}")

        # 1) 章节索引（启发式：标题 + 开头 200 字，零 LLM）
        index = [
            {
                "idx": i + 1,
                "title": title,
                "chars": len(body),
                "head": body[:200],
            }
            for i, (title, body) in enumerate(chapters)
        ]
        # 2) 改编圣经（LLM 精炼 / 无 LLM 降级）
        bible, degraded = self._build_bible(full, chapters, degrade=degrade)

        self._save(bible, index, total, degraded)
        logger.info(
            "novel_index 建立完成: %s (%d 字, %d 章, %s)",
            self.ref, total, len(chapters), "LLM 圣经" if not degraded else "降级索引",
        )
        return {
            "bible": bible,
            "chapters": index,
            "total_chars": total,
            "n": len(chapters),
            "degraded": degraded,
        }

    # -- 读取 ------------------------------------------------------------
    def get_bible(self) -> str:
        """取改编圣经文本；未建索引则先建（内部调 build）。"""
        if not self.bible_path.exists():
            self.build()
        return self.bible_path.read_text(encoding="utf-8")

    def get_meta(self) -> dict | None:
        if not self.chapters_path.exists():
            return None
        return self._load()

    def chapter_toc(self) -> str:
        """章节目录文本（含各章字数），供 prompt 提示模型可引用章节。"""
        meta = self.get_meta()
        if not meta:
            return ""
        lines = [f"《{Path(self.ref).stem}》共 {meta['n']} 章，全文 {meta['total_chars']:,} 字："]
        lines += [f"  {c['idx']}. {c['title']}（{c['chars']:,} 字）" for c in meta["chapters"]]
        return "\n".join(lines)

    def pick_chapters(self, topic: str, k: int = 3) -> list[dict]:
        """按主题挑最相关章节，返回 [{idx, title, chars, body}]（正文完整读入）。

        用 原词+中文bigram 在「标题+开头 200 字」上计分取 top-k；无命中返回空列表，
        调用方自行降级（用圣经或开头几章）。
        """
        meta = self.get_meta()
        if not meta:
            return []
        grams = _topic_grams(topic)
        if not grams:
            return []
        scored: list[tuple[int, dict]] = []
        for ch in meta["chapters"]:
            hay = (ch.get("title") or "") + (ch.get("head") or "")
            score = sum(1 for g in grams if g in hay)
            if score:
                scored.append((score, ch))
        scored.sort(key=lambda t: (-t[0], t[1]["idx"]))
        out: list[dict] = []
        for _, ch in scored[:k]:
            body = self._read_chapter(ch["idx"])
            if body and not _is_error(body):
                out.append(
                    {"idx": ch["idx"], "title": ch["title"], "chars": ch["chars"], "body": body}
                )
        return out

    def get_chapter(self, idx: int) -> dict | None:
        """按章节序号取章节（标题+全文）。越界/读取失败返回 None。"""
        meta = self.get_meta()
        if not meta:
            return None
        ch = next((c for c in meta["chapters"] if c["idx"] == idx), None)
        if ch is None:
            return None
        body = self._read_chapter(idx)
        if not body or _is_error(body):
            return None
        return {"idx": ch["idx"], "title": ch["title"], "chars": ch["chars"], "body": body}

    # -- 内部 ------------------------------------------------------------
    def _llm_ok(self) -> bool:
        try:
            from deskmate.memory.llm import llm_available

            return bool(llm_available())
        except Exception:  # noqa: BLE001
            return False

    def _read_full(self) -> str:
        from deskmate.perception.file_reader import read_full_text

        # 建索引是离线批处理，显式放大大小上限（默认 2MB 是对话安全值，装不下长篇）
        return read_full_text(self.ref, max_bytes=64 * 1024 * 1024)

    def _split(self, full: str) -> list[tuple[str, str]]:
        from deskmate.perception.file_reader import split_chapters

        return split_chapters(full)

    def _read_chapter(self, idx: int, max_chars: int = 30_000) -> str:
        from deskmate.perception.file_reader import read_chapter

        # 建索引要的是纯章节正文：覆盖率标注是给「会下判断的模型」看的对话层提示，
        # 混进 body 会污染圣经索引 / 语义检索（2026-11-11 地基）。
        return read_chapter(self.ref, idx, max_chars=max_chars, coverage=False)

    def _build_bible(self, full: str, chapters: list[tuple[str, str]], *, degrade: bool = False) -> tuple[str, bool]:
        try:
            from deskmate.memory.llm import chat, llm_available
        except Exception:  # noqa: BLE001
            llm_available = lambda: False  # type: ignore[assignment]
            chat = lambda **_: ""  # type: ignore[assignment]
        if degrade or not llm_available():
            return self._fallback_bible(chapters), True

        sample = _sample_chapters(chapters)
        title = Path(self.ref).stem
        user = (
            f"原著《{title}》：全文 {len(full):,} 字，共 {len(chapters)} 章。\n"
            f"以下为原著采样（全文 或 逐章「开头+中段+结尾」节选，非全文）：\n\n{sample}\n\n"
            f"请基于以上采样提炼《改编圣经》，输出 markdown，含：\n"
            f"1. 人物表：姓名/身份/性格/立场/人物关系；每名主要人物必须给出"
            f"【核心目标】【内心动机】【性格弧光（起点→终点）】【关键冲突关系】\n"
            f"2. 主线情节线：按幕/阶段划分，标注大致对应章节；说明每一阶段由"
            f"【何种动机驱动人物行动】\n"
            f"3. 核心主题与母题\n"
            f"4. 关键场景清单：标注所在章节，并说明各场景的【戏剧功能】"
            f"（激励事件/转折/真相揭示/人物抉择）\n"
            f"5. 悬念与暗线：伏笔、疑点、死亡/冤案/阴谋、未解之谜——"
            f"凡是原著中后段才揭示的真相，必须在此捕捉，不得遗漏\n"
            f"6. 改编注意事项：哪些是必须保留的原著骨架，尤其【人物动机与弧光】"
            f"【核心悬念】不可改动"
        )
        # 重试上限：LLM provider（如 mimo）会间歇性返回空 content 且 finish=stop，
        # 若不加重试，圣经会静默降级为章节索引，人物动机/弧光全部丢失。
        BIBLE_CHAT_RETRIES = 3
        last_exc: Exception | None = None
        for attempt in range(1, BIBLE_CHAT_RETRIES + 1):
            try:
                bible = chat(
                    system="你是资深文学编辑与影视改编策划。基于原著采样提炼「改编圣经」，"
                    "供编剧团队改编参考。提炼的重心是驱动改编的深层信息——人物动机、"
                    "性格弧光、悬念暗线、真相疑点——而不是表层事件流水；"
                    "宁可多记动机与悬念，也不要漏掉中后段揭示的关键设定。"
                    "输出简洁、结构化、可执行，不要复述原文。",
                    user=user,
                    max_tokens=BIBLE_MAX_TOKENS,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("圣经 LLM 生成第 %d 次异常: %s", attempt, exc)
                continue
            if bible and bible.strip():
                return f"# 《{title}》改编圣经\n\n{bible.strip()}\n", False
            logger.warning("圣经 LLM 第 %d 次返回空，重试…", attempt)
        logger.warning("圣经 LLM 生成失败（重试 %d 次后仍为空/异常），降级为章节索引: %s",
                       BIBLE_CHAT_RETRIES, last_exc)
        return self._fallback_bible(chapters), True

    def _fallback_bible(self, chapters: list[tuple[str, str]]) -> str:
        """LLM 未配置/失败时的降级圣经：章节目录 + 各章开头采样，至少给全局轮廓。"""
        parts = ["（降级：LLM 未配置，以下为章节索引圣经——各章标题与开头采样）"]
        budget = FALLBACK_BIBLE_CHARS
        for i, (title, body) in enumerate(chapters, 1):
            snip = body[:300].strip()
            piece = f"第{i}章 {title}\n{snip}"
            if budget - len(piece) < 0 and len(parts) > 1:
                break
            parts.append(piece)
            budget -= len(piece)
        return "\n\n".join(parts) + "\n"

    def _save(self, bible: str, index: list[dict], total: int, degraded: bool) -> None:
        _atomic_write(self.bible_path, bible)
        payload = {
            "total_chars": total,
            "n": len(index),
            "degraded": degraded,
            "chapters": index,
        }
        _atomic_write(
            self.chapters_path,
            json.dumps(payload, ensure_ascii=False, indent=1),
        )

    def _load(self) -> dict:
        bible = self.bible_path.read_text(encoding="utf-8")
        payload = json.loads(self.chapters_path.read_text(encoding="utf-8"))
        chapters = payload["chapters"]
        return {
            "bible": bible,
            "chapters": chapters,
            "total_chars": payload.get("total_chars", sum(c.get("chars", 0) for c in chapters)),
            "n": payload.get("n", len(chapters)),
            "degraded": payload.get("degraded", "降级" in bible[:40]),
        }


# -- 模块级便捷函数（pipeline / executor 按需调用） -------------------------


def get_bible(ref: str, *, cache_root: Path | None = None) -> str:
    """取改编圣经文本；无缓存则自动建索引（可能触发一次 LLM 调用）。"""
    return NovelIndex(ref, cache_root=cache_root).get_bible()


def build_index(ref: str, *, force: bool = False, cache_root: Path | None = None) -> dict:
    """建立（或复用）索引。返回 meta 字典。"""
    return NovelIndex(ref, cache_root=cache_root).build(force=force)


def pick_chapters(ref: str, topic: str, k: int = 3, *, cache_root: Path | None = None) -> list[dict]:
    """按主题挑最相关章节（自动建索引）。"""
    return NovelIndex(ref, cache_root=cache_root).pick_chapters(topic, k=k)
