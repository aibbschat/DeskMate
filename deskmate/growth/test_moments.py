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

import pytest

from deskmate.growth.diary_store import DiaryStore
from deskmate.growth.moments import MomentsStore


def _store():
    d = Path(tempfile.mkdtemp())
    return MomentsStore(base_dir=d), d


def test_love_writes_sqlite_and_view():
    store, base = _store()
    r = store.love("m1", "今天终于把第三章改完了")
    assert r["memorized"] is True
    # 落 SQLite（统一库 deskmate.db）
    assert (base / "deskmate.db").is_file()
    conn = __import__("sqlite3").connect(str(base / "deskmate.db"))
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
    conn = __import__("sqlite3").connect(str(base / "deskmate.db"))
    n = conn.execute("SELECT COUNT(*) FROM loved_entries").fetchone()[0]
    assert n == 1, "重复沉淀未去重"


def test_love_searchable_in_chat_memory():
    """回归测试：🩷 落记忆后，聊天主链路的 prefetch 必须能检索到（修复断点）。"""
    store, base = _store()
    store.love("m1", "今天终于把第三章改完了，心情很好")
    from deskmate.memory.manager import DeskMateMemoryManager

    try:
        mgr = DeskMateMemoryManager(base_dir=base)
    except PermissionError:
        # 沙箱内 VaultStore.mkdir 被路径 broker 拦截（EEXIST）；真实环境可正常初始化
        pytest.skip("sandbox blocks VaultStore mkdir")
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


