"""P1-6 圣经回流——创作定稿后把该章「新出现的关键信息」回流进改编圣经。

核心：让「越写越懂你的书」可感知、可追溯（8.0 红线：可见可回滚）。

- 输入：一次定稿的产出文本（章节/大纲正文）+ 原著 ref（定位该小说的 bible.md）
- 动作：纯规则提取该章新出现的候选专名/关键词（对照圣经已有文本去重）→
  追加进 ``bible.md`` 的「创作回流」区（来源章节 + 时间 + 候选词）→
  写回流日志 ``bible_feedback.json``（novel/episode/entities/ts，前端可读、可回滚）。
- 幂等：同一 novel+episode 只回流一次（重跑不重复追加）。
- 零 LLM、fail-open、不阻塞创作流程。

注：提取的是「新出现的关键词候选」（诚实叫法，不是精确实体抽取）——
无 LLM 场景下这是可控的最小回流；后续接 LLM 可升级为实体级 upsert。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

def default_cache_root() -> Path:
    """默认缓存根（与 NovelIndex.default_cache_root() 同源）——**调用时**解析。

    禁模块级常量（2026-09-22 收口）：import 期绑定会让隔离失效。
    """
    from deskmate.paths import deskmate_base_dir

    return deskmate_base_dir() / "novels"

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

# 常见虚词/通用词（过滤，避免「的了我们」之类进回流）
_STOPWORDS = set(
    """的了是在我你他她它我们你们他们咱们这那这个那个这些那些一个没有什么怎么
    这样那样已经还是因为所以然后但是如果就是真的非常可能知道时候东西事情自己
    现在大家所有还有只是通过为了关于可以需要觉得感觉应该能够或者而且并且其中
    比如比如有时正在开始结束继续突然终于一直很更最也会把被让对从向给跟和与又
    再也还要只要只有虽然不管如果不过要么其次另外由于按照经过随着成为成为并却
    其实大概几乎似乎好像仿佛并不没有不要不能不会可以应该必须同样自然真正整个
    完全突然忽然照样依旧曾经一直常常偶尔反复再次每次每日每周每月每年上午下午
    晚上早上中午凌晨夜里昨天今天明天后天之前之后以后过去未来现在如今当今目前
    刚刚马上立刻逐渐渐渐缓缓慢慢快快急急究竟到底到底难道何况况且何况不仅不单""".split()
)


def _cache_key(ref: str) -> str:
    """与 NovelIndex 同款 ref → 缓存目录名（稳定、可移植）。"""
    import hashlib

    return hashlib.md5(ref.strip().encode("utf-8")).hexdigest()[:16]


def bible_path_for(
    novel_ref: str, cache_root: Path | str | None = None
) -> Path | None:
    """定位一部原著的改编圣经路径；找不到（未建索引）返回 None。"""
    if not novel_ref:
        return None
    root = Path(cache_root) if cache_root else default_cache_root()
    return root / _cache_key(novel_ref) / "bible.md"


def _feedback_log_path(novel_ref: str, cache_root: Path | None = None) -> Path:
    root = cache_root or default_cache_root()
    return root / _cache_key(novel_ref) / "bible_feedback.json"


def _load_log(novel_ref: str, cache_root: Path | None = None) -> list[dict]:
    p = _feedback_log_path(novel_ref, cache_root)
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        return []
    return []


def extract_candidate_entities(text: str, known_text: str = "") -> list[str]:
    """纯规则提取「新出现的关键词候选」。

    2-4 字中文连续串 → 过滤停止词 → 过滤圣经已有关键词 → 按出现频率降序。
    返回空列表也是合法结果（该章没有明显新信息时静默跳过）。
    """
    if not text:
        return []
    freq: dict[str, int] = {}
    for run in _CJK_RUN.findall(text):
        n = len(run)
        # 滑动窗口：每段中文连续串的所有 2-4 字子串（避免 {2,4} 贪心切碎长词）
        for i in range(n):
            for L in (2, 3, 4):
                if i + L > n:
                    break
                w = run[i : i + L]
                if w in _STOPWORDS:
                    continue
                freq[w] = freq.get(w, 0) + 1
    known = set(_CJK_RUN.findall(known_text or "")) if known_text else set()
    # 已知词也拆 2-4 子串参与去重（否则「李慕白」会被拆出的「慕白」漏掉）
    known_grams: set[str] = set()
    for run in known:
        for i in range(len(run)):
            for L in (2, 3, 4):
                if i + L > len(run):
                    break
                known_grams.add(run[i : i + L])
    # 首/尾虚词过滤：滤掉「上的」「与提」这类句法碎片（专名不以虚词起止）
    head_filler = "与和的了在上下是其于以为把被对从跟向给因由而据按"
    tail_filler = "的了着在上下与和是之其于以为把被对从跟向给因由而"
    fresh = sorted(
        (
            (w, c)
            for w, c in freq.items()
            if w not in known_grams
            and w[0] not in head_filler
            and w[-1] not in tail_filler
        ),
        key=lambda kv: (-kv[1], kv[0]),
    )
    return [w for w, _c in fresh[:8]]


def record_bible_feedback(
    novel_ref: str,
    episode: str | int,
    content: str,
    cache_root: Path | str | None = None,
    source: str = "",
) -> dict[str, Any]:
    """把一次定稿产出的回流写进圣经（幂等，按 novel+episode 去重）。

    Returns:
        {"ok": bool, "skipped": bool, "novel": str, "episode": str,
         "entities": [...], "bible_path": str|None, "reason": str}
    """
    root = Path(cache_root) if cache_root else default_cache_root()
    if not novel_ref or not content:
        return {"ok": False, "skipped": True, "reason": "缺少 novel_ref 或产出文本"}
    ep = str(episode)

    # 幂等：同 novel+episode 已回流过则跳过
    log = _load_log(novel_ref, root)
    if any(str(e.get("episode")) == ep for e in log):
        return {
            "ok": True,
            "skipped": True,
            "novel": novel_ref,
            "episode": ep,
            "entities": [],
            "reason": "该章节已回流",
        }

    bpath = bible_path_for(novel_ref, root)
    if bpath is None or not bpath.is_file():
        return {"ok": False, "skipped": True, "reason": "bible.md 不存在（未建索引）"}

    try:
        bible_text = bpath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"ok": False, "skipped": True, "reason": "bible.md 读取失败"}

    entities = extract_candidate_entities(content, bible_text)
    entry = {
        "novel": novel_ref,
        "episode": ep,
        "entities": entities,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source or "",
    }

    # 追加到 bible.md 创作回流区（幂等：有「## 创作回流」则不重复建标题）
    block = (
        f"- 第{ep}集 · {entry['ts']}"
        + (f"（{source}）" if source else "")
        + f"：新增关键词 {', '.join(entities) if entities else '（无显著新信息）'}"
    )
    if "## 创作回流" in bible_text:
        new_bible = bible_text.rstrip() + "\n" + block + "\n"
    else:
        new_bible = bible_text.rstrip() + "\n\n## 创作回流\n\n" + block + "\n"
    try:
        bpath.write_text(new_bible, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "skipped": True, "reason": f"bible.md 写入失败: {exc}"}

    # 写回流日志（回滚依据）
    log.append(entry)
    try:
        _feedback_log_path(novel_ref, root).write_text(
            json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError:
        pass  # 日志失败不阻断（bible 已回流）

    return {
        "ok": True,
        "skipped": False,
        "novel": novel_ref,
        "episode": ep,
        "entities": entities,
        "bible_path": str(bpath),
    }


def list_feedback(
    novel_ref: str, cache_root: Path | str | None = None
) -> list[dict]:
    """读取一部原著的回流日志（前端展示用；空列表表示尚无回流）。"""
    root = Path(cache_root) if cache_root else default_cache_root()
    return _load_log(novel_ref, root)


if __name__ == "__main__":
    import sys

    ref = sys.argv[1] if len(sys.argv) > 1 else ""
    if not ref:
        print("用法: python -m deskmate.creation.bible_feedback <原著路径> [episode] [文本]")
        sys.exit(0)
    ep = sys.argv[2] if len(sys.argv) > 2 else "1"
    text = sys.argv[3] if len(sys.argv) > 3 else "第1集：少年李慕白在长安城偶得玄铁令。"
    print(json.dumps(record_bible_feedback(ref, ep, text), ensure_ascii=False, indent=1))
