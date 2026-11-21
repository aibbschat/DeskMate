"""小桔大脑搬家：整库物理快照 + 底座无关可移植包（底座无关，零 LLM）。

用途（docs/brainstorm/rounds/2026-11-20-小桔大脑可移植性.md / 决策 D4）：
把"她记得什么"（数据资产）从 OpenWorker 整体搬走，为将来换底座保底。

两条路线，不混用：
1. **整库快照**（database/deskmate.db）：sqlite3.Connection.backup() 一致拷贝，
   含 WAL 里未 checkpoint 的行、含向量列。物理搬家**不做** oplog 重放 / 向量重刷——
   那是双端同步策略（sync_store_sqlite 策略①），整库搬迁会把向量原样带走。
2. **可移植包**（portable/*.json）：只含明文业务列，exclude 向量/密钥/同步基础设施，
   给没有 OpenWorker 代码的新底座直接读。

安全红线：
- 密钥（secrets.json / .env / token / mcp.json）、史料库（history/）、历史备份（backups/）
  绝不打包。
- 导入前必须过 manifest + sha256 校验；默认拒绝覆盖非空目标（须显式 force）。
- 本模块只用标准库，不 import coworker.*，本身就是可搬走的资产的一部分。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

BUNDLE_VERSION = 1
MANIFEST_NAME = "manifest.json"
DB_REL = "database/deskmate.db"
PORTABLE_REL = "portable"
FILES_REL = "files"
BOOKS_REL = "books"

# 跨底座的明文资产表（与 sync_store_sqlite.SYNC_TABLES 对齐但不 import 它，保持底座无关）。
# external_items（向量）、intents/intent_outputs（本端产物）、emotion_*（设备本地）不在此列。
PORTABLE_TABLES: tuple[str, ...] = (
    "profile_items",
    "learned_history",
    "loved_entries",
    "user_posts",
    "comments",
    "proactive_ledger",
    "llm_circuit_breaker",
    "memory_anchors",
    "known_persons",
    "teachings",
)

# 散文件白名单：叙事真相源 + 各层 settings + 第一人称素材。
# MEMORY.md/USER.md 整文件带走（搬家=物理迁移，不做跨端 #no-sync 脱敏）。
ROOT_FILES: tuple[str, ...] = (
    "MEMORY.md",
    "USER.md",
    "SOUL.md",
    "consult_runs.json",
    "will_settings.json",
    "l2_settings.json",
    "alter_ego_config.json",
    "ingest_dirs.json",
)
WALK_DIRS: tuple[str, ...] = (
    "diary",
    "vault",
    "agency",
    "auto_tasks",
    "skills",
    "sessions",
)
MEMORY_JSONL = ("learned_history.jsonl", "close_loop_pending.jsonl", "disturb_state.json")

_VECTOR_COL_RE = re.compile(r"(?:^|[_])embed(?:ding)?\b|(?:^|[_])vector\b", re.IGNORECASE)
_FORBIDDEN_RE = re.compile(
    r"(?:^|/)(?:secrets\.json|\.env|mcp\.json|history|backups|_quarantine|models)(?:/|$)"
    r"|token|sidecar|private_key|\.pem$",
    re.IGNORECASE,
)


class BundleIntegrityError(RuntimeError):
    """manifest 或 sha256 校验失败。"""


class TargetNotEmptyError(RuntimeError):
    """目标非空且未显式 force。"""


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_forbidden(rel: str) -> bool:
    return bool(_FORBIDDEN_RE.search(rel))


def _default_base() -> Path:
    raw = os.environ.get("DESKMATE_DIR") or os.environ.get("DESKMATE_MEMORY_DIR")
    return Path(raw) if raw else Path.home() / ".deskmate"


def _business_tables(conn: sqlite3.Connection) -> list[str]:
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    # FTS5 影子表不搬（导入端按明文重建），同步基础设施表进整库快照但不计业务行数。
    return [
        n
        for n in names
        if not re.search(r"_fts(?:_|$)|_data$|_idx$|_content$|_docsize$|_config$", n)
        and not n.startswith("_sync")
    ]


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _backup_db(src: Path, dst: Path) -> None:
    """一致快照：走 sqlite3 backup API，保住 WAL 未 checkpoint 的行。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    src_uri = f"file:{quote(str(src.resolve()))}?mode=ro"
    source = sqlite3.connect(src_uri, uri=True)
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _collect_white_files(base: Path) -> list[Path]:
    out: list[Path] = []
    for name in ROOT_FILES:
        p = base / name
        if p.is_file() and not _is_forbidden(name):
            out.append(p)
    for d in WALK_DIRS:
        root = base / d
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(base).as_posix()
            if not _is_forbidden(rel):
                out.append(p)
    mroot = base / "memory"
    if mroot.is_dir():
        for name in MEMORY_JSONL:
            p = mroot / name
            if p.is_file():
                out.append(p)
    return out


