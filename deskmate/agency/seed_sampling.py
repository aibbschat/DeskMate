"""种子取样：来源分流（A）+ 话题去饱和（B）。

【为什么有这个模块】2026-09-18 诊断出的自我引用闭环——
``ai_circle_column.record_self_utterance()`` 把小桔**自己发出去的**帖子和手记
按 ``source=self`` 回流进**同一个**理解池 ``agency/observation_understanding.jsonl``
（09-16 加的"连续感"设计，本身是对的）。但手记/短帖的 seed 是"从这个池子取
最后 N 条"：池子被自家产物灌满后，她其实是在**读自己前几天写的感悟**再写一遍。

实测（09-18 20:15）：池共 152 条，self 80 条；**文件末尾连续 53 条全是 self**，
最后一条外部内容被挤到倒数第 54 位；10 条种子实测 7 条提同一条新闻
（脑机接口医疗器械标准，而那条新闻 09-15 只进来过一次）。

两道闸：

**A 来源分流**（``load_candidates`` 的并入 + ``pick_seed_entries`` 的 self_ratio）
- self（她的旧发言）在种子里最多占 ``self_ratio``，其余名额留给外部素材；
- 候选池**显式并入**最近 ``lookback_days`` 的非 self 条目。只限流是不够的：
  真素材被挤在"最后 N 条"窗口**之外**时，怎么限流都取不到它。

**B 话题去饱和**（``topic_anchors`` + ``recent_anchor_sets``）
- 记下最近 ``saturation_rounds`` 次**真正发出去**的发言的话题锚点；
- 候选与其中任一条锚点重合度 ≥ ``topic_overlap_threshold`` → 判为"又在说同一件事"，
  排到候选末尾（**降权，不硬丢**——观点长出来不靠规则逼）。
- 锚点 = 词频 top-K 内容二元组（去功能字）。参数是在真实池子上量出来的：
  ``K=15 / 阈值 0.25`` 时，脑机旧帖 6/6 命中、脑机**原始新闻** 1/1 命中、
  无关话题 0 误伤（对照见 ``tests/test_seed_sampling.py``）。
  换全量二元组做同一件事**认不出原始新闻**（重合度只有 0.08）——长文本会把
  话题词淹没，所以必须取 top-K 小集合。
- 锚点集刻意与**回流集**对齐（都只在真发出去时记）：B 因此直接对冲她自己的回声，
  而不是泛泛地"惩罚重复话题"。

⚠ 边界（诚实说清）：A+B 治的是"同一条新闻在每篇手记里都出现"。当她的产出速率
（实测每小时顶格 5 条短帖）远高于外部新素材速率（看世界每天 5-13 条）时，
话题仍会轮转——那是发布频率的事，不是取样的事。B 的 ``saturation_rounds``
调大能让轮转更慢，但调过头会把候选全判饱和，fail-open 补足后 B 等于失效。

fail-open 贯穿：任何异常都退化成旧行为（取最后 N 条），绝不让种子变空。
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterator, Optional

try:
    import fcntl  # Unix 文件锁（跨进程串行）；Windows 下不可用，降级为仅进程内锁
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "self_ratio": 0.34,               # self（她的旧发言）在种子里最多占的比例
    "saturation_rounds": 4,           # 用最近多少次产出的锚点判「同话题」
    "topic_overlap_threshold": 0.25,  # 重合度 ≥ 此值 → 判为同话题（降权）
    "anchor_k": 15,                   # 每条取词频 top-K 个内容二元组作锚点
    "lookback_days": 3,               # 候选池显式并入最近几天的非 self 条目
    "max_non_self": 24,               # 并入的非 self 条目上限
    "_max_history": 64,               # 锚点历史最多保留多少轮
}

_SELF_SOURCE = "self"

# 最少锚点数：两边任一少于这个数就不做重合判断（短文本 min 当分母会虚高）
_MIN_ANCHORS = 6

# 中文功能字：含其中任一字的二元组一律不当锚点（"的医"/"机的"这类噪音）。
_FUNC_CHARS = set(
    "的一了是我你他她它们在不有和就都而及与着或这那很上下里外个们什么吗呢吧啊呀哦嗯"
    "太也才又再能被把给对从到但对于并且所以因若虽然然后已可将会没别还要想让"
)

# 常见通用二元组：语义上不构成话题，避免它们把两个不相干的文本拉到一起。
_GENERIC_GRAMS = {
    "时候", "一个", "我们", "你们", "他们", "自己", "什么", "怎么", "这样", "那样",
    "一些", "一样", "一直", "一定", "就是", "不是", "可以", "已经", "因为", "所以",
    "但是", "虽然", "然后", "而且", "或者", "如果", "这个", "那个", "这些", "那些",
    "没有", "知道", "觉得", "好像", "也是", "还是", "现在", "今天", "昨天", "明天",
    "开始", "发现", "其实", "结果", "听到", "看到", "这种", "那种",
}

_LOCK_NAME = "seed_topic_history.lock"
_HISTORY_NAME = "seed_topic_history.jsonl"
_UNDERSTANDING_NAME = "observation_understanding.jsonl"

# 池子里的自动化样板：定时任务的运行记录被摄入流水线提炼后落进理解池，
# 进种子只会占额度（"⏰ Scheduled run — 进度跟踪：国子监秘闻录  This automation
# is due now: carry out the t..."）。并入外部候选后这类条目会被顶到窗口前面，
# 所以在取样这一层直接丢掉。
_NOISE_MARKERS = ("⏰ Scheduled run", "This automation is due now")

_local_lock = threading.Lock()


def _is_noise(text: str) -> bool:
    return any(m in (text or "") for m in _NOISE_MARKERS)


def cfg_of(cfg: Optional[dict]) -> dict[str, Any]:
    """内置默认 + 用户配置（只认已知 key）。"""
    out = dict(DEFAULTS)
    if isinstance(cfg, dict):
        out.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    return out


def _bool(v: Any, default: bool) -> bool:
    return bool(v) if isinstance(v, bool) else default


# ---------------------------------------------------------------------------
# 基础：来源判定 / 文本
# ---------------------------------------------------------------------------
def is_self(rec: Any) -> bool:
    """是不是「她自己的发言回流」（source=self，见 record_self_utterance）。"""
    return isinstance(rec, dict) and str(rec.get("source") or "") == _SELF_SOURCE


def entry_text(rec: Any) -> str:
    return str(rec.get("body") or "").strip() if isinstance(rec, dict) else ""


def _rec_key(rec: dict) -> str:
    return hashlib.md5(entry_text(rec).encode("utf-8")).hexdigest()


def _rec_date(rec: dict) -> Optional[date]:
    """条目日期：优先 date 字段，退回 ts（epoch 秒）。拿不到返回 None。"""
    raw = str(rec.get("date") or "").strip()
    if raw:
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    ts = rec.get("ts")
    try:
        return datetime.fromtimestamp(float(ts)).date()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# B 的零件：话题锚点 / 重合度
# ---------------------------------------------------------------------------
def topic_anchors(text: str, k: int = 15) -> set:
    """抽话题锚点：词频 top-K 的内容二元组（去功能字/通用词）+ 英文词。

    ⚠ 必须取 top-K 小集合、不能拿全量二元组：全量时长文本会把话题词淹没——
    实测"原始新闻 vs 她的旧帖"全量重合度只有 0.08（认不出是同一件事），
    top-15 后升到 0.27（能认出来）。
    """
    t = (text or "").lower()
    if not t:
        return set()
    counts: collections.Counter = collections.Counter()
    first: dict[str, int] = {}
    for i in range(len(t) - 1):
        a, b = t[i], t[i + 1]
        if "\u4e00" <= a <= "\u9fff" and "\u4e00" <= b <= "\u9fff":
            g = a + b
            if a in _FUNC_CHARS or b in _FUNC_CHARS or g in _GENERIC_GRAMS:
                continue
            counts[g] += 1
            first.setdefault(g, i)
    for w in re.findall(r"[a-z]{3,}", t):
        counts[w] += 1
        first.setdefault(w, 0)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], first.get(kv[0], 0)))
    return {g for g, _ in ranked[: max(1, int(k))]}


def overlap_ratio(a: set, b: set) -> float:
    """重合度 = 交集 / 较小集合（containment）。任一边太短则不做判断（返回 0）。"""
    if not a or not b or min(len(a), len(b)) < _MIN_ANCHORS:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def max_overlap(anchors: set, recent: list[set]) -> float:
    """与最近几轮产出的最大重合度。"""
    best = 0.0
    for r in recent:
        v = overlap_ratio(anchors, r)
        if v > best:
            best = v
    return best


# ---------------------------------------------------------------------------
# 话题词（B 的升维）：从「同一条新闻」升到「同一个话题」
# —— 设计意图（萧哥 2026-09-20 拍板）：回流让她「记得自己写过什么、能连上」是
#    故意保留的；要治的是「记得却重复写」。所以这里只降权「同话题词」，不删回流。
# ---------------------------------------------------------------------------
# 永远在场的人称/自称/通用词，不当话题词（否则会误伤「萧盛」「自己」等常态词）。
_THEME_STOP = {
    "萧盛", "萧哥", "主人", "小桔", "手记", "读者", "圈里", "自己", "我们", "他们",
    "你们", "今天", "昨天", "明天", "现在", "最近", "一篇", "这条", "那条", "一个",
    "这种", "那种", "时候", "看到", "听到", "觉得", "好像", "其实", "结果", "发现",
    "新闻", "标准", "模型", "数据", "平台", "算法", "内容", "故事", "小说", "历史",
    "记忆", "未来", "世界", "生活", "真的", "还是", "已经", "可以", "知道",
}


# 她的贯穿隐喻词（线/底座/边界/看），不当话题词——否则每篇都含 → 全量降权。
_SIGNATURE = set("线底座边界看")


def theme_tokens(text: str, max_tokens: int = 30) -> set:
    """抽「话题词」（2 字滑动窗，与 topic_anchors 同源性，但用「集合相交」判同话题）。

    topic_anchors 用比例重叠认「同一条新闻」；这里用相交认「同一类事」——
    红果短剧日活 / AI微短剧12.2万部 / 全行业3.3万部 都含二元组「短剧」，相交即命中，
    把话题级复读（短剧/脑机接口/国子监…）统一降权。通用词与她的招牌隐喻词进停用集，
    避免误伤或全量降权。fail-open：抽不到返回空。
    """
    t = (text or "")
    out: set[str] = set()
    for i in range(len(t) - 1):
        a, b = t[i], t[i + 1]
        if "\u4e00" <= a <= "\u9fff" and "\u4e00" <= b <= "\u9fff":
            g = a + b
            if (
                any(ch in _SIGNATURE for ch in g)
                or a in _FUNC_CHARS or b in _FUNC_CHARS
                or g in _GENERIC_GRAMS or g in _THEME_STOP
            ):
                continue
            out.add(g)
    return set(list(out)[:max_tokens])


def recent_theme_tokens(base_dir: str, rounds: int) -> set:
    """最近 rounds 次产出里出现过的「话题词」并集（供话题级去饱和）。fail-open → set()。"""
    n = max(1, int(rounds))
    out: set[str] = set()
    try:
        for rec in load_rounds(base_dir, limit=n):
            th = rec.get("themes")
            if isinstance(th, list):
                out.update(str(x) for x in th)
    except Exception as e:  # pragma: no cover - 防御性
        logger.debug("seed_sampling: 读话题词失败（忽略）: %s", e)
    return out


# ---------------------------------------------------------------------------
# 锚点历史（跨进程串行；不同入口：L2 tick / GUI POST / MCP）
# ---------------------------------------------------------------------------
@contextmanager
def _exclusive(base_dir: str) -> Iterator[None]:
    """跨进程排他锁。锁文件与数据文件分开，好让数据文件能 os.replace。"""
    with _local_lock:
        path = os.path.join(base_dir, "agency", _LOCK_NAME)
        fh = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fh = open(path, "a+")
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except Exception as e:  # 拿不到跨进程锁也不拦业务（进程内锁已生效）
            logger.debug("seed_sampling: 取锁失败（仅进程内串行）: %s", e)
        try:
            yield
        finally:
            if fh is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                fh.close()


def _history_path(base_dir: str) -> str:
    return os.path.join(base_dir, "agency", _HISTORY_NAME)


def _atomic_write(path: str, lines: list[str]) -> None:
    """原子写（临时文件名必须唯一：pid+线程号，多写入者互踩会丢整次写入）。"""
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("".join(lines))
    os.replace(tmp, path)


def load_rounds(base_dir: str, *, limit: int = 0) -> list[dict]:
    """读已记的产出轮（时间正序）。fail-open → []。"""
    p = _history_path(base_dir)
    if not os.path.isfile(p):
        return []
    out: list[dict] = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("anchors"):
                    out.append(rec)
    except Exception as e:
        logger.debug("seed_sampling: 读锚点历史失败（忽略）: %s", e)
        return []
    return out[-limit:] if limit and limit > 0 else out


def recent_anchor_sets(base_dir: str, rounds: int) -> list[set]:
    """最近 rounds 次产出的锚点集合（供去饱和比对）。fail-open → []。"""
    n = max(1, int(rounds))
    sets: list[set] = []
    for rec in load_rounds(base_dir, limit=n):
        a = rec.get("anchors")
        if isinstance(a, list) and a:
            sets.append({str(x) for x in a})
    return sets


def record_round(base_dir: str, text: str, channel: str = "",
                 cfg: Optional[dict] = None) -> bool:
    """记一轮**真发出去**的产出（抽锚点 → 供后续去饱和）。fail-open。

    ⚠ 刻意只在"真发出去"时调用，与 ``record_self_utterance`` 同点位：
    回流进池子的正是这些发言，所以 B 的锚点集 == 回声集，直接对冲回声。
    """
    body = (text or "").strip()
    if not body:
        return False
    c = cfg_of(cfg)
    anchors = sorted(topic_anchors(body, int(c.get("anchor_k", 15))))
    themes = sorted(theme_tokens(body))
    if not anchors and not themes:
        return False
    p = _history_path(base_dir)
    keep = max(2, int(c.get("_max_history", 64)))
    try:
        with _exclusive(base_dir):
            old = []
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    old = [ln.strip() for ln in f if ln.strip()]
            line = json.dumps(
                {"ts": time.time(), "channel": str(channel or ""),
                 "anchors": anchors, "themes": themes},
                ensure_ascii=False,
            )
            lines = (old + [line])[-keep:]
            os.makedirs(os.path.dirname(p), exist_ok=True)
            _atomic_write(p, [ln + "\n" for ln in lines])
        return True
    except Exception as e:
        logger.debug("seed_sampling: 记锚点失败（忽略）: %s", e)
        return False


# ---------------------------------------------------------------------------
# A 的零件：候选池（并入最近的非 self 条目）
# ---------------------------------------------------------------------------
def _read_jsonl(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and entry_text(rec):
                    out.append(rec)
    except Exception as e:
        logger.debug("seed_sampling: 读理解池失败（忽略）: %s", e)
    return out


def load_candidates(base_dir: str, *, recent_n: int,
                    lookback_days: int = 3, max_non_self: int = 24) -> list[dict]:
    """理解池候选 = 最近 ``recent_n`` 条（旧行为）**并入**最近几天的非 self 条目。

    并入是 A 的关键一步：她的帖子把池子末尾占满后（实测连续 53 条），
    真素材被挤在"最后 N 条"窗口**之外**——只限流不并池，根本取不到它。
    返回按文件顺序（≈时间正序）。
    """
    recs = _read_jsonl(os.path.join(base_dir, "agency", _UNDERSTANDING_NAME))
    recs = [r for r in recs if not _is_noise(entry_text(r))]
    if not recs:
        return []

    n = int(recent_n)
    out = list(recs[-n:]) if n > 0 else list(recs)
    seen = {_rec_key(r) for r in out}

    non_self = [r for r in recs if not is_self(r)]
    cutoff = date.today() - timedelta(days=max(0, int(lookback_days)))
    fresh = [r for r in non_self if (_rec_date(r) or date.min) >= cutoff]
    # 保底：一天新素材都没有时退用最近的非 self——宁可提旧闻，也不让她只读自己
    pool = fresh or non_self
    for r in pool[-max(1, int(max_non_self)):]:
        k = _rec_key(r)
        if k not in seen:
            seen.add(k)
            out.append(r)

    order = {_rec_key(r): i for i, r in enumerate(recs)}
    out.sort(key=lambda r: order.get(_rec_key(r), 0))
    return out


# ---------------------------------------------------------------------------
# 主入口：挑种子（A 来源分流 + B 去饱和）
# ---------------------------------------------------------------------------
def pick_seed_entries(candidates: list[dict], n: int, *, base_dir: str,
                      cfg: Optional[dict] = None) -> list[dict]:
    """从候选里挑 n 条种子。fail-open 到旧行为（取最后 n 条）。

    选择优先级（桶内都按"越近越优先"）：
      先给外部素材留 ``n - max_self`` 席；self 只吃剩下的、且不超过 ``max_self``；
    还没满就继续从外部加，仍然不满才让 self 超出上限（**宁可让她多说一句，
    也不空种**）。输出按「新鲜话题在前、刚写过的话题沉尾」：同话题候选整体排在
    非重复候选之后（降权但不硬丢，观点长出来不靠规则逼），两组内各自时间正序。
    """
    n = int(n)
    if n <= 0 or not candidates:
        return []

    c = cfg_of(cfg)
    if not _bool(c.get("enabled"), True):
        return list(candidates[-n:])

    try:
        recent = recent_anchor_sets(base_dir, c.get("saturation_rounds", 4))
        # 话题级去饱和（升维）：最近几轮写过的「话题词」并集——同话题词直接降权，
        # 堵住「同一类事换不同事实」的复读（红果短剧/AI微短剧/全行业短剧 都含「短剧」）。
        # 不碰 record_self_utterance：她仍「记得自己写过什么、能连上」，只是不再重写。
        recent_themes = recent_theme_tokens(base_dir, c.get("saturation_rounds", 4))
        thr = float(c.get("topic_overlap_threshold", 0.25))
        k = int(c.get("anchor_k", 15))
        saturated = [
            (max_overlap(topic_anchors(entry_text(r), k), recent) >= thr)
            or bool(theme_tokens(entry_text(r)) & recent_themes)
            for r in candidates
        ]

        def by_recency_first(idxs: list[int]) -> list[int]:
            fresh = [i for i in idxs if not saturated[i]]
            stale = [i for i in idxs if saturated[i]]
            return fresh[::-1] + stale[::-1]

        self_ordered = by_recency_first(
            [i for i, r in enumerate(candidates) if is_self(r)]
        )
        nonself_ordered = by_recency_first(
            [i for i, r in enumerate(candidates) if not is_self(r)]
        )

        ratio = float(c.get("self_ratio", 0.34))
        max_self = max(0, min(n, int(n * ratio)))
        budget_nonself = n - max_self

        picked = nonself_ordered[:budget_nonself]
        picked += self_ordered[:max_self]
        if len(picked) < n:                     # 外部还有 → 继续加外部
            picked += nonself_ordered[budget_nonself:]
        if len(picked) < n:                     # 还不够 → self 才超上限
            picked += self_ordered[max_self:]
        if len(picked) < n:                     # 理论上到不了，稳一手
            chosen = set(picked)
            picked += [i for i in range(len(candidates))
                       if i not in chosen][: n - len(picked)]

        # ⚠ 输出序：把候选切成「非重复(新鲜) / 同话题(刚写过)」两组，新鲜组在前、
        # 重复组整体沉到末尾——**所有**刚写过的话题都排在新鲜话题之后（降权、不硬丢）；
        # 每组内各自按时间正序（旧→新），整体读起来是「最近的、没写过的事先讲，
        # 写过的话题收尾」。之前这里写成 `sorted(picked)` 按索引重排，会把上面的
        # 沉尾打回原样、去饱和完全失效（单候选看不出，多候选才暴露）。
        picked = list(dict.fromkeys(picked))[:n]
        non_sat = [i for i in picked if not saturated[i]]
        sat = [i for i in picked if saturated[i]]
        return [candidates[i] for i in sorted(non_sat) + sorted(sat)]
    except Exception as e:
        logger.warning("seed_sampling: 取样异常（退回取最后 n 条）: %s", e)
        return list(candidates[-n:])


def selection_summary(picked: list[dict]) -> str:
    """给 task_log / 面板的一行摘要，例：``7条(外部5/自述2)``。"""
    selfn = sum(1 for r in picked if is_self(r))
    return f"{len(picked)}条(外部{len(picked) - selfn}/自述{selfn})"