def test_migrate_from_legacy_json_and_memory():
    """moments.json + MEMORY.md(##/§ 两种沉淀块) → diary.db，幂等、备份、只删沉淀块。"""
    d = Path(tempfile.mkdtemp())
    # 1) 合成 legacy moments.json
    legacy = {
        "userPosts": [
            {"id": "p1", "author": "me", "content": "legacy one", "createdAt": "2026-08-01T10:00:00+08:00"},
            {"id": "p2", "author": "xiaoju", "content": "legacy two", "createdAt": "2026-08-02T10:00:00+08:00"},
        ],
        "comments": {"p1": [{"id": "c1", "author": "xiaoju", "text": "加油", "createdAt": "2026-08-01T11:00:00+08:00"}]},
        "loved": ["p1"],
    }
    (d / "moments.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    # 2) 合成 MEMORY.md：## 旧格式 + § 新格式 沉淀块 + 无关章节（含国子监，必须保留）
    mem = (
        "§ 工作 #work #sensitive\n萧盛在写《国子监秘闻录》。\n\n"
        "## 日记沉淀\n- 🩷 萧盛的日记（2026-07-10）：旧格式一\n- 🩷 萧盛的日记（2026-07-11）：旧格式二\n\n"
        "§ 日记沉淀 #moment #diary\n2026-07-12：🩷 萧盛的日记：新格式三\n2026-07-13：🩷 萧盛的日记：新格式四\n\n"
        "§ 偏好\n萧盛偏好简洁中文。\n"
    )
    (d / "MEMORY.md").write_text(mem, encoding="utf-8")

    store = DiaryStore(base_dir=d)
    res = store.migrate_from_legacy(backup=True)
    assert res["migrated_posts"] == 2
    assert res["migrated_loved"] == 4, "两种格式沉淀块都应被提取"
    assert res["mem_cleaned"] is True
    assert res["backups"], "应生成备份"

    # 验证 DB（统一库 deskmate.db）
    conn = __import__("sqlite3").connect(str(d / "deskmate.db"))
    posts = conn.execute("SELECT id, loved FROM user_posts").fetchall()
    loved = conn.execute("SELECT content FROM loved_entries").fetchall()
    assert len(posts) == 2 and len(loved) == 4
    assert any(r[1] == 1 for r in posts), "loved 标记未迁移"
    # 验证 MEMORY.md 仅删沉淀块、保留其它
    cleaned = (d / "MEMORY.md").read_text(encoding="utf-8")
    assert "日记沉淀" not in cleaned
    assert "国子监秘闻录" in cleaned and "偏好" in cleaned
    # 验证视图
    view = (d / "diary_memory.md").read_text(encoding="utf-8")
    assert "新格式三" in view and "旧格式一" in view

    # 幂等：第二次跳过
    res2 = store.migrate_from_legacy(backup=True)
    assert res2.get("skipped") is True


def test_pagination_no_loss_no_dup():
    """get_posts 游标翻页：取完所有条、无丢失、无重复、时间倒序。"""
    store = DiaryStore(base_dir=Path(tempfile.mkdtemp()))
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


def test_archive_independent_of_love():
    """每条日记可独立归档；归档仅改 archived 标记，不影响🩷记忆检索维度。"""
    store, base = _store()
    p = store.add_user_post("要归档的日记")
    pid = p["id"]
    # 先🩷沉淀
    store.love(pid, "要归档的日记")
    # 归档
    assert store.archive(pid, archived=True) is True
    item = store.get_posts(limit=10)["items"][0]
    assert item["archived"] is True
    assert item["loved"] is True, "归档不应影响🩷记忆标记"
    # 🩷 与 归档 是两个独立维度：归档后记忆检索仍能命中
    from deskmate.memory.manager import DeskMateMemoryManager

    try:
        mgr = DeskMateMemoryManager(base_dir=base)
    except PermissionError:
        pytest.skip("sandbox blocks VaultStore mkdir")
    assert "要归档的日记" in mgr.prefetch("要归档的日记"), "归档后🩷记忆仍应可检索"
    # 取消归档
    assert store.archive(pid, archived=False) is True
    assert store.get_posts(limit=10)["items"][0]["archived"] is False
    # 不存在的 id 返回 False
    assert store.archive("nope", True) is False


def test_unlove_removes_recall_and_view():
    """A1 反向开关：unlove 从 loved_entries 移除、user_posts.loved=0、视图不再含该条。"""
    store, base = _store()
    # 必须先建 user_posts 行（love/unlove 走 UPDATE，不会 INSERT）
    p = store.add_user_post("今天把第三章改完了")
    mid = p["id"]
    store.love(mid, "今天把第三章改完了")
    conn = __import__("sqlite3").connect(str(base / "deskmate.db"))
    assert conn.execute("SELECT COUNT(*) FROM loved_entries").fetchone()[0] == 1
    r = store.unlove(mid)
    assert r["loved"] is False
    assert conn.execute("SELECT COUNT(*) FROM loved_entries").fetchone()[0] == 0
    assert conn.execute("SELECT loved FROM user_posts WHERE id=?", (mid,)).fetchone()[0] == 0
    # unlove 后视图文件应被清空（无条目即删除文件）
    view = base / "diary_memory.md"
    assert "第三章" not in (view.read_text(encoding="utf-8") if view.exists() else "")


def test_love_unlove_roundtrip_loved_flag():
    """add_user_post 默认 loved=0；love→1；unlove→0；再 love→1（A1 toggle 数据模型）。"""
    store, base = _store()
    p = store.add_user_post("今天读了《兰陵王》")
    mid = p["id"]
    conn = __import__("sqlite3").connect(str(base / "deskmate.db"))
    assert conn.execute("SELECT loved FROM user_posts WHERE id=?", (mid,)).fetchone()[0] == 0
    store.love(mid, "今天读了《兰陵王》")
    assert conn.execute("SELECT loved FROM user_posts WHERE id=?", (mid,)).fetchone()[0] == 1
    store.unlove(mid)
    assert conn.execute("SELECT loved FROM user_posts WHERE id=?", (mid,)).fetchone()[0] == 0
    store.love(mid, "今天读了《兰陵王》")
    assert conn.execute("SELECT loved FROM user_posts WHERE id=?", (mid,)).fetchone()[0] == 1


def test_post_autolove_default_on():
    """模拟 app 端点序列（add_user_post + store.love）→ 默认即进『聊天主动回忆』（A1）。"""
    store, base = _store()
    post = store.add_user_post("今天终于把第四章改完了")
    store.love(post["id"], "今天终于把第四章改完了")
    post["loved"] = True
    conn = __import__("sqlite3").connect(str(base / "deskmate.db"))
    assert conn.execute("SELECT loved FROM user_posts WHERE id=?", (post["id"],)).fetchone()[0] == 1
    view = (base / "diary_memory.md").read_text(encoding="utf-8")
    assert "第四章" in view


def test_reply_persisted_as_xiaoju_comment_on_post():
    """回归（留言消失 bug）：发日记时小桔回复必须落库为 comments(author=xiaoju)，
    否则重载日记页从 DB 读空、回复消失（无论规则还是 LLM 留言都不留痕）。

    模拟 app 端点序列：add_user_post -> generate_reply -> add_comment(xiaoju)。
    用固定回复避免真实 LLM 网络依赖。
    """
    import deskmate.growth.moments as M

    orig = M.MomentsStore.generate_reply
    M.MomentsStore.generate_reply = lambda self, content: "我陪着你呢。"  # type: ignore[assignment]
    try:
        store, base = _store()
        post = store.add_user_post("今天有点累")
        reply = store.generate_reply(post["content"])
        rc = store.add_comment(post["id"], "xiaoju", reply)
    finally:
        M.MomentsStore.generate_reply = orig
    assert rc["author"] == "xiaoju"
    # get_posts 必须读回该留言（重载不丢）
    posts = store.get_posts()
    item = next(p for p in posts["items"] if p["id"] == post["id"])
    assert any(c["author"] == "xiaoju" and c["text"] == reply for c in item["comments"])


def test_xiaoju_auto_reply_on_user_comment():
    """回归（双向闭环）：用户留言(author=me)后，小桔自动回一条并落库，
    get_posts 读回时 me + xiaoju 两条留言都在。"""
    import deskmate.growth.moments as M

    orig = M.MomentsStore.generate_reply
    M.MomentsStore.generate_reply = lambda self, content: "我在听。"  # type: ignore[assignment]
    try:
        store, base = _store()
        post = store.add_user_post("我的日记")
        store.add_comment(post["id"], "me", "今天天气真好")
        rtext = store.generate_reply("今天天气真好")
        store.add_comment(post["id"], "xiaoju", rtext)
    finally:
        M.MomentsStore.generate_reply = orig
    posts = store.get_posts()
    item = next(p for p in posts["items"] if p["id"] == post["id"])
    authors = [c["author"] for c in item["comments"]]
    assert "me" in authors and "xiaoju" in authors



# ---------------------------------------------------------------------------
# 回归：日记正文必须保留段落换行
# ---------------------------------------------------------------------------

def test_add_user_post_preserves_paragraph_breaks():
    """用户敲的回车必须真的存进去。

    回归背景：落库前曾用 ``re.sub(r"\\s+", " ", content)`` —— ``\\s`` 含 ``\\n``，
    写的时候分了三段，存进去变成一整行，前端按 ``\\n`` 分行渲染时就只剩一段
    （2026-09-05 萧哥反馈「写日记不能分段」）。
    """
    store, _ = _store()
    text = "今天把第三章改完了。\n\n中间卡了很久，后来发现是开头没立住。\n明天再顺一遍。"
    post = store.add_user_post(text)

    assert "\n" in post["content"], "换行被吃掉了，段落会挤成一段"
    assert post["content"].split("\n") == [
        "今天把第三章改完了。",
        "",
        "中间卡了很久，后来发现是开头没立住。",
        "明天再顺一遍。",
    ]


def test_add_user_post_roundtrips_multiline():
    """多行日记落库再读回，内容逐字一致（持久化层不能二次压平）。"""
    store, _ = _store()
    text = "第一段。\n第二段。\n第三段。"
    post = store.add_user_post(text)

    items = store.get_posts()["items"]
    saved = next(p for p in items if p["id"] == post["id"])
    assert saved["content"] == text


def test_add_user_post_collapses_excess_blank_lines():
    """连续空行最多留一个，首尾空行去掉（防止一列空行刷屏）。"""
    store, _ = _store()
    post = store.add_user_post("\n\n第一段。\n\n\n\n第二段。\n\n\n")
    assert post["content"] == "第一段。\n\n第二段。"


def test_add_user_post_squeezes_inline_spaces_only():
    """行内多余空格仍然压平（清洗语义不变），但不影响换行。"""
    store, _ = _store()
    post = store.add_user_post("开头   挤了   很多空格\n\t制表符也压掉")
    assert post["content"] == "开头 挤了 很多空格\n制表符也压掉"


def test_add_user_post_handles_crlf():
    """Windows 换行 CRLF 归一为 LF，不留游离的 \\r。"""
    store, _ = _store()
    post = store.add_user_post("第一段。\r\n第二段。")
    assert "\r" not in post["content"]
    assert post["content"] == "第一段。\n第二段。"


def test_add_comment_preserves_line_breaks():
    """留言同样保留换行（小桔的回复也走这条落库路径）。"""
    store, _ = _store()
    post = store.add_user_post("我的日记")
    c = store.add_comment(post["id"], "xiaoju", "这句我记下了。\n下次你写到这里，我会接得上。")
    assert c["text"] == "这句我记下了。\n下次你写到这里，我会接得上。"


# ---------------------------------------------------------------------------
# 长文日记沉淀/回复不截断（2026-10-16 根治）
#
# 旧 _trim（_CONTENT_MAX=120）曾同时污染两处「她读日记」的链路：
# - love()：>120 字的日记只存前 120 字符+「…」进 loved_entries ⇒ 她「聊天主动回忆」
#   永远只看得到前半段（萧哥实测：她回复「你写到'也就是说…'就断在那儿了」，
#   那个省略号是代码加的，不是日记原文）；
# - generate_reply()：她刚看完日记回话，prompt 里也是 120 截断版 ⇒ 接不住后半段。
# 且 _trim 的 re.sub(r"\s+", " ") 把换行压成空格——add_user_post 早已修掉的
# 「日记禁压行」，沉淀链路又踩一遍。
# 用户原文完整保存在 user_posts（235/218 字符实测），失真的只是记忆副本。
# ---------------------------------------------------------------------------
_LONG_DIARY = (
    "如果我们的出版行业真的死了，那么我们还剩下什么呢？那就只剩下活着了，像动物一样活着。\n"
    "图书是什么？图书是我们唯一能够获取的精神食粮，这一点是网文无法比拟的。"
    "也就是说，图书行业通过最大可能的保证了作品的质量，传输给读者的是有营养的东西。\n"
    "所以，出版行业真的死了，那么我们精神上吸取营养的入口就没有了，剩下的就只是像动物一样活着。"
)  # 150+ 字符，超旧 _CONTENT_MAX=120；结尾关键词「剩下的就只是」


def _loved_content(base: Path) -> str:
    import sqlite3

    conn = sqlite3.connect(str(base / "deskmate.db"))
    try:
        return conn.execute("SELECT content FROM loved_entries").fetchone()[0]
    finally:
        conn.close()


def test_love_keeps_full_text_over_120_chars():
    """>120 字日记沉淀必须存全文——她记住的不能是前半段。"""
    store, base = _store()
    r = store.love("m-long", _LONG_DIARY)
    assert r["memorized"] is True
    content = _loved_content(base)
    assert "剩下的就只是像动物一样活着" in content, "loved_entries 仍是 120 截断版"
    assert not content.rstrip().endswith("…"), "不该有截断省略号"
    assert len(content) > 150


def test_love_preserves_paragraph_newlines():
    """沉淀副本保留段落换行（_trim 的 re.sub(r"\\s+"," ") 曾把它压成空格）。"""
    store, base = _store()
    store.love("m-nl", "第一段，出版的事。\n第二段，活着的事。")
    content = _loved_content(base)
    assert "\n" in content, "换行被压平"
    assert content.count("🩷 萧盛的日记：") == 1


def test_generate_reply_prompt_sees_full_text(monkeypatch):
    """她读完日记回话：喂给 LLM 的 prompt 必须含全文结尾，不是 120 截断。"""
    store, _ = _store()
    seen: dict = {}

    def fake_call_llm(system, user, deadline=None):
        seen["user"] = user
        return "我看到了，鼻子有点酸。"

    monkeypatch.setattr(store, "_call_llm", fake_call_llm)
    reply = store.generate_reply(_LONG_DIARY)
    assert reply == "我看到了，鼻子有点酸。"
    assert "剩下的就只是像动物一样活着" in seen["user"], "回复 prompt 被 120 截断"
    assert "\n" in seen["user"], "回复 prompt 把日记压成了单行"
