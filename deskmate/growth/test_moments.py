"""MomentFeed / 小桔日记 关键测试：🩷 落记忆 是否真正进 SQLite 并被聊天检索。

存储已从 moments.json 单文件 + 全局 MEMORY.md 改为 SQLite（diary.db）+ 导出视图
（diary_memory.md，§ 分节），解决了「落盘了却检索不到」与「数据膨胀」两类问题。

覆盖：
- love() 后落 SQLite（loved_entries）且导出 diary_memory.md 检索视图（不再写 MEMORY.md）
- 同一内容重复 love → 去重跳过（不重复污染记忆）
- 发日记 / 留言 / 快照往返正常（重启后可恢复）
- 核心回归：🩷 落记忆后，聊天主链路的 prefetch / build_full_context_for_turn 必须能检索到
  （修复原 moments 写 ## 自由格式、而 _parse_sections 只认 § 分节 导致永远检索不到的断点）
- 迁移：moments.json + MEMORY.md 的 ##/§ 日记沉淀 块 → diary.db（幂等、备份、只删沉淀块）
- 分页：get_posts 游标翻页无丢失/重复、时间倒序
"""

import json
import tempfile
from pathlib import Path

from deskmate.growth.diary_store import DiaryStore
from deskmate.growth.moments import MomentsStore


def _store():
    d = Path(tempfile.mkdtemp())
    return MomentsStore(base_dir=d), d


def test_love_writes_sqlite_and_view():
    store, base = _store()
    r = store.love("m1", "今天终于把第三章改完了")
    assert r["memorized"] is True
    # 落 SQLite
    assert (base / "diary.db").is_file()
    conn = __import__("sqlite3").connect(str(base / "diary.db"))
    rows = conn.execute("SELECT content FROM loved_entries").fetchall()
    assert len(rows) == 1 and "第三章" in rows[0][0]
    # 导出检索视图（§ 分节）
    view = (base / "diary_memory.md").read_text(encoding="utf-8")
    assert "日记沉淀" in view and "第三章" in view
    # 不再写全局 MEMORY.md
    assert not (base / "MEMORY.md").exists()


def test_love_dedup_no_repeat():
    store, base = _store()
    store.love("m1", "今天读了《兰陵王》")
    r2 = store.love("m2", "今天读了《兰陵王》")  # 不同 id、同内容
    assert r2["memorized"] is False
    conn = __import__("sqlite3").connect(str(base / "diary.db"))
    n = conn.execute("SELECT COUNT(*) FROM loved_entries").fetchone()[0]
    assert n == 1, "重复沉淀未去重"


def test_love_searchable_in_chat_memory():
    """回归测试：🩷 落记忆后，聊天主链路的 prefetch 必须能检索到（修复断点）。"""
    store, base = _store()
    store.love("m1", "今天终于把第三章改完了，心情很好")
    from deskmate.memory.manager import DeskMateMemoryManager

    mgr = DeskMateMemoryManager(base_dir=base)
    ctx = mgr.prefetch("第三章改完了")
    assert "日记沉淀" in ctx, "prefetch 未命中 🩷 沉淀块"
    assert "第三章" in ctx, "prefetch 未命中沉淀内容关键词"
    full = mgr.build_full_context_for_turn("第三章改完了")
    assert "日记沉淀" in full and "第三章" in full


def test_add_post_and_reply_never_empty():
    store, _ = _store()
    p = store.add_user_post("今天很累")
    assert p["content"] == "今天很累"
    r = store.generate_reply("今天很累")
    assert r and r.strip()


def test_snapshot_roundtrip_persists():
    store, base = _store()
    p = store.add_user_post("hello world")
    pid = p["id"]  # SQLite 下留言/🩷 必须挂在真实 post id 上
    store.add_comment(pid, "me", "hi")
    store.love(pid, "hello world")
    s2 = MomentsStore(base_dir=base).snapshot()
    assert len(s2["userPosts"]) == 1
    assert s2["userPosts"][0]["content"] == "hello world"
    assert len(s2["comments"][pid]) == 1
    assert pid in s2["loved"]


