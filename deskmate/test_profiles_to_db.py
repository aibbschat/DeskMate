"""profile 类 JSON 收进统一库 deskmate.db 的迁移 / 往返测试。

覆盖：
- growth.json / style_profile.json / decision_profile.json 经 BlobStore 迁移进 deskmate.db
  （默认路径时），load/save 往返一致；legacy 文件迁移后备份重命名（不再以 .json 存在）。
- diary.db 旧库并入侵统一库（user_posts/comments/loved_entries 全量迁入，旧库备份重命名）。

测试密闭：通过把各 profile 模块的 BlobStore 引用与 legacy 路径重定向到临时目录，
绝不触碰真实 ~/.deskmate。
"""

import json
import sqlite3
import tempfile
from pathlib import Path

from deskmate.persistence import BlobStore


def _hermetic(tmp: Path):
    """把三个 profile 模块的 BlobStore + legacy 路径重定向到临时库，返回还原用快照。"""
    from deskmate.growth import state as stmod
    from deskmate.alter_ego import decision_profile as dmod
    from deskmate import style_profile_store as smod

    db = tmp / "deskmate.db"

    class _TmpBlob(BlobStore):
        def __init__(self):
            super().__init__(db_path=db)

    saved = {
        "st_BlobStore": stmod.BlobStore,
        "st_base": stmod._default_base_dir,
        "d_BlobStore": dmod.BlobStore,
        "d_path": dmod.DEFAULT_PROFILE_PATH,
        "s_BlobStore": smod.BlobStore,
        "s_path": smod.DEFAULT_PROFILE_PATH,
    }
    stmod.BlobStore = _TmpBlob
    stmod._default_base_dir = lambda: tmp
    dmod.BlobStore = _TmpBlob
    dmod.DEFAULT_PROFILE_PATH = tmp / "decision_profile.json"
    smod.BlobStore = _TmpBlob
    smod.DEFAULT_PROFILE_PATH = tmp / "style_profile.json"
    return saved, db


def _restore(saved):
    from deskmate.growth import state as stmod
    from deskmate.alter_ego import decision_profile as dmod
    from deskmate import style_profile_store as smod

    stmod.BlobStore = saved["st_BlobStore"]
    stmod._default_base_dir = saved["st_base"]
    dmod.BlobStore = saved["d_BlobStore"]
    dmod.DEFAULT_PROFILE_PATH = saved["d_path"]
    smod.BlobStore = saved["s_BlobStore"]
    smod.DEFAULT_PROFILE_PATH = saved["s_path"]


