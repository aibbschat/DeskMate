"""DiaryStore：小桔日记的 SQLite 持久层 + 记忆视图导出。

取代原 ``moments.json`` 单文件全量读写，解决两类瓶颈：
- 单 JSON 全量序列化/反序列化 + 全量进内存 + 前端一次拉全 → 改行级写入 + 索引 + 游标分页。
- 🩷 沉淀塞进全局 ``MEMORY.md``（2 万字符尾部截断 + compress/purify 改写会丢/改）
  → 改落 ``loved_entries`` 表，有独立生命周期。

表（均在统一库 ``~/.deskmate/deskmate.db`` 内，与认知层/CognitiveStore 同库）：
- ``user_posts(id, author, content, created_at, loved, archived)``  用户/小桔写的日记
- ``comments(id, moment_id, author, text, created_at)``       双方留言
- ``loved_entries(id, moment_id, content, day, created_at, tags)``  🩷 沉淀（去重、供检索）

迁移：从 ``moments.json`` + ``MEMORY.md`` 的 ``##/§ 日记沉淀`` 块**幂等**导入；
迁移前自动 ``.bak`` 备份 ``moments.json`` 与 ``MEMORY.md``（绝不碰 MEMORY.md 其它内容）。

视图：``export_memory_view()`` 把 ``loved_entries`` 导出为 ``~/.deskmate/diary_memory.md``
（``§ 日记沉淀 #moment #diary`` 分节），供 ``memory/manager.prefetch`` 检索，
复用现有 § 分节体系，**零改 manager 检索核心**。

fail-open：任何 DB 异常都不向上抛，保证主聊天链路不被日记存储拖垮。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

_DEFAULT_DIR = Path.home() / ".deskmate"
# 2026-08-31：日记表并入统一库 deskmate.db（与认知层/CognitiveStore、profile 类 JSON 同库），
# 收口成单一 SQLite，备份是同一原子单元。旧 diary.db 首次启动自动迁移并备份重命名。
_DIARY_DB = "deskmate.db"
_OLD_DIARY_DB = "diary.db"
_DIARY_MEMORY_VIEW = "diary_memory.md"
_MOMENTS_JSON = "moments.json"

# 导出视图里保留的沉淀天数窗口（超出则不再进检索视图，避免无限膨胀）
_VIEW_DAYS = 365


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    # 微秒精度：同秒写入也能保持时间倒序（feed 顺序自然），且游标比较严格无歧义
    return datetime.datetime.now().astimezone().isoformat(timespec="microseconds")


def _today() -> str:
    return datetime.date.today().isoformat()


def _norm(text: str) -> str:
    """去全部空白，仅用于内容去重比对（不用于展示）。"""
    return re.sub(r"\s+", "", text or "")


def _clean_body(text: str) -> str:
    """清洗用户输入的正文：**保留段落换行**，只压行内空白与过量空行。

    历史坑：这里曾用 ``re.sub(r"\\s+", " ", content)`` —— ``\\s`` 含 ``\\n``，
    用户在输入框里敲的回车被整段压成一行，前端按 ``\\n`` 分行渲染时就只剩一个
    段落（2026-09-05 萧哥反馈「写日记不能分段」）。换行是日记正文的结构，不能压。

    规则：
    - 统一换行符（CRLF/CR → LF）
    - 每行内部连续空格/制表/全角空格压成一个，并去行首尾空白
    - 连续空行最多保留一个（防止尾部一串空行刷屏）
    - 去掉正文首尾的空行
    """
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    blank = 0
    for line in raw.split("\n"):
        line = re.sub(r"[ \t\u3000]+", " ", line).strip()
        if line:
            blank = 0
            out.append(line)
        else:
            blank += 1
            if blank <= 1:
                out.append("")
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


class DiaryStore:
    """SQLite 持久层。所有写操作短连接 + 自动提交（WAL 支持并发读）。"""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.base = Path(base_dir) if base_dir else _DEFAULT_DIR
        self.db_path = self.base / _DIARY_DB
        self.base.mkdir(parents=True, exist_ok=True)
        self._connect().close()  # 触发建库 + 建表
        self._ensure_schema()
        # 并入统一库：把旧 diary.db 的日记/留言/🩷 沉淀迁进来（幂等、备份）
        self._migrate_from_old_diary_db()

    # ------------------------------------------------------------------
    # 连接 / schema
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_posts (
                    id         TEXT PRIMARY KEY,
                    author     TEXT NOT NULL DEFAULT 'me',
                    content    TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    loved      INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_posts_created ON user_posts(created_at DESC)"
            )
            # 每条日记独立归档标记（与🩷、回复并列的卡片操作；仅影响时间线展示）
            try:
                conn.execute(
                    "ALTER TABLE user_posts ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
                )
            except Exception:
                pass  # 列已存在则跳过（幂等）
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS comments (
                    id         TEXT PRIMARY KEY,
                    moment_id  TEXT NOT NULL,
                    author     TEXT NOT NULL,
                    text       TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_comments_moment ON comments(moment_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS loved_entries (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    moment_id  TEXT,
                    content    TEXT NOT NULL,
                    day        TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    tags       TEXT NOT NULL DEFAULT '#moment #diary'
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_loved_day ON loved_entries(day DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_loved_content ON loved_entries(content)"
            )

    # ------------------------------------------------------------------
    # 迁移：旧 diary.db → 统一库 deskmate.db
    # ------------------------------------------------------------------
    def _migrate_from_old_diary_db(self) -> None:
        """把旧 ``diary.db`` 的 user_posts/comments/loved_entries 并入统一库。

        - 幂等：若统一库已有 user_posts 数据（已并入），直接备份弃用旧库。
        - 迁移后把旧库重命名为 ``diary.db.bak-<ts>``（绝不删除），留作安全网。
        - fail-open：任何异常跳过，绝不影响新库使用。
        """
        old = self.base / _OLD_DIARY_DB
        if not old.is_file():
            return
        try:
            with self._connect() as conn:
                already = conn.execute("SELECT 1 FROM user_posts LIMIT 1").fetchone()
            if already:
                # 已并入：旧库可弃
                self._backup_old_diary(old)
                return
            src = sqlite3.connect(str(old))
            try:
                posts = src.execute(
                    "SELECT id, author, content, created_at, loved, archived "
                    "FROM user_posts"
                ).fetchall()
                comments = src.execute(
                    "SELECT id, moment_id, author, text, created_at FROM comments"
                ).fetchall()
                loved = src.execute(
                    "SELECT moment_id, content, day, created_at, tags FROM loved_entries"
                ).fetchall()
            finally:
                src.close()
            with self._connect() as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO user_posts"
                    "(id, author, content, created_at, loved, archived) VALUES(?,?,?,?,?,?)",
                    posts,
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO comments"
                    "(id, moment_id, author, text, created_at) VALUES(?,?,?,?,?)",
                    comments,
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO loved_entries"
                    "(moment_id, content, day, created_at, tags) VALUES(?,?,?,?,?)",
                    loved,
                )
            self.export_memory_view()
            self._backup_old_diary(old)
        except Exception:
            return

    @staticmethod
    def _backup_old_diary(path: Path) -> None:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = path.with_name(f"{path.name}.bak-{ts}")
        n = 0
        while bak.exists():
            n += 1
            bak = path.with_name(f"{path.name}.bak-{ts}-{n}")
        try:
            path.rename(bak)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 写：用户/小桔日记
    # ------------------------------------------------------------------
    def add_user_post(self, content: str, author: str = "me") -> dict[str, Any]:
        post = {
            "id": _uid(),
            "author": author if author in ("me", "xiaoju") else "me",
            # 保留段落换行（历史 bug：曾在此把 \s+ 压成单空格，回车被吃掉）
            "content": _clean_body(content),
            "createdAt": _now_iso(),
        }
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO user_posts(id, author, content, created_at, loved) VALUES(?,?,?,?,0)",
                (post["id"], post["author"], post["content"], post["createdAt"]),
            )
        return post

    # ------------------------------------------------------------------
    # 写：留言
    # ------------------------------------------------------------------
    def add_comment(self, moment_id: str, author: str, text: str) -> dict[str, Any]:
        c = {
            "id": _uid(),
            "moment_id": moment_id,
            "author": author if author in ("me", "xiaoju") else "me",
            "text": _clean_body(text),
            "createdAt": _now_iso(),
        }
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO comments(id, moment_id, author, text, created_at) VALUES(?,?,?,?,?)",
                (c["id"], c["moment_id"], c["author"], c["text"], c["createdAt"]),
            )
        return c

    # ------------------------------------------------------------------
    # 写：🩷 落记忆（取代原 MEMORY.md 追加）
    # ------------------------------------------------------------------
    def love(self, moment_id: str, content: str) -> dict[str, Any]:
        """把一条日记沉淀落 loved_entries；去重；维护 per-moment loved 标记；导出视图。

        返回 {loved, memorized}：memorized=True 表示本次新增（非重复）。
        """
        if not content or not content.strip():
            return {"loved": True, "memorized": False}
        norm = _norm(content)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM loved_entries WHERE content = ? LIMIT 1",
                (content.strip(),),
            ).fetchone()
            # 归一化去重（兼容历史写法差异）
            if row is None:
                for (existing,) in conn.execute("SELECT content FROM loved_entries"):
                    if _norm(existing) == norm:
                        row = (1,)
                        break
            memorized = row is None
            if memorized:
                conn.execute(
                    "INSERT INTO loved_entries(moment_id, content, day, created_at, tags) "
                    "VALUES(?,?,?,?,?)",
                    (moment_id, content.strip(), _today(), _now_iso(), "#moment #diary"),
                )
            if moment_id:
                conn.execute(
                    "UPDATE user_posts SET loved = 1 WHERE id = ?", (moment_id,)
                )
        self.export_memory_view()
        return {"loved": True, "memorized": memorized}

    def unlove(self, moment_id: str) -> bool:
        """取消一条日记的『聊天主动回忆』（静音）。

        从 loved_entries 移除该 moment 的沉淀、把 user_posts.loved 置 0、
        并重导 diary_memory.md 视图。返回是否有行受影响。fail-open 由调用方兜底。
        """
        if not moment_id:
            return False
        with self._connect() as conn:
            conn.execute("DELETE FROM loved_entries WHERE moment_id = ?", (moment_id,))
            conn.execute("UPDATE user_posts SET loved = 0 WHERE id = ?", (moment_id,))
        self.export_memory_view()
        return True

    def get_loved_ids(self) -> set[str]:
        """所有已🩷 的 moment_id 集合（含小桔日记卡的 xj-* id）。

        用于前端按 id 回显🩷 状态——之前小桔日记卡 loved 写死 false、
        从不读回，导致🩷 刷新即丢。有了这个集合，前端就能按 eid 还原。
        """
        try:
            with self._connect() as conn:
                return {
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT moment_id FROM loved_entries WHERE moment_id IS NOT NULL"
                    )
                }
        except Exception:
            return set()

    # ------------------------------------------------------------------
    # 写：每条日记独立归档（与🩷、回复并列的卡片操作）
    # ------------------------------------------------------------------
    def archive(self, moment_id: str, archived: bool = True) -> bool:
        """把单条日记标记为已归档 / 未归档。仅影响时间线展示，不影响🩷记忆检索。

        返回是否有行被更新（moment_id 不存在则返回 False）。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE user_posts SET archived = ? WHERE id = ?",
                (1 if archived else 0, moment_id),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 读：分页
    # ------------------------------------------------------------------
    def get_posts(self, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """按 created_at DESC 分页。cursor=上一页最后一条的 ``created_at|id``。

        用 (created_at, id) 组合游标，避免同秒写入时漏条/重复。
        返回 {items, next_cursor}。items 为 user_posts（含 comments 内联、loved 标记）。
        next_cursor 为 None 表示没有下一页。
        """
        limit = max(1, min(limit, 200))
        # cursor 形如 "created_at|id"；拆分失败则当作无游标（兼容旧客户端）
        cur_ca = cur_id = None
        if cursor and "|" in cursor:
            cur_ca, cur_id = cursor.split("|", 1)
        with self._connect() as conn:
            if cur_ca:
                rows = conn.execute(
                    "SELECT id, author, content, created_at, loved, archived FROM user_posts "
                    "WHERE (created_at, id) < (?, ?) ORDER BY created_at DESC, id DESC LIMIT ?",
                    (cur_ca, cur_id, limit + 1),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, author, content, created_at, loved, archived FROM user_posts "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (limit + 1,),
                ).fetchall()
            comments = conn.execute(
                "SELECT id, moment_id, author, text, created_at FROM comments"
            ).fetchall()
        by_moment: dict[str, list[dict]] = {}
        for cid, mid, cauthor, ctext, cat in comments:
            by_moment.setdefault(mid, []).append(
                {"id": cid, "author": cauthor, "text": ctext, "createdAt": cat}
            )
        items: list[dict] = []
        for rid, rauthor, rcontent, rcreated, rloved, rarchived in rows[:limit]:
            items.append(
                {
                    "id": rid,
                    "author": rauthor,
                    "content": rcontent,
                    "createdAt": rcreated,
                    "loved": bool(rloved),
                    "archived": bool(rarchived),
                    "comments": by_moment.get(rid, []),
                }
            )
        # rows 为 sqlite 元组（SELECT 顺序 id,author,content,created_at,loved）
        # 游标取「最后一条已展示项」的 (created_at, id)，下一页严格更旧 → 不漏条、不重复
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = f"{last[3]}|{last[0]}"
        else:
            next_cursor = None
        # comments 全量映射（含 xj-* 小桔日记留言），供前端按 moment_id 查找；
        # user_posts 自身的留言已在 items 内联，此映射主要为小桔日记（非 user_posts 行）服务。
        return {"items": items, "next_cursor": next_cursor, "comments": by_moment}

    # ------------------------------------------------------------------
    # 读：快照（兼容旧 MomentsStore.snapshot 结构）
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._connect() as conn:
            posts = conn.execute(
                "SELECT id, author, content, created_at, loved, archived FROM user_posts "
                "ORDER BY created_at DESC"
            ).fetchall()
            comments = conn.execute(
                "SELECT id, moment_id, author, text, created_at FROM comments"
            ).fetchall()
        by_moment: dict[str, list[dict]] = {}
        for cid, mid, cauthor, ctext, cat in comments:
            by_moment.setdefault(mid, []).append(
                {"id": cid, "author": cauthor, "text": ctext, "createdAt": cat}
            )
        user_posts = [
            {
                "id": pid,
                "author": pauthor,
                "content": pcontent,
                "createdAt": pcreated,
                "loved": bool(ploved),
                "archived": bool(parchived),
                "comments": by_moment.get(pid, []),
            }
            for pid, pauthor, pcontent, pcreated, ploved, parchived in posts
        ]
        return {
            "userPosts": user_posts,
            "comments": by_moment,
            "loved": [pid for pid, _, _, _, ploved, _ in posts if ploved],
        }

    # ------------------------------------------------------------------
    # 迁移（幂等）
    # ------------------------------------------------------------------
    def migrate_from_legacy(self, backup: bool = True) -> dict[str, Any]:
        """从 moments.json + MEMORY.md 的 ##/§ 日记沉淀 块迁移到统一库 deskmate.db。

        - 幂等：若 user_posts 非空，跳过（已迁移）。
        - 迁移前对 moments.json / MEMORY.md 做 .bak 备份（backup=True）。
        - 从 MEMORY.md 删除沉淀块（只删日记沉淀，不动其它内容）。
        返回 {migrated_posts, migrated_loved, mem_cleaned, backup}。
        """
        with self._connect() as conn:
            already = conn.execute("SELECT 1 FROM user_posts LIMIT 1").fetchone()
        if already:
            return {
                "skipped": True,
                "reason": "user_posts 非空，已迁移",
                "migrated_posts": 0,
                "migrated_loved": 0,
                "mem_cleaned": False,
            }

        result: dict[str, Any] = {
            "migrated_posts": 0,
            "migrated_loved": 0,
            "mem_cleaned": False,
            "backups": [],
        }

        # 1) moments.json
        moments_path = self.base / _MOMENTS_JSON
        if moments_path.is_file():
            if backup:
                result["backups"].append(self._backup(moments_path))
            try:
                data = json.loads(moments_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            with self._connect() as conn:
                for p in data.get("userPosts", []):
                    conn.execute(
                        "INSERT OR IGNORE INTO user_posts(id, author, content, created_at, loved) "
                        "VALUES(?,?,?,?,0)",
                        (
                            p.get("id") or _uid(),
                            p.get("author") or "me",
                            p.get("content", ""),
                            p.get("createdAt") or _now_iso(),
                        ),
                    )
                    result["migrated_posts"] += 1
                for mid, clist in (data.get("comments") or {}).items():
                    for c in clist:
                        conn.execute(
                            "INSERT OR IGNORE INTO comments(id, moment_id, author, text, created_at) "
                            "VALUES(?,?,?,?,?)",
                            (
                                c.get("id") or _uid(),
                                mid,
                                c.get("author") or "me",
                                c.get("text", ""),
                                c.get("createdAt") or _now_iso(),
                            ),
                        )
                loved_ids = set(data.get("loved") or [])
                if loved_ids:
                    conn.executemany(
                        "UPDATE user_posts SET loved = 1 WHERE id = ?",
                        [(i,) for i in loved_ids],
                    )

        # 2) MEMORY.md 里的 ##/§ 日记沉淀 块
        mem_path = self.base / "MEMORY.md"
        if mem_path.is_file():
            mem = mem_path.read_text(encoding="utf-8")
            extracted = self._extract_diary_blocks(mem)
            if extracted:
                if backup:
                    result["backups"].append(self._backup(mem_path))
                # 去重导入 loved_entries
                with self._connect() as conn:
                    for content, day in extracted:
                        row = conn.execute(
                            "SELECT 1 FROM loved_entries WHERE content = ? LIMIT 1",
                            (content,),
                        ).fetchone()
                        if row is None:
                            conn.execute(
                                "INSERT INTO loved_entries(moment_id, content, day, created_at, tags) "
                                "VALUES(?,?,?,?,?)",
                                (None, content, day or _today(), _now_iso(), "#moment #diary"),
                            )
                            result["migrated_loved"] += 1
                # 从 MEMORY.md 删除沉淀块
                cleaned = self._strip_diary_blocks(mem)
                if cleaned != mem:
                    mem_path.write_text(cleaned, encoding="utf-8")
                    result["mem_cleaned"] = True

        self.export_memory_view()
        return result

    @staticmethod
    def _extract_diary_blocks(mem: str) -> list[tuple[str, str]]:
        """从 MEMORY.md 提取 ##/§ 日记沉淀 块的条目，返回 [(content, day)]。"""
        out: list[tuple[str, str]] = []
        # ## 日记沉淀 块（preamble 式，行首 - 🩷 ...）
        for m in re.finditer(
            r"^##\s*日记沉淀\b.*$\n((?:- .*\n?)*)", mem, re.MULTILINE
        ):
            for line in m.group(1).splitlines():
                line = line.strip()
                if not line.startswith("-"):
                    continue
                content, day = _parse_diary_line(line[1:].strip())
                if content:
                    out.append((content, day))
        # § 日记沉淀 分节（到下一个 § 或文件尾）
        # 注意：header 用 [^\n]* 而非 .* —— 本正则带 DOTALL，.* 会跨行吞掉整个块
        for m in re.finditer(
            r"^§\s*日记沉淀\b[^\n]*\n(.*?)(?=\n§ |\Z)", mem, re.MULTILINE | re.DOTALL
        ):
            for line in m.group(1).splitlines():
                line = line.strip()
                if not line:
                    continue
                content, day = _parse_diary_line(line)
                if content:
                    out.append((content, day))
        return out

    @staticmethod
    def _strip_diary_blocks(mem: str) -> str:
        """从 MEMORY.md 删除 ##/§ 日记沉淀 块（仅删沉淀，不动其它内容）。"""
        mem = re.sub(
            r"\n*##\s*日记沉淀\b.*$(?:\n- .*)*", "", mem, flags=re.MULTILINE
        )
        mem = re.sub(
            r"\n*§\s*日记沉淀\b.*$(?:\n(?!§ ).*)*", "", mem, flags=re.MULTILINE
        )
        # 清理多余空行
        mem = re.sub(r"\n{3,}", "\n\n", mem)
        return mem.strip() + "\n" if mem.strip() else mem

    @staticmethod
    def _backup(path: Path) -> str:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = path.with_name(f"{path.name}.bak-{ts}")
        try:
            bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
        return str(bak)

    # ------------------------------------------------------------------
    # 视图导出（供 manager.prefetch 检索）
    # ------------------------------------------------------------------
    def export_memory_view(self) -> str:
        """把 loved_entries 导出为 diary_memory.md（§ 分节，条目空行分隔）。

        每个条目 `YYYY-MM-DD：{content}`，便于 Recall 层 7 天窗口 + 全局关键词检索。
        返回视图文件路径（或空串：无数据）。
        """
        try:
            cutoff = (datetime.date.today() - datetime.timedelta(days=_VIEW_DAYS)).isoformat()
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT content, day FROM loved_entries WHERE day >= ? "
                    "ORDER BY day DESC",
                    (cutoff,),
                ).fetchall()
            if not rows:
                # 无沉淀时清空视图文件（否则磁盘上残留旧内容，unlove 后仍能检索到）
                view_path = self.base / _DIARY_MEMORY_VIEW
                try:
                    if view_path.exists():
                        view_path.unlink()
                except Exception:
                    pass
                return ""
            view_path = self.base / _DIARY_MEMORY_VIEW
            blocks = ["§ 日记沉淀 #moment #diary"]
            for content, day in rows:
                blocks.append(f"\n{day}：{content}\n")
            view_path.write_text("\n".join(blocks).rstrip() + "\n", encoding="utf-8")
            return str(view_path)
        except Exception:
            return ""


def _parse_diary_line(line: str) -> tuple[str, str]:
    """解析一条沉淀行，析出 (content, day)。

    兼容多种写法：
    - `🩷 萧盛的日记（2026-08-31）：第三章改完`
    - `2026-08-31：🩷 萧盛的日记：第三章改完`
    - `🩷 萧盛的日记：第三章改完`（无日期 → day 空）
    """
    content = line.strip()
    day = ""
    m = re.match(r"(\d{4}-\d{1,2}-\d{1,2})[:：]\s*(.*)$", content)
    if m:
        day = m.group(1)
        content = m.group(2).strip()
    else:
        m2 = re.search(r"（(\d{4}-\d{1,2}-\d{1,2})）", content)
        if m2:
            day = m2.group(1)
            content = re.sub(r"（\d{4}-\d{1,2}-\d{1,2}）", "", content).strip()
    return content, day