def export_brain(
    base_dir: Path | str,
    out_dir: Path | str,
    *,
    book_dirs: list[Path] | list[str] | None = None,
) -> dict[str, Any]:
    """导出小桔大脑。返回写入的 manifest（dict）。"""
    base = Path(base_dir).expanduser().resolve()
    out = Path(out_dir).expanduser().resolve()
    db = base / "deskmate.db"
    if not db.is_file():
        raise FileNotFoundError(f"统一库不存在：{db}")

    out.mkdir(parents=True, exist_ok=True)
    for sub in (Path(DB_REL).parent, Path(PORTABLE_REL), Path(FILES_REL), Path(BOOKS_REL)):
        (out / sub).mkdir(parents=True, exist_ok=True)

    db_out = out / DB_REL
    _backup_db(db, db_out)

    manifest: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {
            "base_dir_rel": base.name,
            "node": None,
            "schema_version": None,
        },
        "database": {
            "path": DB_REL,
            "sha256": _sha(db_out),
            "bytes": db_out.stat().st_size,
        },
        "files": [],
        "portable": [],
        "books": [],
        "table_rows": {},
    }

    # 一致快照上读元信息（不碰源库，避免 WAL 竞争）。
    conn = sqlite3.connect(f"file:{quote(str(db_out.resolve()))}?mode=ro", uri=True)
    try:
        tables = _business_tables(conn)
        manifest["table_rows"] = {
            t: int(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]) for t in tables
        }
        try:
            manifest["source"]["node"] = conn.execute(
                "SELECT node FROM _sync_meta WHERE id=1"
            ).fetchone()[0]
        except (sqlite3.Error, TypeError, IndexError):
            pass
        try:
            manifest["source"]["schema_version"] = conn.execute(
                "SELECT v FROM _deskmate_schema WHERE k='version'"
            ).fetchone()[0]
        except (sqlite3.Error, TypeError, IndexError):
            pass

        for table in PORTABLE_TABLES:
            if table not in tables:
                continue
            cols = [c for c in _table_columns(conn, table) if not _VECTOR_COL_RE.search(c)]
            rows = [
                dict(zip(cols, row))
                for row in conn.execute(
                    'SELECT ' + ",".join(f'"{c}"' for c in cols) + f' FROM "{table}"'
                )
            ]
            rel = f"{PORTABLE_REL}/{table}.json"
            fp = out / rel
            fp.write_text(
                json.dumps({"table": table, "rows": rows}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            manifest["portable"].append(
                {"path": rel, "table": table, "rows": len(rows), "sha256": _sha(fp)}
            )
    finally:
        conn.close()

    for src in _collect_white_files(base):
        rel = f"{FILES_REL}/{src.relative_to(base).as_posix()}"
        dst = out / rel
        _copy_file(src, dst)
        manifest["files"].append(
            {"path": rel, "sha256": _sha(dst), "bytes": dst.stat().st_size}
        )

    for book in book_dirs or []:
        book = Path(book).expanduser().resolve()
        bdb = book / ".deskmate" / "entities.db"
        if not bdb.is_file():
            continue
        dst_dir = out / BOOKS_REL / book.name
        dst_dir.mkdir(parents=True, exist_ok=True)
        bdst = dst_dir / "entities.db"
        _backup_db(bdb, bdst)
        entry: dict[str, Any] = {
            "name": book.name,
            "path": f"{BOOKS_REL}/{book.name}/entities.db",
            "sha256": _sha(bdst),
            "aux": [],
        }
        for aux in ("CHARACTERS.md", "NOTES.md", "OUTLINE.md", "TIMELINE.md", "fts5.db"):
            src = book / ".deskmate" / aux
            if src.is_file():
                rel = f"{BOOKS_REL}/{book.name}/{aux}"
                _copy_file(src, out / rel)
                entry["aux"].append({"path": rel, "sha256": _sha(out / rel)})
        manifest["books"].append(entry)

    mf_path = out / MANIFEST_NAME
    mf_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def verify_bundle(bundle_dir: Path | str) -> dict[str, Any]:
    """校验 manifest + 每个文件的 sha256。失败抛 BundleIntegrityError。"""
    out = Path(bundle_dir).expanduser().resolve()
    mf_path = out / MANIFEST_NAME
    if not mf_path.is_file():
        raise BundleIntegrityError(f"缺少 {MANIFEST_NAME}：{out}")
    try:
        mf = json.loads(mf_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleIntegrityError(f"manifest 无法解析：{exc}") from exc
    if mf.get("bundle_version") != BUNDLE_VERSION:
        raise BundleIntegrityError(
            f"bundle_version 不匹配：{mf.get('bundle_version')} != {BUNDLE_VERSION}"
        )

    checks = [mf.get("database", {})]
    checks.extend(mf.get("files", []))
    checks.extend(mf.get("portable", []))
    checks.extend(mf.get("books", []))
    for book in mf.get("books", []):
        checks.extend(book.get("aux", []))

    for entry in checks:
        rel = entry.get("path")
        if not rel:
            continue
        fp = out / rel
        if not fp.is_file():
            raise BundleIntegrityError(f"包内文件缺失：{rel}")
        expected = entry.get("sha256")
        if expected and _sha(fp) != expected:
            raise BundleIntegrityError(f"sha256 不匹配：{rel}")
    return mf


def import_brain(
    bundle_dir: Path | str,
    target_dir: Path | str,
    *,
    force: bool = False,
    portable_only: bool = False,
    books_root: Path | str | None = None,
) -> dict[str, Any]:
    """导入大脑。默认拒绝覆盖非空目标；portable_only 只落明文包、不碰数据库。"""
    out = Path(bundle_dir).expanduser().resolve()
    target = Path(target_dir).expanduser().resolve()
    mf = verify_bundle(out)

    if not portable_only and target.exists() and any(target.iterdir()):
        if not force:
            raise TargetNotEmptyError(f"目标非空，须显式 force=True：{target}")

    target.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "mode": "portable_only" if portable_only else "full",
        "bundle_version": mf["bundle_version"],
        "source_node": mf.get("source", {}).get("node"),
        "imported_tables": 0,
        "table_rows": {},
        "files": 0,
        "books": 0,
        "target": str(target),
    }

    if portable_only:
        psrc = out / PORTABLE_REL
        pdst = target / PORTABLE_REL
        pdst.mkdir(parents=True, exist_ok=True)
        for entry in mf.get("portable", []):
            _copy_file(out / entry["path"], pdst / Path(entry["path"]).name)
        report["portable_files"] = len(mf.get("portable", []))
        return report

    # 整库恢复：先写临时文件再原子替换，避免半截库。
    db_dst = target / "deskmate.db"
    with tempfile.TemporaryDirectory(dir=str(target)) as td:
        tmp_db = Path(td) / "deskmate.db"
        _backup_db(out / mf["database"]["path"], tmp_db)
        if _sha(tmp_db) != mf["database"]["sha256"]:
            raise BundleIntegrityError("快照写入后 sha256 变化，导入中止")
        os.replace(tmp_db, db_dst)

    conn = sqlite3.connect(f"file:{quote(str(db_dst.resolve()))}?mode=ro", uri=True)
    try:
        tables = _business_tables(conn)
        report["imported_tables"] = len(tables)
        report["table_rows"] = {
            t: int(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]) for t in tables
        }
    finally:
        conn.close()

    for entry in mf.get("files", []):
        rel = Path(entry["path"]).relative_to(FILES_REL).as_posix()
        _copy_file(out / entry["path"], target / rel)
        report["files"] += 1

    root = Path(books_root).expanduser().resolve() if books_root else target.parent / BOOKS_REL
    for book in mf.get("books", []):
        dst_dir = root / book["name"] / ".deskmate"
        dst_dir.mkdir(parents=True, exist_ok=True)
        _backup_db(out / book["path"], dst_dir / "entities.db")
        for aux in book.get("aux", []):
            _copy_file(out / aux["path"], root / book["name"] / ".deskmate" / Path(aux["path"]).name)
        report["books"] += 1

    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="deskmate-migrate",
        description="小桔大脑搬家：整库快照 + 底座无关可移植包",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="导出大脑到目录")
    e.add_argument("--base", default=str(_default_base()), help="源 ~/.deskmate（默认读 DESKMATE_DIR）")
    e.add_argument("--out", required=True, help="输出目录")
    e.add_argument("--book", action="append", default=[], dest="books", help="小说根目录，可重复")

    v = sub.add_parser("verify", help="校验包完整性")
    v.add_argument("bundle", help="包目录")

    i = sub.add_parser("import", help="导入大脑")
    i.add_argument("bundle", help="包目录")
    i.add_argument("--target", default=str(_default_base()), help="目标 ~/.deskmate")
    i.add_argument("--force", action="store_true", help="允许覆盖非空目标")
    i.add_argument("--portable-only", action="store_true", help="只落明文可移植包，不碰数据库")
    i.add_argument("--books-root", default=None, help="圣经索引落地根（默认 <target>/../books）")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "export":
            mf = export_brain(Path(args.base), Path(args.out), book_dirs=[Path(b) for b in args.books])
            print(json.dumps(mf, ensure_ascii=False, indent=2))
        elif args.cmd == "verify":
            mf = verify_bundle(Path(args.bundle))
            print(f"OK bundle_version={mf['bundle_version']} tables={len(mf.get('table_rows', {}))}")
        else:
            r = import_brain(
                Path(args.bundle),
                Path(args.target),
                force=args.force,
                portable_only=args.portable_only,
                books_root=Path(args.books_root) if args.books_root else None,
            )
            print(json.dumps(r, ensure_ascii=False, indent=2))
    except (BundleIntegrityError, TargetNotEmptyError, FileNotFoundError, OSError, sqlite3.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
