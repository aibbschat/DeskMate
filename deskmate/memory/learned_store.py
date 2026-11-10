"""LearnedStore：长期记忆（「越用越懂你」）的 SQLite 持久层。

取代原 ``memory/learned_history.jsonl`` 单文件追加读，收口进统一库
``~/.deskmate/deskmate.db``（与认知层 CognitiveStore、日记 DiaryStore、
profile 类 JSON 同库），**备份即同一原子单元**。

为什么收（2026-09-02 实测数据）：
- 长期记忆**只增不减**：211 条 / 25 个活跃日 ≈ 8.4 条/天，外推 ~2500 条/年。
- 原实现每次读取都要 ``read_text()`` 全量进内存再逐行 ``json.loads``，
  且 **一次请求解析两遍**（取最近 N 条一遍、全量统计又一遍）。
  体量小时无感，但这是只增不减的数据，解析成本随条数线性上涨。
- 统计（total / by_category）本该是 ``COUNT(*) + GROUP BY`` 的活，
  全量解析纯属浪费。

表：
- ``learned_history(id, ts, fact, category, fp, source)``
  - ``fp`` = fact 归一化后的 md5，``UNIQUE`` → 重复记忆天然幂等。
  - ``source`` 标记来源（``legacy`` = 从 jsonl 迁移，``live`` = 运行时新学）。

契约（**改这里必须同步改前端**）：
- ``recent()`` 返回的 items 是**时间正序（最老在前）**，前端
  UnderstandingCard 会自行 reverse 后展示。
- 排序 tie-break 用 ``id``：ts 是秒精度，同一秒学到的多条靠 id 保持
  学到的先后顺序。

迁移：``migrate_from_legacy()`` 幂等导入 jsonl（INSERT OR IGNORE，靠 fp
去重）。导入后**校验条数**——DB 去重条数必须 ≥ 文件去重条数，否则视为
失败、不归档原文件。校验通过才由 ``archive_legacy()`` 把 jsonl 改名
``.bak-<ts>``（**绝不删除**）。

与 growth.json 的教训：本 store **不做 legacy 回灌**。归档后代码只读 DB。
若 DB 被清空，数据不会"自己又长回来"——那是比丢失更诡异的行为。安全网
是保留的 ``.bak`` 文件，需要时用 ``migrate_from_legacy(path)`` 手动导回。

fail-open：任何 DB 异常都不向上抛，保证主聊天链路不被记忆存储拖垮。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

def _default_base_dir() -> Path:
    """默认根目录——**调用时**解析 ``DESKMATE_DIR``。

    ⚠ 历史坑（2026-09-21）：这里曾是模块级常量 ``Path.home()/".deskmate"``，
    完全不认环境变量，于是 conftest 的 autouse 隔离对 LearnerStore 全线失效
    —— 测试里任何一次不带 base_dir 的实例化都会写进开发者**真实库**。
    同族修复见 ``memory/persistence.py``、``growth/diary_store.py``。
    """
    from deskmate.paths import deskmate_base_dir

    return deskmate_base_dir()
_DB_NAME = "deskmate.db"
_LEGACY_REL = Path("memory") / "learned_history.jsonl"


def _now_iso() -> str:
    # 与原实现一致：秒精度，不带时区（既有数据全是这个格式，保持可比较）
    return datetime.datetime.now().isoformat(timespec="seconds")


def _fingerprint(fact: str) -> str:
    """事实指纹：忽略空白差异，用于去重。"""
    return hashlib.md5(re.sub(r"\s+", "", fact or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 来源 / 效力层级（2026-11-10 萧哥终裁：已写正文事实是唯一事实基准）
#
# **复用 ``source`` 列编码，零 schema 变更**。原因：learned_history 的同步链路
# ``sync_store_sqlite.SYNC_TABLES["learned_history"]["business_cols"]`` 是硬白名单，
# 新增列不进 oplog、远端 upsert 的 LWW 还会把对端新列抹成 NULL ⇒ 静默破坏双端
# 收敛；而 ``source`` 已在白名单里、跨端天然同步，且有 find_active_by_source /
# retire_by_source 配套。格式 ``<prefix>[:<book_id>]``，如 ``manuscript:夜不收``。
#
# 现场教训：定时读稿摘要、口述裁定、圆桌结论、外部新闻、进度流水全被压成一条
# 无维度的 ``fact`` 且一律 active；每纠正一次就新写一条 active，旧错条仍是
# active ⇒ 越纠越乱。下列分层的目的就是让「什么算事实」在写入时即可判定。
# ---------------------------------------------------------------------------

SOURCE_MANUSCRIPT = "manuscript"        # 已写正文逐字直引 —— 唯一事实基准
SOURCE_AUTHOR_RULING = "author_ruling"  # 作者明确裁定（规划层「往后怎么走」）
SOURCE_USER = "live"                    # 用户口述（偏好/习惯/一般事实），兼容历史值
SOURCE_CORRECTION = "correction"        # 纠正：写入时取代同锚点的旧错条
SOURCE_REASONING = "reasoning"          # 推理 / 解读 / 读后感 / 考据判断
SOURCE_SETTING = "setting"              # 设定 / 大纲 / 章纲 / 规划
SOURCE_ROUNDTABLE = "roundtable"        # 圆桌 / 简报 / 讨论 / 复盘结论
SOURCE_PROGRESS = "progress"            # 进度 / 改稿动作记录
SOURCE_EXTERNAL = "external"            # 外部观察 / 新闻
SOURCE_LOCAL_INTAKE = "local_intake"    # 本地笔记摄入（书稿资料/人物/目录）——参考层
SOURCE_LEGACY = "legacy"                # 从 learned_history.jsonl 迁移的存量数据（低置信）

#: P2 生命周期：进度类写入侧默认有效期（天）。召回侧 user_fact_context
#: 也有 30 天 TTL；两处口径必须一致，前者让 DB 里的 valid_until 字段本身可信，
#: 后者兜住历史存量（此前 7/1389 条才有值）。正文/裁定/偏好永不自动过期。
PROGRESS_TTL_DAYS = 30

#: 非正文来源：只能作线索，不得作为「已写事实」陈述
NON_FACT_SOURCES = frozenset({
    SOURCE_REASONING,
    SOURCE_SETTING,
    SOURCE_ROUNDTABLE,
    SOURCE_PROGRESS,
    SOURCE_EXTERNAL,
    # 2026-11-10 补：local_intake 是「本地笔记摄入」（书稿资料/人物资料/目录/大纲），
    # 不是萧哥口述事实。此前它既非 live 也非 NON_FACT，34 条研究笔记被当成
    # 「你跟我说过的」主块注入（实测 12:40 快照：34/633）。
    SOURCE_LOCAL_INTAKE,
})

#: 写入侧允许的 source 前缀（抽取器输出经此白名单过滤，未知值退回 live）
VALID_SOURCE_PREFIXES = frozenset({
    SOURCE_MANUSCRIPT, SOURCE_AUTHOR_RULING, SOURCE_USER, SOURCE_CORRECTION,
    SOURCE_REASONING, SOURCE_SETTING, SOURCE_ROUNDTABLE,
    SOURCE_PROGRESS, SOURCE_EXTERNAL, SOURCE_LOCAL_INTAKE,
})

#: 「与书有关」的来源：写入时若能从正文/会话判定书名，就把书名拼进 source 后缀，
#: 让召回侧有书维度可用。live 偏好类不拼——一句「已出版《明朝的裂变》」是通用
#: 事实，拼上书维度后会在其它书的会话里被隔离掉，反而丢信息。
BOOKISH_SOURCES = frozenset({
    SOURCE_MANUSCRIPT, SOURCE_AUTHOR_RULING, SOURCE_CORRECTION,
    SOURCE_SETTING, SOURCE_PROGRESS, SOURCE_REASONING, SOURCE_ROUNDTABLE,
})

_AUTHORITY = {
    SOURCE_MANUSCRIPT: 100,
    SOURCE_AUTHOR_RULING: 60,
    SOURCE_USER: 50,
    SOURCE_CORRECTION: 50,
    SOURCE_LEGACY: 40,
    SOURCE_LOCAL_INTAKE: 25,
    SOURCE_PROGRESS: 30,
    SOURCE_SETTING: 20,
    SOURCE_REASONING: 15,
    SOURCE_ROUNDTABLE: 10,
    SOURCE_EXTERNAL: 5,
}
_DEFAULT_AUTHORITY = 40  # 未知 source 按「一般口述」处理，不臆断降权


def source_prefix(source: str | None) -> str:
    """取 ``<prefix>[:<book_id>]`` 的前缀；空值按 ``live``。"""
    return str(source or SOURCE_USER).split(":", 1)[0].strip()


def source_book(source: str | None) -> str:
    """取书名维度；无则空串。"""
    parts = str(source or "").split(":", 1)
    return parts[1].strip() if len(parts) == 2 else ""


def make_source(prefix: str, book_id: str | None = None) -> str:
    return f"{prefix}:{book_id}" if book_id else prefix


def source_authority(source: str | None) -> int:
    """效力层级：数值越大越可信；只有 manuscript 是「事实基准」。"""
    return _AUTHORITY.get(source_prefix(source), _DEFAULT_AUTHORITY)


def is_canon_source(source: str | None) -> bool:
    """是否「已写正文逐字直引」——唯一事实基准。"""
    return source_prefix(source) == SOURCE_MANUSCRIPT


def is_non_fact_source(source: str | None) -> bool:
    """是否非正文来源（推理/设定/大纲/简报/进度/外部/本地笔记），只能作线索。"""
    return source_prefix(source) in NON_FACT_SOURCES


# ---------------------------------------------------------------------------
# 存量来源兜底（2026-11-10 补）
#
# 实测（12:40 只读快照）：active 1389 条里 live 1123、legacy 211、local_intake 34，
# manuscript 0 条、author_ruling 只有 4 条。存量里混着外部新闻、小桔自己的决定/
# 复盘口吻、本地笔记摘要——它们写库时没有来源维度，不能靠改历史数据「补齐」，
# 只能在**读侧**归一：召回时按内容特征把它们降进参考层。
#
# 关键取舍：只认高置信的**外部信源标记**，不做「像新闻就降级」的模糊判断——
# 误把萧哥口述降级，比漏降一条外部新闻更糟。存量清洗另有 reclassify_legacy()
# 显式迁移方法（默认 dry-run），不自动跑。
# ---------------------------------------------------------------------------
_EXTERNAL_SIGNS = (
    "（外部",
    "【天下要闻】", "【财经脉动】", "【国际观察】",
    "【人文现场】", "【科技前沿】",
    "财联社", "新华网", "央视网", "新闻联播",
    "早啊！新闻", "早8点", "川观早",
)


def looks_external(fact: str) -> bool:
    """一条事实是否带高置信的外部信源标记（存量 live/legacy 降级用）。"""
    text = str(fact or "")
    return any(sign in text for sign in _EXTERNAL_SIGNS)


def effective_source(source: str | None, fact: str = "") -> str:
    """读侧归一来源：给存量无维度条目一个保守的「实际效力」判定。

    - 空值按 legacy（比 live 更保守：不冒充用户口述）；
    - live / legacy 但正文带外部信源标记 → external（只能作参考）；
    - 其余原样返回（含 <prefix>:<book_id>）。
    """
    prefix = source_prefix(source)
    if not source:
        return SOURCE_EXTERNAL if looks_external(fact) else SOURCE_LEGACY
    if prefix == SOURCE_LEGACY:
        return SOURCE_EXTERNAL if looks_external(fact) else SOURCE_LEGACY
    if prefix == SOURCE_USER and looks_external(fact):
        return SOURCE_EXTERNAL
    return str(source)


def fact_titles(fact: str) -> list[str]:
    """取一条事实里的《书名》/「引号专名」，去重保序（最多 8 个）。"""
    seen: list[str] = []
    pattern = r"[《「『\u201c\u0022](.+?)[》」』\u201d\u0022]"
    for raw in re.findall(pattern, str(fact or "")):
        title = raw.strip()
        if title and title not in seen:
            seen.append(title)
    return seen[:8]


def fact_book(source: str | None, fact: str = "") -> str:
    """一条事实的「书维度」原始提示：优先 source 后缀，其次正文里唯一的书名。

    - manuscript:夜不收 → 夜不收
    - 无 source 后缀、正文只提到一本《X》 → X
    - 正文提到多本（如创作阶段回顾）/ 一本都没提 → 空串（视为通用事实）
    """
    book = source_book(source)
    if book:
        return book
    titles = fact_titles(fact)
    return titles[0] if len(titles) == 1 else ""


def infer_book_hint(fact: str, session_text: str = "") -> str:
    """写入侧推断书维度：本条事实唯一书名优先，其次整段会话唯一书名。

    宁缺勿错：都判不出来就返回空串（通用事实），不猜当前项目。
    """
    titles = fact_titles(fact)
    if len(titles) == 1:
        return titles[0]
    if len(titles) > 1:
        return ""
    session_titles = fact_titles(session_text)
    return session_titles[0] if len(session_titles) == 1 else ""


def with_book(source: str, fact: str = "", session_text: str = "") -> str:
    """给「与书有关」的来源补上书维度后缀（幂等，已有后缀不覆盖）。"""
    prefix = source_prefix(source)
    if source_book(source) or prefix not in BOOKISH_SOURCES:
        return source
    hint = infer_book_hint(fact, session_text)
    return make_source(prefix, hint) if hint else source


# 抽取器漏给 source（本地规则抽取 / 旧版 LLM 输出）时的保守兜底。
_SETTING_MARKS = ("设定", "大纲", "章纲", "规划", "打算", "定为", "改成", "决定", "拟")
_PROGRESS_MARKS = ("完成", "写完", "推进", "发布", "定稿", "收尾", "改完", "落盘", "新增正文")


def infer_source(raw: str | None, category: str, text: str = "") -> str:
    """把抽取器输出归一成合法 source；缺失/非法时按类别与措辞保守推断。

    宁把「设定/进度」降到参考层，也不让它们冒充用户口述事实进主块。
    """
    if raw:
        candidate = str(raw).strip()
        if source_prefix(candidate) in VALID_SOURCE_PREFIXES:
            return candidate
    cat = (category or "").lower()
    if cat == "preference":
        return SOURCE_USER
    if cat == "correction":
        return SOURCE_CORRECTION
    # 2026-11-10 P1-A：category 有两个来源口径——LLM 抽取器的 "work"，
    # 和 classify_fact() 的 "other"/"habit"/"tool"。原实现只在 "work" 下看
    # 文本标记，导致数据层内建推断时「设定/进度」全部退回 live，等于没治。
    # 现在除 preference/correction 短路外一律按措辞保守判断（宁降级成线索，
    # 也不让设定冒充口述）；habit/tool 的「每天/用」等词不参与，避免误伤。
    if cat in ("work", "other", ""):
        t = text or ""
        if any(m in t for m in _SETTING_MARKS):
            return SOURCE_SETTING
        if any(m in t for m in _PROGRESS_MARKS):
            return SOURCE_PROGRESS
    return SOURCE_USER


# ---------------------------------------------------------------------------
# P3 正文证据锚点（方案 B：零 schema、本地索引、不进同步表）
#
# learned_history 一行都不加列：证据指针落在 ~/.deskmate/canon_evidence.json，
# 键用 fp（本来就在同步白名单里、双端稳定），值记 (书, 文件, 段号, 引文 hash)。
# 这样本地可回查到正文原句，云端只看得到 source=manuscript:<书名> 的效力标记；
# 云端没有书稿正文，验证一律失败 → 写入侧自动降级，绝不臆造「正文事实」。
# 详见 deskmate/novel_memory/canon.py 与设计文档 §5。
# ---------------------------------------------------------------------------

CANON_EVIDENCE_FILE = "canon_evidence.json"


def canon_evidence_path(base_dir) -> "Path":
    from pathlib import Path

    return Path(base_dir) / CANON_EVIDENCE_FILE


def load_canon_evidence(base_dir) -> dict:
    import json

    try:
        data = json.loads(canon_evidence_path(base_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}  # fail-open：索引缺失/损坏不阻断任何链路


def record_canon_evidence(base_dir, fp: str, record: dict) -> None:
    """原子写一条证据指针（临时文件 + os.replace，项目持久化红线）。"""
    import json

    from deskmate.growth._atomic import atomic_write_text

    data = load_canon_evidence(base_dir)
    data[str(fp)] = record
    atomic_write_text(
        canon_evidence_path(base_dir),
        json.dumps(data, ensure_ascii=False, indent=2),
    )


def verify_canon_quote(quote, book_id: str = "", folder=None) -> dict:
    """服务端闸门：引文命中本地正文才允许标 manuscript；异常一律判失败。"""
    try:
        from deskmate.novel_memory.canon import verify_quote

        return verify_quote(quote, book_id, folder=folder)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"正文校验不可用：{exc}"}


def canon_provenance(base_dir, fp: str, source: str) -> str:
    """召回时给正文条附出处；云端无索引时明说「仅本地可回查」。"""
    if source_prefix(source) != SOURCE_MANUSCRIPT:
        return ""
    ev = load_canon_evidence(base_dir).get(str(fp))
    if not ev:
        return "〔正文出处仅本地可回查〕"
    return (
        f"〔出处：{ev.get('book', '')} 第{ev.get('para_no', '?')}段"
        f"·{ev.get('file_path', '')}〕"
    )


# 与 app._classify_learned_fact 保持一致的启发式归类。
# 收进 store 是为了让「分类」在数据侧就定型，而不是每次读取时重算。
_PREFERENCE_KEYS = (
    "偏好", "喜欢", "倾向", "爱用", "常用", "希望", "想要", "期望",
    "不愿", "讨厌", "避免", "prefer", "like", "hate",
)
_HABIT_KEYS = (
    "习惯", "通常", "一般", "每天", "每次", "经常", "往往", "固定",
    "habit", "usually", "always",
)
_TOOL_KEYS = (
    "工具", "用", "使用", "命令", "快捷键", "软件", "编辑器", "ide",
    "tool", "use", "command",
)


def classify_fact(fact: str) -> str:
    """启发式归类一条学到的记忆（用于「了解度」统计）。"""
    t = (fact or "").lower()
    if any(k in t for k in _PREFERENCE_KEYS):
        return "preference"
    if any(k in t for k in _HABIT_KEYS):
        return "habit"
    if any(k in t for k in _TOOL_KEYS):
        return "tool"
    return "other"


def _extract_entity(fact: str) -> str:
    """抽取事实的主体（用于矛盾检测的「同实体」判定）。

    只在**高置信**场景才返回实体，避免误判把不相关事实标成矛盾：
    - 书名/文献：``《...》`` / ``「...」`` / ``"..."`` 括起来的 → ``书:书名``
    - 其它（人名/泛指/无括实体）→ 返回空串，靠 fp 去重即可，不强做矛盾配对。

    这是「《宋会要辑稿》在史料库」与「《宋会要辑稿》不在史料库」能被识别为
    同一实体相反极性的关键；泛主体不抽，宁可漏判也不误判。
    """
    m = re.search(r"[《「『\"“](.+?)[》」』\"”]", fact or "")
    if m:
        inner = m.group(1).strip()
        if inner:
            return "书:" + inner
    return ""


# 否定标记：命中即视为「否认/不存在」类断言，与「断言/存在」类相反极性。
# 只覆盖明确否定词，避免把「不在意细节」这类中性句误判（其 entity 为空也不做配对）。
_DENY_RE = re.compile(
    r"(不在|没(有|在|用)|未(必|曾|收录|在)|无|否(则|定|认|有)|并非|不是|"
    r"没有|从不|绝不|别(说|提|用)|并未|没有收录)"
)


def _extract_polarity(fact: str) -> str:
    """抽取事实的极性：``deny``（否认/不存在）或 ``assert``（断言/存在）。"""
    return "deny" if _DENY_RE.search(fact or "") else "assert"


# ---------------------------------------------------------------------------
# 主体一致性检测（P1-1，2026-09-26）：人称/主体颠倒检测
#
# 对应真实踩过的坑：把小桔的活儿记成萧盛做的（「主体错记/人称颠倒」）。
# 现有矛盾检测只认书名实体（_extract_entity），抓不到这类归因错误。
#
# 检测模式：**主体颠倒对** —— 同一谓词内容 X 被记在两个对立主体名下
# （「小桔整理了史料」 vs 「萧盛整理了史料」）。这属于「谁做的」归因
# 冲突，机器不判谁对：新条标 ``questioned``（不内化、不进活跃检索），
# 同时发 L2 提议卡由用户裁决（见 agency/engine.resolve_proposal）。
# 与书名冲突走 supersede 的差别：书名在/不在史料库是客观事实，后到
# 的算更新；归因错误不能由机器单方面判旧条错。
#
# 灰度开关：``DESKMATE_SUBJECT_GUARD=0`` 关闭（默认开）。
# ---------------------------------------------------------------------------

#: 主体消解规则表——**唯一**主体判定来源，别在别处再写人称判断。
#: 只收「高置信专名」：小桔侧 / 萧盛侧。泛指与代词（他/她/我/自己）
#: 一律不收——「我」在学到的记忆里多是被引述的用户原话，「他/她」
#: 无法指认 → 宁可漏判不误判（≥2 字才算证据，同红线口径）。
_SUBJECT_RULES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("xiaoju", re.compile(r"小桔|小橘")),
    ("user", re.compile(r"萧盛|萧哥|用户|你")),
)

#: 主体互为颠倒的配对（同谓词 X 挂到两个不同主体 = 归因冲突）
_SUBJECT_OPPOSITE = {("xiaoju", "user"), ("user", "xiaoju")}

#: 谓词指纹剥离：主体词 + 引语标记 + 时间戳 + 标点空白。
#: 剥完剩余 ≥2 字才参与配对——「小桔赢了」「用户赢了」这类剩单字的
#: 谓词没有判别力，宁漏勿误。
_SUBJECT_STRIP_RE = re.compile(
    r"小桔|小橘|萧盛|萧哥|用户|你|我"
    r"|\d{4}-\d{1,2}-\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?"
    r"|[《「『\"“」』”》:：，,。！!？?；;、\s\-—·()（）\"]"
)


def _subject_guard_enabled() -> bool:
    """主体错记检测开关（``DESKMATE_SUBJECT_GUARD=0`` 关闭，默认开）。"""
    import os

    return os.environ.get("DESKMATE_SUBJECT_GUARD", "1") != "0"


def _extract_subject(fact: str) -> tuple[str, str]:
    """抽取 ``(主体 canonical, 谓词指纹)``；低置信场景返回 ``("", "")``。

    - 事实里同时出现两侧主体（「萧盛让小桔整理史料」）→ 引述/协作关系，
      不是单一归属 → 不配对；
    - 两侧都不出现 → 无主体信息 → 不配对；
    - 谓词剥完 < 2 字 → 无判别力 → 不配对。
    """
    text = (fact or "").strip()
    if not text:
        return "", ""
    hits = {canon for canon, pat in _SUBJECT_RULES if pat.search(text)}
    if len(hits) != 1:
        return "", ""
    subject = hits.pop()
    x = _SUBJECT_STRIP_RE.sub("", text)
    if len(x) < 2:
        return "", ""
    return subject, x


class LearnedStore:
    """长期记忆的 SQLite 持久层。短连接 + 自动提交（WAL 支持并发读）。"""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.base = Path(base_dir) if base_dir else _default_base_dir()
        self.db_path = self.base / _DB_NAME
        self.legacy_path = self.base / _LEGACY_REL
        self.base.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ------------------------------------------------------------------
    # 连接 / schema
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _add_column(self, table: str, col: str, col_type: str) -> None:
        """兼容已存在的库：缺失列才补（SQLite 无 ADD COLUMN IF NOT EXISTS）。"""
        try:
            with self._connect() as conn:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if col not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except sqlite3.Error:
            pass

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS learned_history (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts       TEXT NOT NULL,
                    fact     TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'other',
                    fp       TEXT NOT NULL,
                    source   TEXT NOT NULL DEFAULT 'live'
                )
                """
            )
            # 兼容已存在的库：补齐矛盾检测 / 时效失效所需列
            # （SQLite 不支持 ADD COLUMN IF NOT EXISTS，逐个判断后补）
            self._add_column("learned_history", "entity", "TEXT")
            self._add_column("learned_history", "polarity", "TEXT")
            self._add_column("learned_history", "status", "TEXT NOT NULL DEFAULT 'active'")
            self._add_column("learned_history", "superseded_by", "INTEGER")
            self._add_column("learned_history", "valid_until", "TEXT")
            # 双时态（P1-2）：valid_from = 事实生效起点（学到即生效，默认=记录时间 ts）
            self._add_column("learned_history", "valid_from", "TEXT")
            # fp 唯一 → 重复学到的同一事实天然幂等（INSERT OR IGNORE 即可）
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_learned_fp "
                "ON learned_history(fp)"
            )
            # 取最近 N 条：ORDER BY ts DESC, id DESC
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_learned_ts "
                "ON learned_history(ts DESC, id DESC)"
            )
            # 分类统计走覆盖索引，不再全表解析
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_learned_cat "
                "ON learned_history(category)"
            )
            # 矛盾检测：按 (实体, 状态, 极性) 定位旧条
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_learned_entity "
                "ON learned_history(entity, status, polarity)"
            )

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def append(
        self,
        facts: list[str],
        ts: str | None = None,
        valid_until: str | None = None,
        source: str | None = None,
        *,
        book_id: str | None = None,
        anchor: str | None = None,
        supersedes: list[str] | None = None,
        session_text: str | None = None,
        canon_quote: str | None = None,
        canon_folder: str | None = None,
    ) -> int:
        """追加若干条学到的事实，返回**实际新增**条数（重复的被 fp 挡掉）。

        Args:
            valid_until: 可选过期时间（ISO 秒精度）。外部/趋势类信息带过期，
                到点的条目在 ``active_recent`` / ``relevant`` 中自动失效剔除。
            source: 来源与效力，``<prefix>[:<book_id>]``（见模块级常量）。
                **P1-A：默认 None，不再默认 live。** 未给来源时逐条用
                ``infer_source`` 按类别/措辞保守推断，并用 ``with_book`` 补书维度；
                非法前缀同样退回推断。调用点应显式传语义化来源，数据层兜底保证
                任何旁路都不会把设定/进度/外部信息冒充「萧哥口述」写进主块。
            book_id: 便捷写法，等价于把来源拼成 ``<source>:<book_id>``。
            anchor: 无《》书名时显式给出冲突配对键（写入 entity 列）。
            supersedes: 调用方点名的「被纠正的旧说法」原文，按 fp 精确取代。
            session_text: 本轮会话文本，供 ``with_book`` 在单条事实无书名时
                按「整段会话唯一书名」补书维度（判不出就不猜）。
            canon_quote: P3 正文逐字引文。标 ``source=manuscript`` 时**必须**
                提供且能在本地正文索引里命中；否则自动降级为 ``author_ruling``
                （不臆造事实基准）。云端无正文 → 一律降级。
            canon_folder: 显式书稿目录，跳过按书名发现（测试/本地工具用）。

        事实纪律（2026-11-10 萧哥终裁：已写正文事实是唯一事实基准）：

        1. 显式 ``supersedes`` → 按 fp 精确取代指定的旧活跃条；
        2. ``manuscript`` 写入 → 同锚点的非正文旧活跃条一律让位；
        3. ``correction`` 写入 → 取代最近一条非正文基准的旧活跃条
           （治「每纠正一次就新写一条 active、旧错条仍 active」）；
        4. 原有「同实体 + 反极性」取代保留，但**正文事实不被非正文来源推翻**。

        注意：冲突走 ``INSERT OR IGNORE``，已存在的 fp 不会被更新——因此重复
        写入同一条事实不会刷新其 ``valid_until``，过期语义稳定。
        """
        clean: list[tuple[str, str, str, str, str, str, str | None, str]] = []
        stamp = ts or _now_iso()
        # 批级来源：显式给且合法才整批沿用；否则逐条推断（P1-A 写入纪律）。
        # 非法前缀（例如抽取器幻觉出的值）一律退回推断，不写进 source 列。
        given = str(source).strip() if source is not None else ""
        batch_source = given if source_prefix(given) in VALID_SOURCE_PREFIXES else ""
        canon_by_fp: dict[str, dict] = {}
        for raw in facts:
            fact = str(raw).strip() if raw else ""
            if not fact:
                continue
            cat_ = classify_fact(fact)
            fact_source = batch_source or infer_source(None, cat_, fact)
            fact_source = with_book(fact_source, fact, session_text or "")
            if book_id and not source_book(fact_source):
                fact_source = make_source(source_prefix(fact_source), book_id)
            # P3 正文效力闸门：自称「已写正文」不够，必须有能在本地正文中
            # 命中的逐字引文。命中则记证据指针，未命中降级为作者裁定。
            if source_prefix(fact_source) == SOURCE_MANUSCRIPT:
                bid = source_book(fact_source) or str(_extract_entity(fact)).removeprefix("书:")
                fp_ = _fingerprint(fact)
                ev = verify_canon_quote(canon_quote, bid, canon_folder)
                if ev.get("ok"):
                    canon_by_fp[fp_] = {
                        "book": ev.get("book") or bid,
                        "file_path": ev.get("file_path", ""),
                        "para_no": ev.get("para_no"),
                        "quote": ev.get("quote", ""),
                        "quote_hash": ev.get("quote_hash", ""),
                    }
                else:
                    fact_source = make_source(
                        SOURCE_AUTHOR_RULING, source_book(fact_source)
                    )
            # P2：进度类写侧默认 30 天有效期（显式传入的优先，不覆盖）。
            fact_valid_until = valid_until
            if (
                fact_valid_until is None
                and source_prefix(fact_source) == SOURCE_PROGRESS
            ):
                fact_valid_until = (
                    datetime.datetime.now()
                    + datetime.timedelta(days=PROGRESS_TTL_DAYS)
                ).isoformat(timespec="seconds")
            entity = anchor or _extract_entity(fact)
            clean.append(
                (
                    stamp,
                    fact,
                    cat_,
                    _fingerprint(fact),
                    entity,
                    _extract_polarity(fact),
                    fact_valid_until,
                    fact_source,
                )
            )
        if not clean:
            return 0
        try:
            with self._connect() as conn:
                inserted = 0
                for (
                    stamp_, fact_, cat_, fp_, entity_, polarity_, valid_until_,
                    fact_source_,
                ) in clean:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO learned_history"
                        "(ts, fact, category, fp, source, entity, polarity, status, valid_until, valid_from) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                        (stamp_, fact_, cat_, fp_, fact_source_, entity_, polarity_, valid_until_, stamp_),
                    )
                    if not (cur.rowcount and cur.rowcount > 0):
                        continue  # 重复（同 fp）→ 跳过，也不做矛盾处理
                    inserted += 1
                    rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    if fp_ in canon_by_fp:
                        try:
                            record_canon_evidence(self.base, fp_, canon_by_fp[fp_])
                        except Exception:
                            pass  # 证据索引写失败不阻断记忆写入（fail-open）

                    # (1) 显式取代：纠正方点名的旧说法，按 fp 精确命中，不误伤同书其它条
                    for old_text in supersedes or []:
                        conn.execute(
                            "UPDATE learned_history SET status='superseded', valid_until=?, superseded_by=? "
                            "WHERE status='active' AND fp=? AND id!=?",
                            (stamp_, rid, _fingerprint(str(old_text)), rid),
                        )

                    if entity_:
                        old_rows = conn.execute(
                            "SELECT id, source, polarity FROM learned_history "
                            "WHERE status='active' AND entity=? AND id!=? "
                            "ORDER BY ts DESC, id DESC",
                            (entity_, rid),
                        ).fetchall()
                        canon_new = is_canon_source(fact_source_)
                        correction_new = source_prefix(fact_source_) == SOURCE_CORRECTION

                        # (2) 正文事实写入：同锚点的非正文旧条一律让位
                        if canon_new:
                            for old_id, old_source, _pol in old_rows:
                                if is_non_fact_source(old_source):
                                    conn.execute(
                                        "UPDATE learned_history SET status='superseded', "
                                        "valid_until=?, superseded_by=? WHERE id=?",
                                        (stamp_, rid, old_id),
                                    )

                        # (3) 纠正类写入：只取代最近一条「非正文基准」的旧活跃条
                        if correction_new:
                            for old_id, old_source, _pol in old_rows:
                                if not is_canon_source(old_source):
                                    conn.execute(
                                        "UPDATE learned_history SET status='superseded', "
                                        "valid_until=?, superseded_by=? WHERE id=?",
                                        (stamp_, rid, old_id),
                                    )
                                    break

                        # (4) 同实体 + 反极性：保留历史取代语义，但正文事实不可被非正文推翻
                        for old_id, old_source, old_pol in old_rows:
                            if old_pol == polarity_:
                                continue
                            if is_canon_source(old_source) and not canon_new:
                                continue
                            conn.execute(
                                "UPDATE learned_history SET status='superseded', "
                                "valid_until=?, superseded_by=? WHERE id=?",
                                (stamp_, rid, old_id),
                            )

                    if _subject_guard_enabled():
                        # P1-1 主体颠倒检测：同谓词挂在对立主体名下 → 新条
                        # 不内化（questioned）+ 发提议卡由用户裁决。
                        self._detect_subject_conflict(conn, rid, fact_)
                return inserted
        except sqlite3.Error:
            return 0

    def _detect_subject_conflict(
        self, conn: sqlite3.Connection, new_id: int, new_fact: str
    ) -> None:
        """主体颠倒对检测（fail-open：任何异常只记日志，不影响写入主链路）。

        扫描活跃事实里与新条**同谓词、对立主体**的旧条；命中则把新条标
        ``questioned``（从 active_recent/relevant 等活跃检索自动剔除），
        并向 L2 TaskLog 发一条 ``memory_subject_conflict`` 提议卡。旧条
        保持 active——它此前已被相信，机器不单方面翻案。
        """
        try:
            subj_new, x_new = _extract_subject(new_fact)
            if not subj_new or len(x_new) < 2:
                return
            rows = conn.execute(
                "SELECT id, fact FROM learned_history "
                "WHERE status='active' AND id != ?",
                (new_id,),
            ).fetchall()
            for old_id, old_fact in rows:
                subj_old, x_old = _extract_subject(str(old_fact))
                if not subj_old or x_old != x_new:
                    continue
                if (subj_old, subj_new) not in _SUBJECT_OPPOSITE:
                    continue
                # 命中：新条降级为 questioned（不内化），旧条不动
                conn.execute(
                    "UPDATE learned_history SET status='questioned' WHERE id=?",
                    (new_id,),
                )
                self._queue_subject_proposal(new_id, new_fact, int(old_id), str(old_fact))
                return  # 一条新条最多挂一张卡
        except Exception:  # noqa: BLE001 —— fail-open，绝不拖垮写入
            return

    def _queue_subject_proposal(
        self, new_id: int, new_fact: str, old_id: int, old_fact: str
    ) -> None:
        """给主体冲突发 L2 提议卡（同一旧条不重复发卡）。"""
        try:
            from deskmate.agency.task_log import TaskLog, TaskStatus

            log = TaskLog(self.base)
            for rec in log.list(limit=200, status=TaskStatus.PROPOSED):
                if rec.action_type == "memory_subject_conflict" and (
                    rec.extra.get("old_id") == old_id
                ):
                    return  # 同一旧条已有待裁决卡，不刷屏
            log.add(
                action_type="memory_subject_conflict",
                risk="high",
                status=TaskStatus.PROPOSED,
                topic="记忆主体冲突",
                summary="两条记忆把同一件事记在不同主体名下，需要你裁决",
                detail=f"新条：{new_fact}\n旧条：{old_fact}\n"
                f"（新条暂不生效；选「执行」= 以新条为准，旧条标记被取代；"
                f"选「不用」= 保留旧条，新条作废）",
                extra={
                    "new_id": new_id,
                    "old_id": old_id,
                    "new_fact": new_fact,
                    "old_fact": old_fact,
                    "base_dir": str(self.base),
                },
            )
        except Exception:  # noqa: BLE001 —— fail-open
            return

    def resolve_subject_conflict(
        self, new_id: int, keep_new: bool, old_id: int | None = None
    ) -> bool:
        """裁决一条 questioned 记忆（由 L2 提议卡 ``memory_subject_conflict`` 调用）。

        Args:
            keep_new: True = 以新条为准（新条转 active、旧条 superseded）；
                False = 保留旧条（新条 forgotten 作废）。
            old_id: 配对的旧条 id（提议卡 extra 里带）；缺省时自动按
                同谓词 + 对立主体重找。
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT fact FROM learned_history WHERE id=? AND status='questioned'",
                    (new_id,),
                ).fetchone()
                if not row:
                    return False
                if not keep_new:
                    conn.execute(
                        "UPDATE learned_history SET status='forgotten' WHERE id=?",
                        (new_id,),
                    )
                    return True
                conn.execute(
                    "UPDATE learned_history SET status='active' WHERE id=?",
                    (new_id,),
                )
                # 只取代配对的那条旧条（机器不扩大化翻案）
                if old_id is not None:
                    conn.execute(
                        "UPDATE learned_history SET status='superseded', "
                        "valid_until=?, superseded_by=? "
                        "WHERE id=? AND status='active'",
                        (_now_iso(), new_id, old_id),
                    )
                    return True
                # 未带 old_id：按同谓词 + 对立主体重找（保守，最多处理一条）
                subj_new, x_new = _extract_subject(str(row[0]))
                if not subj_new or len(x_new) < 2:
                    return True
                for oid, ofact in conn.execute(
                    "SELECT id, fact FROM learned_history "
                    "WHERE status='active' AND id != ?",
                    (new_id,),
                ).fetchall():
                    subj_old, x_old = _extract_subject(str(ofact))
                    if (
                        subj_old
                        and x_old == x_new
                        and (subj_old, subj_new) in _SUBJECT_OPPOSITE
                    ):
                        conn.execute(
                            "UPDATE learned_history SET status='superseded', "
                            "valid_until=?, superseded_by=? WHERE id=?",
                            (_now_iso(), new_id, oid),
                        )
                        break
                return True
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        """最近 limit 条，**时间正序（最老在前）**——前端会 reverse 后展示。

        2026-09-23 修复：此前不带 ``status`` 过滤 → 已遗忘（``forgotten``）/
        已被取代（``superseded``）的事实仍会被日记素材回填、 compromising
        「遗忘即刻生效」的承诺。这里只看 ``active``。
        2026-11-10 P2：补 ``valid_until`` 过滤——进度类写入侧现在带 30 天
        有效期，否则过期条目仍会从 recent() 回填日记素材/理解面板，与召回侧
        ``_is_stale`` 的口径打架。
        """
        limit = max(1, min(int(limit), 5000))
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT ts, fact, category FROM learned_history "
                    "WHERE status='active' "
                    "AND (valid_until IS NULL OR valid_until >= ?) "
                    "ORDER BY ts DESC, id DESC LIMIT ?",
                    (_now_iso(), limit),
                ).fetchall()
        except sqlite3.Error:
            return []
        # SQL 取的是倒序（最新在前），reverse 成正序，与旧文件版契约一致
        return [
            {"ts": r[0], "fact": r[1], "category": r[2]} for r in reversed(rows)
        ]

    def stats(self) -> dict[str, Any]:
        """总量 + 分类计数。走 SQL 聚合，不再全量解析文件。"""
        try:
            with self._connect() as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM learned_history"
                ).fetchone()[0]
                by_cat = {
                    r[0]: r[1]
                    for r in conn.execute(
                        "SELECT category, COUNT(*) FROM learned_history GROUP BY category"
                    ).fetchall()
                }
        except sqlite3.Error:
            return {"total": 0, "by_category": {}}
        return {"total": int(total), "by_category": by_cat}

    def snapshot(self, limit: int = 30, order: str = "time") -> dict[str, Any]:
        """一次返回 {items, stats}——路由直接吃这个。

        - ``order="time"``（默认）：items 为**活跃**事实、时间正序（最老在前），
          与前端 UnderstandingCard 的 reverse 契约一致；已被新事实取代的旧条
          不出现，避免矛盾两条并存展示。
        - ``order="relevance"``：items 按「新鲜度 × 频次」降序，带 ``score``
          字段，供叙事/上下文注入使用（recency 加权，见 :meth:`relevant`）。
        - stats 只计**当前有效(active)**事实，被取代的旧条不计入「了解度」。
        """
        if order == "relevance":
            items = self.relevant(limit)
        else:
            items = self.active_recent(limit)
        return {"items": items, "stats": self.active_stats()}

    def active_recent(self, limit: int = 30) -> list[dict[str, Any]]:
        """活跃事实，时间正序（最老在前），排除已失效/被取代的。供 UI 展示。"""
        limit = max(1, min(int(limit), 5000))
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT ts, fact, category FROM learned_history "
                    "WHERE status='active' AND (valid_until IS NULL OR valid_until >= ?) "
                    "ORDER BY ts DESC, id DESC LIMIT ?",
                    (_now_iso(), limit),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            {"ts": r[0], "fact": r[1], "category": r[2]} for r in reversed(rows)
        ]

    def active_stats(self) -> dict[str, Any]:
        """当前有效(active)事实的总量与分类计数（「了解度」只数有效信念）。"""
        try:
            with self._connect() as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM learned_history WHERE status='active'"
                ).fetchone()[0]
                by_cat = {
                    r[0]: r[1]
                    for r in conn.execute(
                        "SELECT category, COUNT(*) FROM learned_history "
                        "WHERE status='active' GROUP BY category"
                    ).fetchall()
                }
        except sqlite3.Error:
            return {"total": 0, "by_category": {}}
        return {"total": int(total), "by_category": by_cat}

    def relevant(self, limit: int = 30) -> list[dict[str, Any]]:
        """按「新鲜度 × 频次」排序的活跃事实——供注入上下文/叙事用（recency 加权）。

        新鲜度 = 1/(1+距今天数)；频次 = 同实体(无实体则同 fp)的事实条数
        （同一件事被反复学到 → 越重要）。返回降序，每条带 ``score`` 字段。
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, ts, fact, category, entity, fp FROM learned_history "
                    "WHERE status='active' AND (valid_until IS NULL OR valid_until >= ?) "
                    "ORDER BY ts DESC, id DESC",
                    (_now_iso(),),
                ).fetchall()
        except sqlite3.Error:
            return []
        now = datetime.datetime.now()
        freq: dict[str, int] = {}
        parsed: list[dict[str, Any]] = []
        for r in rows:
            rid, ts, fact, cat, entity, fp = r
            key = (entity or fp) or f"#{rid}"
            freq[key] = freq.get(key, 0) + 1
            try:
                dt = datetime.datetime.fromisoformat(ts)
            except ValueError:
                dt = now
            age_days = max(0, (now - dt).days)
            recency = 1.0 / (1.0 + age_days)
            score = round(recency * (1.0 + 0.5 * (freq[key] - 1)), 4)
            parsed.append(
                {"ts": ts, "fact": fact, "category": cat, "score": score}
            )
        parsed.sort(key=lambda x: -x["score"])
        return parsed[: max(1, min(int(limit), 5000))]

    def conflicts_report(self) -> list[dict[str, Any]]:
        """返回被取代（矛盾）的事实及其取代者——供 UI 标注「已更新」。"""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT o.fact, o.ts, n.fact, n.ts FROM learned_history o "
                    "JOIN learned_history n ON o.superseded_by = n.id "
                    "WHERE o.status='superseded' ORDER BY n.ts DESC, n.id DESC"
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            {
                "superseded_fact": r[0],
                "superseded_at": r[1],
                "replaced_by_fact": r[2],
                "replaced_at": r[3],
            }
            for r in rows
        ]

    def all_items(self) -> list[dict[str, Any]]:
        """全量导出（时间正序）。供迁移校验 / 数据导出用。"""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT ts, fact, category FROM learned_history ORDER BY ts ASC, id ASC"
                ).fetchall()
        except sqlite3.Error:
            return []
        return [{"ts": r[0], "fact": r[1], "category": r[2]} for r in rows]

    def forget(self, item_id: int) -> bool:
        """用户主动遗忘某条学到的记忆（Replika 式可控记忆）。

        标记 ``status='forgotten'``，使其从 ``active_recent`` / ``relevant`` 等
        活跃检索中自动剔除（这些检索都过滤 ``status='active'``）。不物理删除、
        不动 fp 幂等，保留遗忘痕迹可追溯。返回是否命中并变更。
        """
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE learned_history SET status='forgotten' "
                    "WHERE id=? AND status='active'",
                    (item_id,),
                )
                return bool(cur.rowcount and cur.rowcount > 0)
        except sqlite3.Error:
            return False

    def list_for_ui(self, limit: int = 200) -> list[dict[str, Any]]:
        """供 MemoryViewer 条目级可控：返回学到的记忆条目（含 id/status/source），
        按「活跃优先、时间倒序」排列，便于前端分区展示 active / forgotten。
        """
        limit = max(1, min(int(limit), 5000))
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, ts, fact, category, status, valid_until, "
                    "COALESCE(valid_from, ts), source "
                    "FROM learned_history "
                    "ORDER BY (status='active') DESC, ts DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            {
                "id": r[0], "ts": r[1], "fact": r[2], "category": r[3],
                "status": r[4], "valid_until": r[5], "valid_from": r[6],
                "source": r[7],
            }
            for r in rows
        ]

    def find_active_by_source(self, source: str) -> list[dict[str, Any]]:
        """返回某来源（如 ``local_intake:<path>``）当前活跃的事实 fp 列表。

        供摄入管线做「同文件内容是否变化」的幂等判断。
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT fp FROM learned_history "
                    "WHERE status='active' AND source=?",
                    (source,),
                ).fetchall()
            return [{"fp": r[0]} for r in rows]
        except sqlite3.Error:
            return []

    def retire_by_source(self, source: str, superseded_by: int | None = None) -> int:
        """把某来源的活跃条目标 ``superseded``（不再参与回忆），用于「内容变了重新沉淀」。

        P2：可传 ``superseded_by`` 指向取代它的新条目 id，补全双向可查链；
        不传时保留原有值（整批作废未必有单一后继，不臆造指向）。

        Returns:
            实际被取代的条数。
        """
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE learned_history SET status='superseded', valid_until=?, "
                    "superseded_by = CASE WHEN ? IS NULL THEN superseded_by ELSE ? END "
                    "WHERE status='active' AND source=?",
                    (_now_iso(), superseded_by, superseded_by, source),
                )
                return int(cur.rowcount or 0)
        except sqlite3.Error:
            return 0

    def reclassify_legacy(self, dry_run: bool = True) -> dict[str, Any]:
        """存量来源清洗：把带高置信外部信源标记的 live/legacy 条目标成 external。

        只动「来源列」，不动 fact/ts/status；默认 dry-run（真实库不被自动改写）。
        这是给运维/迁移留的显式入口，不在任何主链路上调用。

        Returns:
            {scanned, candidates, updated, samples}
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, fact, source FROM learned_history "
                    "WHERE source NOT LIKE '%:%' OR source IN (?, ?)",
                    (SOURCE_USER, SOURCE_LEGACY),
                ).fetchall()
        except sqlite3.Error:
            return {"scanned": 0, "candidates": 0, "updated": 0, "samples": []}
        targets: list[tuple[int, str]] = []
        samples: list[str] = []
        for rid, fact, source in rows:
            if source_prefix(source) not in (SOURCE_USER, SOURCE_LEGACY):
                continue
            if not looks_external(fact):
                continue
            targets.append((SOURCE_EXTERNAL, int(rid)))
            if len(samples) < 5:
                samples.append(str(fact)[:80])
        if not dry_run and targets:
            try:
                with self._connect() as conn:
                    conn.executemany(
                        "UPDATE learned_history SET source=? WHERE id=?", targets
                    )
            except sqlite3.Error:
                return {
                    "scanned": len(rows), "candidates": len(targets),
                    "updated": 0, "samples": samples,
                }
        return {
            "scanned": len(rows),
            "candidates": len(targets),
            "updated": 0 if dry_run else len(targets),
            "samples": samples,
        }

    # ------------------------------------------------------------------
    # 迁移：learned_history.jsonl → DB
    # ------------------------------------------------------------------
    def _read_legacy(self) -> list[dict[str, Any]]:
        """解析 legacy jsonl（容错跳过坏行）。文件不存在返回 []。"""
        if not self.legacy_path.is_file():
            return []
        out: list[dict[str, Any]] = []
        try:
            for line in self.legacy_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                fact = str(rec.get("fact", "")).strip()
                if not fact:
                    continue
                out.append(
                    {
                        "ts": rec.get("ts") or _now_iso(),
                        "fact": fact,
                        # 文件里已存了 category 就用它的（尊重历史分类），
                        # 缺失才重算——避免迁移改变既有统计口径
                        "category": rec.get("category") or classify_fact(fact),
                    }
                )
        except OSError:
            return []
        return out

    def migrate_from_legacy(self, archive: bool = True) -> dict[str, Any]:
        """幂等导入 legacy jsonl。

        Args:
            archive: 校验通过是否把原文件改名 ``.bak-<ts>``。

        Returns:
            ``{ok, imported, in_db, in_file, archived, reason}``
        """
        rows = self._read_legacy()
        result: dict[str, Any] = {
            "ok": False,
            "imported": 0,
            "in_db": 0,
            "in_file": len(rows),
            "archived": False,
            "reason": "",
        }
        if not rows:
            result["ok"] = True
            result["reason"] = "无 legacy 数据（文件不存在或为空）"
            return result

        payload = [
            (r["ts"], r["fact"], r["category"], _fingerprint(r["fact"]))
            for r in rows
        ]
        try:
            with self._connect() as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO learned_history(ts, fact, category, fp, source) "
                    "VALUES (?, ?, ?, ?, 'legacy')",
                    payload,
                )
                in_db = conn.execute(
                    "SELECT COUNT(*) FROM learned_history"
                ).fetchone()[0]
        except sqlite3.Error as exc:  # 导入失败绝不改原文件
            result["reason"] = f"导入失败：{exc}"
            return result

        result["in_db"] = int(in_db)
        # 校验：DB 里的总量必须覆盖文件里的去重条数。
        # 用「去重后的 fp 数」比对，避免文件里本身有重复导致误判。
        uniq_in_file = len({p[3] for p in payload})
        if in_db < uniq_in_file:
            result["reason"] = (
                f"校验失败：DB 只有 {in_db} 条 < 文件去重 {uniq_in_file} 条，"
                f"疑似导入不全，保留原文件"
            )
            return result

        result["ok"] = True
        result["imported"] = int(in_db)
        result["reason"] = f"导入完成，DB 现有 {in_db} 条"
        if archive:
            result["archived"] = self.archive_legacy()
        return result

    def archive_legacy(self) -> bool:
        """把 legacy jsonl 改名 ``.bak-<ts>``（绝不删除）。改名后代码只读 DB。"""
        if not self.legacy_path.is_file():
            return False
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.legacy_path.with_name(f"{self.legacy_path.name}.bak-{stamp}")
        try:
            self.legacy_path.rename(target)
            return True
        except OSError:
            return False
