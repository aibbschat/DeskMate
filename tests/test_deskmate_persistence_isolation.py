"""BlobStore 隔离契约回归（2026-09-21 现场）。

根因
----
``deskmate/persistence.py`` 的默认库路径曾是**模块级常量**
``_DB_DEFAULT = Path.home() / ".deskmate" / "deskmate.db"``，完全不认
``DESKMATE_DIR``。而同一个物理库的另一个入口 ``CognitiveStore`` 走的是
``deskmate.paths.deskmate_db_path()``（认环境变量）——两者注释都写"保持一致的
同一物理文件"，实现却分叉了。

后果
----
conftest 的 autouse 隔离（``monkeypatch.setenv("DESKMATE_DIR", tmp_path)``）
对 BlobStore 全线失效：跑一次全量 pytest，下面这些 key 会被写进开发者**真实库**

    KEY_GROWTH / KEY_STYLE / KEY_DECISION / KEY_EMOTION / KEY_CARE_LOG / KEY_GARDEN

现场取证：全量 pytest 之后 ``json_blobs.updated_at`` 变成测试窗口内的时刻
（``decision_profile`` / ``style_profile`` 同一秒、``growth`` 紧随），而**表行数
一条不变**——因为 json_blobs 是 upsert。所以从行数看完全正常，只有把 mtime 与
``updated_at`` 摆在一起反查窗口才抓得到。

本文件锁死一条不变量：
**只要 ``DESKMATE_DIR`` 指向别处，BlobStore 就绝不能落到 ``~/.deskmate``。**
"""

from __future__ import annotations

from pathlib import Path

from deskmate import persistence as P
from deskmate.persistence import BlobStore

REAL_DB = Path.home() / ".deskmate" / "deskmate.db"


class TestDefaultPathFollowsEnv:
    def test_default_path_uses_deskmate_dir(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("DESKMATE_DIR", str(tmp_path / "dm"))
        assert P._default_db_path() == tmp_path / "dm" / "deskmate.db"

    def test_blobstore_default_follows_deskmate_dir(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("DESKMATE_DIR", str(tmp_path / "dm"))
        assert BlobStore().db_path == tmp_path / "dm" / "deskmate.db"

    def test_alias_env_also_honoured(self, tmp_path: Path, monkeypatch):
        """``DESKMATE_MEMORY_DIR`` 是兼容别名（paths.deskmate_base_dir 认）。"""
        monkeypatch.delenv("DESKMATE_DIR", raising=False)
        monkeypatch.setenv("DESKMATE_MEMORY_DIR", str(tmp_path / "alias"))
        assert BlobStore().db_path == tmp_path / "alias" / "deskmate.db"

    def test_explicit_path_still_wins(self, tmp_path: Path):
        """显式传参（CLI / 测试）必须压过环境变量。"""
        p = tmp_path / "custom.db"
        assert BlobStore(db_path=p).db_path == p


class TestNoModuleLevelConstant:
    def test_legacy_constant_is_gone(self):
        """回归锁：模块级常量会让 autouse 隔离在 import 期就被绕过。

        （import 发生在测试收集阶段，此时 conftest 的 fixture 还没机会 setenv。）
        """
        assert not hasattr(P, "_DB_DEFAULT"), (
            "persistence._DB_DEFAULT 又回来了——它是模块级常量，import 时即绑定真实 ~/.deskmate，"
            "会让 conftest 的 DESKMATE_DIR 隔离对 BlobStore 全线失效"
        )

    def test_every_blob_key_resolves_under_isolation(self, tmp_path: Path, monkeypatch):
        """六个 key 共用同一个 db_path——它必须落在隔离目录内。"""
        monkeypatch.setenv("DESKMATE_DIR", str(tmp_path / "dm"))
        store = BlobStore()
        for key in (
            store.KEY_GROWTH,
            store.KEY_STYLE,
            store.KEY_DECISION,
            store.KEY_EMOTION,
            store.KEY_CARE_LOG,
            store.KEY_GARDEN,
        ):
            store.set(key, {"k": key})
        assert store.db_path.parent == tmp_path / "dm"


class TestRealLibraryUntouched:
    def test_writing_in_isolation_does_not_touch_real_db(self, tmp_path: Path, monkeypatch):
        """端到端取证法：隔离环境下写 blob，真实库的 mtime 不许变。

        这条断言就是当初发现问题的那个手法（只看行数会漏，必须看 mtime）。
        真实库不存在时直接跳过。
        """
        if not REAL_DB.exists():
            import pytest

            pytest.skip("本机没有真实库，跳过")

        before = REAL_DB.stat().st_mtime_ns
        monkeypatch.setenv("DESKMATE_DIR", str(tmp_path / "dm"))
        store = BlobStore()
        store.set(store.KEY_GROWTH, {"version": 2, "trust": 0.0, "mood": 0.0})
        assert store.get(store.KEY_GROWTH)["trust"] == 0.0

        after = REAL_DB.stat().st_mtime_ns
        assert after == before, (
            "隔离环境下写 BlobStore 却改动了真实库 mtime——"
            "说明有代码绕过了 DESKMATE_DIR（模块级常量 / 硬编码 ~/.deskmate）"
        )
