"""WorksStore — 作品从「类别」落到「个体」（《夜不收》≠「稿子」）。

设计依据：reports/2026-11-16_小桔_情感层设计.md §12.2 C / §14.5。

## 为什么需要它

P0 的作品判据把「稿子/章节/文章」都算成 ``work``。这在归因上够用，在情感
上不成立：**情感对象必须是有具体指向的个体，而不是类别**。「稿子」本身
不构成情感对象——只有当情感落在稿子里的某一章、某个情节、某个人物、某句
话，并且她由那个具体的东西联想到现实里的谁/哪件事时，才成立。

所以对象层需要一张作品表 + 别名归一：``《夜不收》= 夜不收 = 夜不收那本``。
情感与情绪的对象最终落到具体的书上，而不是「稿子」这个跨年大筐。

## 落点

- 新表 ``works``（用户 2026-11-17 拍板：作品个体化提前做，不等 P2）；
- 只读书架 ``~/Documents/已出版``（``DESKMATE_BOT_BOOKS_ROOT`` 可覆盖），
  目录名的数字前缀只剥不改名，与 ``deskmate/bots/reading.py`` 同一口径；
- 别名来源：书架正名、``learned_history`` 里 ``书:XXX`` 且是正名的**短写**
  （如「国子监」→「国子监秘闻录」）。不拿新闻/栏目名建作品。
- ``resolve`` 只认已登记的作品，**不自动从噪声里造作品**。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

#: 默认书架；与 bots/reading.py 的 _DEFAULT_BOOKS_ROOT 一致。
DEFAULT_BOOKS_ROOT = Path("~/Documents/已出版")

#: 上次从书架重扫的时间戳（json_blobs），用来把扫描限在一周一次。
_SEED_TS_KEY = "works_seed_ts"

#: 目录名编号前缀（"16.夜不收" → "夜不收"）。
_NUM_PREFIX = re.compile(r"^\d+[.\-_、\s]+")

#: 书名号/引号/空白：surface 归一，不改变作品名本身。
_WRAPS = "《》〈〉「」『』\"'“”‘’ \t\r\n"

#: 只表示「一类东西」的稿件/文章叫法——不是作品个体，不能当情感对象。
_GENERIC_WORK_REFS = frozenset(
    {
        "稿子", "稿件", "稿", "新稿", "旧稿", "初稿", "终稿", "定稿", "主稿",
        "书稿", "文稿", "文章", "章节", "章", "篇", "文", "帖子", "大纲",
        "梗概", "作品", "书", "本书", "正文", "稿纸", "一稿", "二稿", "三稿",
    }
)
_GENERIC_WORK_RE = re.compile(
    r"^(这|那|本|该|整|一)?[\u4e00-\u9fa5]{0,2}"
    r"(稿子|稿件|稿|章节|文章|篇|文|帖子|大纲|梗概|书稿|作品|正文)$"
)


def work_id(title: str) -> str:
    """作品 id：只由正名派生，别名不参与。"""
    key = re.sub(r"\s+", "", clean_surface(title))
    return "work_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def clean_surface(ref: str) -> str:
    """对象 surface 归一：去书名号/引号/空白/编号前缀。

    只做「同一个名字的不同写法」，不猜内容。例：
    ``《夜不收》`` → ``夜不收``；``16.夜不收`` → ``夜不收``。
    """
    s = str(ref or "").strip()
    for ch in _WRAPS:
        s = s.replace(ch, "")
    s = _NUM_PREFIX.sub("", s).strip()
    return s


def is_generic_work_ref(ref: str) -> bool:
    """是不是「稿子/章节/文章」这类类别泛称（不是作品个体）。

    只拦明确的工作类别词；``第五部``、``夜不收``、``封面`` 都不算泛称。
    单字「稿」「章」「文」「篇」只在长度 ≤4 且整体是类别词时命中，
    避免把「文」这种可能的单字人名/书名一起清掉——调用方只在
    work/thing 类型上用它。
    """
    s = clean_surface(ref)
    if not s:
        return False
    if s in _GENERIC_WORK_REFS:
        return True
    return len(s) <= 4 and bool(_GENERIC_WORK_RE.match(s))


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class WorksStore:
    """作品表读写。只认已登记作品；读路径 fail-open。"""

    def __init__(
        self,
        db_path: Optional[str | Path] = None,
        books_root: Optional[str | Path] = None,
    ):
        self._db = Path(db_path) if db_path else (
            Path.home() / ".deskmate" / "deskmate.db"
        )
        root = books_root or os.environ.get("DESKMATE_BOT_BOOKS_ROOT") or DEFAULT_BOOKS_ROOT
        self._books_root = Path(root).expanduser()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db), timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS works (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                aliases TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                source TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_works_title ON works(title)"
        )
        # seed 时间戳借道已有的 json_blobs（同一个库），不新建表、不改架构。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS json_blobs (
                key        TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def register(
        self,
        title: str,
        aliases: Iterable[str] = (),
        source: str = "manual",
    ) -> Optional[dict]:
        """登记一个作品；同正名合并别名，不覆盖已有别名。"""
        name = clean_surface(title)
        if not name:
            return None
        aliases = [clean_surface(a) for a in aliases if clean_surface(a)]
        now = _now_iso()
        try:
            with self._connect() as conn:
                self._ensure_schema(conn)
                row = conn.execute(
                    "SELECT aliases FROM works WHERE title=?", (name,)
                ).fetchone()
                merged = {a for a in (str(row["aliases"]).split(",") if row else []) if a}
                merged.update(a for a in aliases if a != name)
                conn.execute(
                    "INSERT INTO works(id, title, aliases, status, source, created_at, updated_at)"
                    " VALUES(?,?,?,'active',?,?,?)"
                    " ON CONFLICT(title) DO UPDATE SET"
                    "  aliases=excluded.aliases, status='active',"
                    "  source=excluded.source, updated_at=excluded.updated_at",
                    (work_id(name), name, ",".join(sorted(merged)), source, now, now),
                )
                conn.commit()
            return {"id": work_id(name), "title": name, "aliases": sorted(merged)}
        except Exception as e:  # noqa: BLE001 —— 登记失败不影响主流程
            logger.warning("works.register 失败（%s）: %s", name, e)
            return None

    def seed_from_shelf(self) -> int:
        """从只读书架登记作品，并从 ``learned_history`` 补短写别名。

        幂等：重复跑只更新，不重复建。返回本次登记的作品数。
        书架上不存在的名字**不建作品**——避免把新闻栏目当作品。
        """
        count = 0
        try:
            if self._books_root.is_dir():
                for p in sorted(self._books_root.iterdir()):
                    if not p.is_dir() or p.name.startswith("."):
                        continue
                    title = clean_surface(p.name)
                    if title and self.register(title, source="shelf"):
                        count += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("works.seed_from_shelf 扫描书架失败: %s", e)

        # 别名的唯一来源：learned_history 里「书:XXX」且 XXX 是正名的**短写**。
        # 长写（「夜不收：绝命救援」）可能是另一本书，不做别名。
        try:
            with self._connect() as conn:
                self._ensure_schema(conn)
                rows = conn.execute(
                    "SELECT DISTINCT entity FROM learned_history "
                    "WHERE entity LIKE '书:%' LIMIT 500"
                ).fetchall()
            for row in rows:
                alias = clean_surface(str(row["entity"])[2:])
                if not alias:
                    continue
                hit = self.resolve(alias)
                if hit and hit["title"] != alias and alias in hit["title"]:
                    self.register(hit["title"], aliases=[alias], source="learned")
        except Exception as e:  # noqa: BLE001
            logger.warning("works 别名补齐失败（忽略）: %s", e)
        return count

    def ensure_seeded(self, max_age_days: int = 7) -> bool:
        """按需补 seed：距上次重扫超过 ``max_age_days``（默认 7 天）才扫描。

        2026-11-17 复查修正两处：
        1. 初版只在「表为空」时 seed，第一次之后永远不再扫描——新书、改名、
           新增短写别名都进不来，周更钩子形同虚设；
        2. 第二版按 ``source='shelf'`` 的最近 ``updated_at`` 判断，但云端没有
           书架时这个集合永远为空，等于每 tick 全量扫 ``learned_history``
           并逐条开连接。现在用 ``json_blobs[works_seed_ts]`` 记「上次重扫」，
           与有没有书架、有没有 shelf 行无关，扫描频率被真正限在一周一次。

        幂等、失败 fail-open。返回本次是否真的重扫。
        """
        now = datetime.now()
        try:
            with self._connect() as conn:
                self._ensure_schema(conn)
                row = conn.execute(
                    "SELECT data FROM json_blobs WHERE key=?", (_SEED_TS_KEY,)
                ).fetchone()
            last = str((row["data"] if row else "") or "")
            if last:
                try:
                    age = now - datetime.fromisoformat(last)
                    if age.total_seconds() < max_age_days * 86400:
                        return False
                except ValueError:
                    pass
        except Exception as e:  # noqa: BLE001
            logger.warning("works.ensure_seeded 读时间戳失败（继续重扫）: %s", e)

        try:
            return self._seed_and_mark(now)
        except Exception as e:  # noqa: BLE001
            logger.warning("works.ensure_seeded 失败（忽略）: %s", e)
            return False

    def _seed_and_mark(self, now: datetime) -> bool:
        """扫一次书架并记下时间戳；seed 内部已 fail-open，这里只保证记账。"""
        try:
            self.seed_from_shelf()
        finally:
            ts = now.isoformat(timespec="seconds")
            with self._connect() as conn:
                self._ensure_schema(conn)
                conn.execute(
                    "INSERT INTO json_blobs(key, data, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET data=excluded.data, "
                    "updated_at=excluded.updated_at",
                    (_SEED_TS_KEY, ts, ts),
                )
                conn.commit()
        return True

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def list_active(self, limit: int = 200) -> list[dict]:
        try:
            with self._connect() as conn:
                self._ensure_schema(conn)
                rows = conn.execute(
                    "SELECT * FROM works WHERE status='active' ORDER BY title LIMIT ?",
                    (limit,),
                ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("works.list_active 失败（返回空）: %s", e)
            return []
        return [dict(r) for r in rows]

    def resolve(self, ref: str) -> Optional[dict]:
        """surface → 已登记作品（正名 + 别名）。对不上返回 None，不猜。"""
        s = clean_surface(ref)
        if not s:
            return None
        try:
            works = self.list_active(limit=500)
        except Exception:
            return None
        # 1) 正名精确
        for w in works:
            if s == w["title"]:
                return w
        # 2) 别名精确
        for w in works:
            aliases = {clean_surface(a) for a in str(w.get("aliases") or "").split(",") if a}
            if s in aliases:
                return w
        # 3) surface 里含正名（「夜不收那本」「夜不收的第七章」）→ 取最长正名
        contained = [w for w in works if len(w["title"]) >= 2 and w["title"] in s]
        if contained:
            return max(contained, key=lambda w: len(w["title"]))
        # 4) surface 是正名的短写（「国子监」→「国子监秘闻录」）→ 唯一才认
        owns = [w for w in works if len(s) >= 2 and s in w["title"]]
        if len(owns) == 1:
            return owns[0]
        return None

    def aliases_for(self, title: str) -> list[str]:
        name = clean_surface(title)
        try:
            with self._connect() as conn:
                self._ensure_schema(conn)
                row = conn.execute(
                    "SELECT aliases FROM works WHERE title=?", (name,)
                ).fetchone()
        except Exception:
            return []
        if not row:
            return []
        return [a for a in str(row["aliases"] or "").split(",") if a]
