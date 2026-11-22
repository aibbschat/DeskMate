"""小桔大脑搬家 CLI 的先红后绿契约。

三层判定（见 docs/brainstorm/rounds/2026-11-20-小桔大脑可移植性.md）：
① 数据资产可整体搬；② 纯逻辑随代码走；③ 接口层换底座重写。
本文件只锁 ① 的搬运机制：整库 .backup + 底座无关可移植包 + 散文件白名单 + 校验。

关键判据（不要改成"看起来对"的弱断言）：
- 导出必须走 sqlite3 .backup：WAL 里未 checkpoint 的行必须出现在包里（cp 主文件会丢）。
- 整库迁移不得重放 oplog / 重刷向量：那是双端同步策略，物理搬家会把向量一起搬走。
- 密钥 / 史料库 / 历史备份绝不打包。
- 导入前必须过 manifest + sha256 校验，且默认拒绝覆盖非空目标。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from deskmate.migration import (
    BUNDLE_VERSION,
    MANIFEST_NAME,
    PORTABLE_TABLES,
    BundleIntegrityError,
    TargetNotEmptyError,
    export_brain,
    import_brain,
    verify_bundle,
)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _seed_db(path: Path, *, wal_row: str | None = None) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE profile_items (
            id TEXT PRIMARY KEY, layer TEXT, dimension TEXT,
            content TEXT, status TEXT, sync_uuid TEXT
        );
        CREATE TABLE learned_history (
            id INTEGER PRIMARY KEY, fact TEXT, fp TEXT UNIQUE, sync_uuid TEXT
        );
        CREATE TABLE json_blobs (key TEXT PRIMARY KEY, data TEXT, updated_at TEXT);
        CREATE TABLE user_posts (
            id TEXT PRIMARY KEY, content TEXT, created_at TEXT, sync_uuid TEXT
        );
        CREATE TABLE proactive_ledger (
            id TEXT PRIMARY KEY, kind TEXT, content TEXT, status TEXT, sync_uuid TEXT
        );
        CREATE TABLE external_items (
            id TEXT PRIMARY KEY, content TEXT, embedding BLOB, embedding_model TEXT
        );
        CREATE TABLE _sync_meta (id INTEGER PRIMARY KEY CHECK(id=1), node TEXT, hlc_counter INTEGER);
        CREATE TABLE _sync_oplog (seq INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT, op TEXT);
        """
    )
    conn.execute(
        "INSERT INTO profile_items VALUES (?,?,?,?,?,?)",
        ("p1", "cognitive", "stance", "测试立场", "active", "u-p1"),
    )
    conn.execute(
        "INSERT INTO learned_history VALUES (?,?,?,?)",
        (1, "测试事实", "fp-1", "u-l1"),
    )
    conn.execute(
        "INSERT INTO json_blobs VALUES (?,?,?)",
        ("style_profile", '{"tone":"calm"}', "2026-11-20T00:00:00"),
    )
    conn.execute(
        "INSERT INTO external_items VALUES (?,?,?,?)",
        ("e1", "外部资料", b"\x00\x01\x02", "text-embedding-3-small"),
    )
    conn.execute("INSERT INTO _sync_meta VALUES (1,?,?)", ("node-test", 7))
    conn.commit()
    if wal_row:
        # 保持连接打开、不 checkpoint：这行只在 -wal 里，主库文件读不到。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO profile_items VALUES (?,?,?,?,?,?)",
            (wal_row, "worldview", "value", "只在 WAL", "active", "u-wal"),
        )
        conn.commit()
    return conn