def test_growth_json_migrates_to_db_and_roundtrip():
    tmp = Path(tempfile.mkdtemp())
    legacy = tmp / "growth.json"
    legacy.write_text(
        json.dumps(
            {
                "version": 1,
                "intimacy": 42.0,
                "trust": 0.8,
                "empathy": 10.0,
                "mood": 70.0,
                "vitality": 90.0,
                "level": 3,
                "growth_value": 55,
                "counters": {"edited_chapters": 7},
                "badges_unlocked": {"first_chat": "2026-01-01"},
                "empathy_domains": {"creation": 50.0},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    saved, db = _hermetic(tmp)
    try:
        from deskmate.growth.state import load_state, save_state

        st = load_state()  # 默认 → 临时库，触发迁移
        assert st.intimacy == 42.0, "迁移后字段未还原"
        assert st.badges_unlocked.get("first_chat") == "2026-01-01", "勋章未迁移"
        assert not legacy.is_file(), "legacy 应被备份重命名"
        # 修改保存 → 重载一致
        st.intimacy = 50.0
        save_state(st)
        st2 = load_state()
        assert st2.intimacy == 50.0, "保存后重载不一致"
        # DB 真有数据
        kb = BlobStore(db_path=db)
        assert kb.get(kb.KEY_GROWTH)["intimacy"] == 50.0
    finally:
        _restore(saved)
    print("OK test_growth_json_migrates_to_db_and_roundtrip")


def test_style_profile_json_migrates_to_db():
    tmp = Path(tempfile.mkdtemp())
    legacy = tmp / "style_profile.json"
    legacy.write_text(
        json.dumps(
            {
                "profile": {"summary": "简净克制", "features": {"rhythm": "短句多"}},
                "summary": "简净克制",
                "features": {"rhythm": "短句多"},
                "metrics": {"n": 3, "chars": 1200},
                "updates": 2,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    saved, db = _hermetic(tmp)
    try:
        from deskmate.style_profile_store import load_profile, save_profile

        data = load_profile(Path(legacy))  # 默认 → 临时库，迁移
        assert data.get("summary") == "简净克制", "风格画像未迁移"
        assert not legacy.is_file(), "legacy 应被备份重命名"
        # 改写保存 → 重载一致
        data["updates"] = 9
        save_profile(Path(legacy), data)
        data2 = load_profile(Path(legacy))
        assert data2["updates"] == 9, "保存后重载不一致"
        kb = BlobStore(db_path=db)
        assert kb.get(kb.KEY_STYLE)["updates"] == 9
    finally:
        _restore(saved)
    print("OK test_style_profile_json_migrates_to_db")


def test_decision_profile_json_migrates_to_db():
    tmp = Path(tempfile.mkdtemp())
    legacy = tmp / "decision_profile.json"
    legacy.write_text(
        json.dumps(
            {
                "risk": {"保守/稳健": {"count": 2, "last_ts": 1.0, "evidence": "求稳"}},
                "total_signals": 2,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    saved, db = _hermetic(tmp)
    try:
        from deskmate.alter_ego.decision_profile import (
            extract_and_update,
            load_profile,
        )

        prof = load_profile()  # 默认 → 临时库，迁移
        assert prof.total_signals == 2, "决策画像未迁移"
        assert not legacy.is_file(), "legacy 应被备份重命名"
        # 增量抽取 → 落库
        prof2, sigs = extract_and_update("先调研再决定，别太激进")
        assert prof2.total_signals >= 3, "增量抽取未落库"
        prof3 = load_profile()
        assert prof3.total_signals >= 3, "抽取后重载不一致"
        kb = BlobStore(db_path=db)
        assert kb.get(kb.KEY_DECISION)["total_signals"] >= 3
    finally:
        _restore(saved)
    print("OK test_decision_profile_json_migrates_to_db")


def test_diary_old_db_merged_into_unified():
    """旧 diary.db 整体并入侵统一库 deskmate.db，旧库备份重命名。"""
    tmp = Path(tempfile.mkdtemp())
    old = tmp / "diary.db"
    src = sqlite3.connect(str(old))
    src.executescript(
        """
        CREATE TABLE user_posts(
            id TEXT PRIMARY KEY, author TEXT, content TEXT, created_at TEXT,
            loved INTEGER DEFAULT 0, archived INTEGER DEFAULT 0);
        CREATE TABLE comments(
            id TEXT PRIMARY KEY, moment_id TEXT, author TEXT, text TEXT, created_at TEXT);
        CREATE TABLE loved_entries(
            id INTEGER PRIMARY KEY AUTOINCREMENT, moment_id TEXT, content TEXT,
            day TEXT, created_at TEXT, tags TEXT DEFAULT '#moment #diary');
        """
    )
    src.execute(
        "INSERT INTO user_posts VALUES('p1','me','legacy diary',?,0,0)",
        ("2026-08-01T00:00:00+08:00",),
    )
    src.execute(
        "INSERT INTO loved_entries(moment_id,content,day,created_at,tags) "
        "VALUES('p1','萧盛的日记：legacy','2026-08-01','2026-08-01','#moment #diary')"
    )
    src.commit()  # 默认 isolation 下需显式提交，否则 close() 回滚
    src.close()

    from deskmate.growth.diary_store import DiaryStore

    store = DiaryStore(base_dir=tmp)  # 触发并入
    assert (tmp / "deskmate.db").is_file(), "统一库未建立"
    assert not old.is_file(), "旧库应被备份重命名"

    # 日记内容迁入
    posts = store.get_posts(limit=10)["items"]
    assert any(p["content"] == "legacy diary" for p in posts), "日记未并入"
    # 🩷 沉淀迁入
    conn = sqlite3.connect(str(tmp / "deskmate.db"))
    n = conn.execute("SELECT COUNT(*) FROM loved_entries").fetchone()[0]
    assert n >= 1, "🩷 沉淀未并入"
    print("OK test_diary_old_db_merged_into_unified")


if __name__ == "__main__":
    test_growth_json_migrates_to_db_and_roundtrip()
    test_style_profile_json_migrates_to_db()
    test_decision_profile_json_migrates_to_db()
    test_diary_old_db_merged_into_unified()
    print("ALL PROFILE-TO-DB TESTS PASSED")