def test_migrate_from_legacy_json_and_memory(tmp_path):
    """moments.json + MEMORY.md(##/§ 两种沉淀块) → diary.db，幂等、备份、只删沉淀块。"""
    # 1) 合成 legacy moments.json
    legacy = {
        "userPosts": [
            {"id": "p1", "author": "me", "content": "legacy one", "createdAt": "2026-08-01T10:00:00+08:00"},
            {"id": "p2", "author": "xiaoju", "content": "legacy two", "createdAt": "2026-08-02T10:00:00+08:00"},
        ],
        "comments": {"p1": [{"id": "c1", "author": "xiaoju", "text": "加油", "createdAt": "2026-08-01T11:00:00+08:00"}]},
        "loved": ["p1"],
    }
    (tmp_path / "moments.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    # 2) 合成 MEMORY.md：## 旧格式 + § 新格式 沉淀块 + 无关章节（含国子监，必须保留）
    mem = (
        "§ 工作 #work #sensitive\n萧盛在写《国子监秘闻录》。\n\n"
        "## 日记沉淀\n- 🩷 萧盛的日记（2026-07-10）：旧格式一\n- 🩷 萧盛的日记（2026-07-11）：旧格式二\n\n"
        "§ 日记沉淀 #moment #diary\n2026-07-12：🩷 萧盛的日记：新格式三\n2026-07-13：🩷 萧盛的日记：新格式四\n\n"
        "§ 偏好\n萧盛偏好简洁中文。\n"
    )
    (tmp_path / "MEMORY.md").write_text(mem, encoding="utf-8")

    store = DiaryStore(base_dir=tmp_path)
    res = store.migrate_from_legacy(backup=True)
    assert res["migrated_posts"] == 2
    assert res["migrated_loved"] == 4, "两种格式沉淀块都应被提取"
    assert res["mem_cleaned"] is True
    assert res["backups"], "应生成备份"

    # 验证 DB
    conn = __import__("sqlite3").connect(str(tmp_path / "diary.db"))
    posts = conn.execute("SELECT id, loved FROM user_posts").fetchall()
    loved = conn.execute("SELECT content FROM loved_entries").fetchall()
    assert len(posts) == 2 and len(loved) == 4
    assert any(r[1] == 1 for r in posts), "loved 标记未迁移"
    # 验证 MEMORY.md 仅删沉淀块、保留其它
    cleaned = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "日记沉淀" not in cleaned
    assert "国子监秘闻录" in cleaned and "偏好" in cleaned
    # 验证视图
    view = (tmp_path / "diary_memory.md").read_text(encoding="utf-8")
    assert "新格式三" in view and "旧格式一" in view

    # 幂等：第二次跳过
    res2 = store.migrate_from_legacy(backup=True)
    assert res2.get("skipped") is True


def test_pagination_no_loss_no_dup(tmp_path):
    """get_posts 游标翻页：取完所有条、无丢失、无重复、时间倒序。"""
    store = DiaryStore(base_dir=tmp_path)
    ids, cts = [], []
    for i in range(7):
        p = store.add_user_post(f"post {i}")
        ids.append(p["id"])
        cts.append(p["createdAt"])

    seen = []
    cursor = None
    pages = 0
    while True:
        page = store.get_posts(limit=3, cursor=cursor)
        for it in page["items"]:
            seen.append(it["id"])
        pages += 1
        cursor = page["next_cursor"]
        assert pages < 50, "分页死循环"
        if cursor is None:
            break
    assert sorted(seen) == sorted(ids), "分页丢失/重复"
    seen_cts = [cts[ids.index(i)] for i in seen]
    assert seen_cts == sorted(seen_cts, reverse=True), "非时间倒序"
    assert seen[0] == ids[-1], "最新未排最前"