@pytest.fixture
def source(tmp_path: Path):
    base = tmp_path / "source" / "deskmate"
    conn = _seed_db(base / "deskmate.db", wal_row="p-wal")
    (base / "MEMORY.md").write_text("# 记忆\n\n关于他的一部分\n\n#no-sync\n私密段落\n", encoding="utf-8")
    (base / "USER.md").write_text("# 萧盛\n", encoding="utf-8")
    (base / "SOUL.md").write_text("# 小桔\n", encoding="utf-8")
    (base / "will_settings.json").write_text("{}", encoding="utf-8")
    (base / "diary").mkdir()
    (base / "diary" / "2026-11-20.md").write_text("日记正文", encoding="utf-8")
    (base / "vault").mkdir()
    (base / "vault" / "小桔眼中的萧盛.md").write_text("静态档案", encoding="utf-8")

    # 明确不得打包的东西
    (base / "secrets.json").write_text("API_KEY_SHOULD_NOT_LEAK", encoding="utf-8")
    (base / ".env").write_text("TOKEN=leak", encoding="utf-8")
    (base / "history").mkdir()
    (base / "history" / "huge.db").write_bytes(b"x" * 2048)
    (base / "backups").mkdir()
    (base / "backups" / "old.bak").write_bytes(b"y" * 2048)

    # 每本书的圣经索引（不在统一库里）
    book = tmp_path / "source" / "books" / "国子监"
    bd = book / ".deskmate"
    bd.mkdir(parents=True)
    bc = sqlite3.connect(str(bd / "entities.db"))
    bc.execute("CREATE TABLE entities (id TEXT PRIMARY KEY, name TEXT)")
    bc.execute("INSERT INTO entities VALUES (?,?)", ("e1", " Character "))
    bc.commit()
    bc.close()
    (bd / "CHARACTERS.md").write_text("人物", encoding="utf-8")

    yield base, book, conn
    conn.close()


def test_export_manifest_records_version_node_sha256_and_table_rows(tmp_path: Path, source):
    base, book, _conn = source
    bundle = tmp_path / "bundle"
    manifest = export_brain(base, bundle, book_dirs=[book])

    assert manifest["bundle_version"] == BUNDLE_VERSION
    assert manifest["source"]["node"] == "node-test"
    assert manifest["source"]["base_dir_rel"] == "deskmate"
    assert manifest["created_at"]

    mf = json.loads((bundle / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert mf == manifest
    assert mf["database"]["sha256"] == _sha(bundle / mf["database"]["path"])
    assert mf["database"]["bytes"] > 0
    for entry in mf["files"]:
        assert entry["sha256"] == _sha(bundle / entry["path"]), entry["path"]
    assert mf["table_rows"]["profile_items"] == 2  # WAL 那行也进来了
    assert mf["books"][0]["name"] == "国子监"


def test_export_uses_sqlite_backup_preserves_uncheckpointed_wal(tmp_path: Path, source):
    """cp 主文件会丢 WAL 行；.backup 不会。这条锁死搬运机制。"""
    _base, _book, _conn = source
    bundle = tmp_path / "bundle"
    export_brain(_base, bundle)

    conn = sqlite3.connect(str(bundle / "database" / "deskmate.db"))
    try:
        rows = conn.execute(
            "SELECT id FROM profile_items WHERE id='p-wal'"
        ).fetchall()
        assert rows == [("p-wal",)]
    finally:
        conn.close()


def test_export_excludes_secrets_history_and_backups(tmp_path: Path, source):
    base, _book, _conn = source
    bundle = tmp_path / "bundle"
    export_brain(base, bundle)

    packed = [str(p.relative_to(bundle)) for p in bundle.rglob("*")]
    # 按路径组件判断，不能用子串（learned_history.json 含 history 字样）。
    for rel in packed:
        assert not rel.endswith("secrets.json"), rel
        assert not rel.endswith(".env"), rel
        assert not rel.startswith("files/history/"), rel
        assert not rel.startswith("files/backups/"), rel
    # 白名单文件必须在
    assert "files/MEMORY.md" in packed
    assert "files/diary/2026-11-20.md" in packed
    assert "files/vault/小桔眼中的萧盛.md" in packed


def test_export_portable_bundle_is_base_agnostic_and_keeps_vectors_out(tmp_path: Path, source):
    """可移植包只含明文业务行：底座无关；向量列属整库快照，不进可移植包。"""
    base, _book, _conn = source
    bundle = tmp_path / "bundle"
    export_brain(base, bundle)

    pdir = bundle / "portable"
    names = sorted(p.name for p in pdir.glob("*.json"))
    assert "profile_items.json" in names
    assert "learned_history.json" in names
    assert "external_items.json" not in names  # 向量表不是跨底座资产
    data = json.loads((pdir / "profile_items.json").read_text(encoding="utf-8"))
    assert data["table"] == "profile_items"
    assert len(data["rows"]) == 2
    assert all("embedding" not in json.dumps(row) for row in data["rows"])
    assert set(PORTABLE_TABLES) >= {"profile_items", "learned_history", "user_posts", "proactive_ledger"}


def test_verify_bundle_detects_tampering(tmp_path: Path, source):
    base, _book, _conn = source
    bundle = tmp_path / "bundle"
    export_brain(base, bundle)
    verify_bundle(bundle)  # 绿的先过

    (bundle / "files" / "MEMORY.md").write_text("被篡改", encoding="utf-8")
    with pytest.raises(BundleIntegrityError):
        verify_bundle(bundle)


def test_import_roundtrip_restores_tables_files_and_books(tmp_path: Path, source):
    base, book, _conn = source
    bundle = tmp_path / "bundle"
    export_brain(base, bundle, book_dirs=[book])

    target = tmp_path / "target" / "deskmate"
    report = import_brain(bundle, target)

    # 6 张业务表 + 整库快照里的 _sync_meta/_sync_oplog（基础设施表不计入 imported_tables）
    assert report["imported_tables"] == 6
    assert report["table_rows"]["profile_items"] == 2
    tc = sqlite3.connect(str(target / "deskmate.db"))
    try:
        assert tc.execute("SELECT node FROM _sync_meta WHERE id=1").fetchone() == ("node-test",)
        assert tc.execute("SELECT content FROM profile_items WHERE id='p1'").fetchone() == ("测试立场",)
        blob = json.loads(tc.execute("SELECT data FROM json_blobs WHERE key='style_profile'").fetchone()[0])
        assert blob == {"tone": "calm"}
        vec = tc.execute("SELECT embedding, embedding_model FROM external_items WHERE id='e1'").fetchone()
        assert bytes(vec[0]) == b"\x00\x01\x02"  # 向量随整库走，不重算
    finally:
        tc.close()
    assert (target / "MEMORY.md").read_text(encoding="utf-8").startswith("# 记忆")
    assert (target / "diary" / "2026-11-20.md").read_text(encoding="utf-8") == "日记正文"
    assert not (target / "secrets.json").exists()
    assert (tmp_path / "target" / "books" / "国子监" / ".deskmate" / "entities.db").exists()


def test_import_refuses_nonempty_target_without_force(tmp_path: Path, source):
    base, _book, _conn = source
    bundle = tmp_path / "bundle"
    export_brain(base, bundle)

    target = tmp_path / "target" / "deskmate"
    target.mkdir(parents=True)
    (target / "USER.md").write_text("existing", encoding="utf-8")
    with pytest.raises(TargetNotEmptyError):
        import_brain(bundle, target)
    # force 才允许覆盖
    report = import_brain(bundle, target, force=True)
    assert (target / "USER.md").read_text(encoding="utf-8") == "# 萧盛\n"


def test_import_portable_only_never_touches_database(tmp_path: Path, source):
    base, _book, _conn = source
    bundle = tmp_path / "bundle"
    export_brain(base, bundle)

    target = tmp_path / "new-base" / "imported"
    report = import_brain(bundle, target, portable_only=True)
    assert report["mode"] == "portable_only"
    assert not (target / "deskmate.db").exists()
    assert (target / "portable" / "profile_items.json").exists()
