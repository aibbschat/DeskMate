"""记忆召回纪律回归（2026-11-10 补：按书隔离 / 来源分层 / 冲突显式 / 陈旧降级）。

对应现场：记忆乱窜不只是「写进去没分层」，还有「召回时不分书、不分来源、不报冲突」。
本测试锁住四类行为：

1. 书维度：当前书专属记忆只进当前书会话；通用事实（偏好/习惯）永远保留；
   认不出当前书时保持旧行为（不误杀）。
2. 来源分层：local_intake（本地笔记）与存量外部新闻（source=live 但带信源标）
   只进参考层，不得冒充「你跟我说过的」。
3. 冲突显式：历史遗留的同锚点相反判断同时 active 时，注入里必须有冲突提示。
4. 陈旧/重复：progress 超过 TTL 不再注入；同一句换说法的近重复只留一条。

所有用例只用 tmp_path，不碰真实库。
"""
from __future__ import annotations

import sqlite3

from deskmate.memory import learned_store as ls
from deskmate.memory import user_fact_context as uf
from deskmate.memory.learned_store import LearnedStore


def _make_projects(base, *books):
    for book in books:
        (base / "projects" / book).mkdir(parents=True, exist_ok=True)
    return base


def _insert_raw(store, *, ts, fact, source, entity="", polarity="assert", status="active"):
    """直接插行，模拟历史遗留数据（绕过写入侧取代逻辑）。"""
    con = sqlite3.connect(store.db_path)
    try:
        con.execute(
            "INSERT INTO learned_history"
            "(ts, fact, category, fp, source, entity, polarity, status) "
            "VALUES (?, ?, 'other', ?, ?, ?, ?, ?)",
            (ts, fact, ls._fingerprint(fact), source, entity, polarity, status),
        )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 1. 书维度隔离
# ---------------------------------------------------------------------------
def test_book_filter_keeps_current_book_and_general_facts(tmp_path):
    _make_projects(tmp_path, "夜不收", "国子监秘闻录")
    store = LearnedStore(base_dir=tmp_path)
    store.append(["萧盛把《夜不收》第十一章的粮仓方向改成了往东"], source="progress:夜不收")
    store.append(["萧盛设定《国子监秘闻录》党争只写单线不写互攻"],
                 source="setting:国子监秘闻录")
    store.append(["萧哥偏好短句、少形容词、多对话动作"], source="live")

    ctx = uf.build_user_fact_context(tmp_path, query="《夜不收》第十一章粮仓怎么改 偏好短句")
    assert "粮仓方向" in ctx
    assert "党争只写单线" not in ctx, "书 A 专属记忆进了书 B 会话"
    assert "偏好短句" in ctx, "通用偏好被书过滤误杀"


def test_unknown_book_in_query_demotes_known_other_book(tmp_path):
    """query 提到一本已知书单之外的书（如已完结的《衙探》）时，别的书的专属记忆不得灌进来。"""
    _make_projects(tmp_path, "夜不收")
    store = LearnedStore(base_dir=tmp_path)
    store.append(["萧盛确认《夜不收》粮仓改往东"], source="live")
    store.append(["萧哥喜欢在访谈里聊写作习惯"], source="live")

    ctx = uf.build_user_fact_context(tmp_path, query="《衙探》访谈稿怎么聊萧盛")
    assert "粮仓改往东" not in ctx
    assert "访谈" in ctx


def test_no_book_context_prefers_general_over_book_specific(tmp_path):
    """认不出当前书时，书专属条目只做补充，不得挤掉通用偏好/习惯。"""
    _make_projects(tmp_path, "夜不收")
    store = LearnedStore(base_dir=tmp_path)
    for i in range(12):
        store.append([f"萧盛关于《夜不收》的第{i}条剧情裁定"], source="author_ruling:夜不收")
    store.append(["萧哥偏好短句、少形容词"], source="live")

    ctx = uf.build_user_fact_context(tmp_path, query="")
    assert "偏好短句" in ctx
    # 通用事实排在书专属条目之前，且书专属最多占 limit-1 条
    first_book = ctx.find("剧情裁定")
    assert first_book == -1 or ctx.index("偏好短句") < first_book
    assert ctx.count("剧情裁定") <= 9


def test_no_book_signal_keeps_legacy_behaviour(tmp_path):
    """认不出当前书时不误杀：老行为（不按书过滤）保留。"""
    store = LearnedStore(base_dir=tmp_path)
    store.append(["萧盛确认《夜不收》粮仓改往东"], source="live")
    store.append(["萧盛设定《国子监秘闻录》党争单线"], source="live")
    ctx = uf.build_user_fact_context(tmp_path, query="")
    assert "粮仓改往东" in ctx
    assert "党争单线" in ctx


def test_book_from_recent_session_when_query_has_no_title(tmp_path):
    """多轮对话里后续轮次不再带书名——从最近 session 文件认出当前书。"""
    _make_projects(tmp_path, "夜不收", "国子监秘闻录")
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "1000.md").write_text(
        "## User\n《国子监秘闻录》第六卷的党争怎么写\n", encoding="utf-8"
    )
    store = LearnedStore(base_dir=tmp_path)
    store.append(["萧盛设定《国子监秘闻录》党争单线"], source="setting:国子监秘闻录")
    store.append(["萧盛把《夜不收》粮仓方向改成往东"], source="progress:夜不收")

    ctx = uf.build_user_fact_context(tmp_path, query="接着把这一节党争展开说说")
    assert "党争单线" in ctx
    assert "粮仓方向" not in ctx


# ---------------------------------------------------------------------------
# 2. 来源分层（存量兜底）
# ---------------------------------------------------------------------------
def test_local_intake_notes_go_to_reference_layer(tmp_path):
    store = LearnedStore(base_dir=tmp_path)
    store.append(["[本地笔记] 宋口语资料：直恁（真的，真是）"],
                 source="local_intake:/Users/x/13.衙探/宋口语.txt")
    ctx = uf.build_user_fact_context(tmp_path, query="宋口语 直恁")
    primary = ctx.split("## 参考层")[0]
    assert "宋口语" not in primary, "本地笔记被当成萧哥口述事实"
    assert "宋口语" in ctx
    assert "参考层" in ctx


def test_legacy_live_news_demoted_by_content_marker(tmp_path):
    store = LearnedStore(base_dir=tmp_path)
    store.append(["【天下要闻】早啊！新闻来了：某地发生地震"], source="live")
    store.append(["萧哥今天想聊聊怎么写好开头"], source="live")
    ctx = uf.build_user_fact_context(tmp_path, query="新闻 开头")
    primary = ctx.split("## 参考层")[0] if "## 参考层" in ctx else ctx
    assert "地震" not in primary
    assert "地震" in ctx  # 降进参考层，不当事实但可作线索
    assert "写好开头" in primary


def test_effective_source_normalizes_legacy_rows():
    assert ls.effective_source(None, "（外部·news）某新闻") == ls.SOURCE_EXTERNAL
    assert ls.effective_source("live", "【财经脉动】财联社电") == ls.SOURCE_EXTERNAL
    assert ls.effective_source("live", "萧哥偏好短句") == ls.SOURCE_USER
    assert ls.effective_source("legacy", "存量旧事实") == ls.SOURCE_LEGACY
    assert ls.effective_source("manuscript:夜不收", "正文") == "manuscript:夜不收"


def test_reclassify_legacy_is_dry_run_by_default(tmp_path):
    store = LearnedStore(base_dir=tmp_path)
    store.append(["【天下要闻】某外部新闻"], source="live")
    store.append(["萧哥偏好短句"], source="live")

    report = store.reclassify_legacy()
    assert report["candidates"] == 1
    assert report["updated"] == 0
    # 真跑一次
    report2 = store.reclassify_legacy(dry_run=False)
    assert report2["updated"] == 1
    rows = store.list_for_ui(limit=10)
    sources = {r["fact"]: r["source"] for r in rows}
    assert sources["【天下要闻】某外部新闻"] == ls.SOURCE_EXTERNAL
    assert sources["萧哥偏好短句"] == ls.SOURCE_USER


# ---------------------------------------------------------------------------
# 3. 冲突显式提示
# ---------------------------------------------------------------------------
def test_conflict_note_when_opposite_polarity_share_entity(tmp_path):
    store = LearnedStore(base_dir=tmp_path)
    _insert_raw(
        store, ts="2026-10-01T10:00:00", fact="《宋会要辑稿》在史料库",
        source="legacy", entity="书:宋会要辑稿", polarity="assert",
    )
    _insert_raw(
        store, ts="2026-10-02T10:00:00", fact="《宋会要辑稿》不在史料库",
        source="legacy", entity="书:宋会要辑稿", polarity="deny",
    )
    ctx = uf.build_user_fact_context(tmp_path, query="宋会要辑稿")
    assert "冲突提示" in ctx
    assert "回正文" in ctx


def test_no_false_conflict_for_unrelated_same_book_facts(tmp_path):
    """只靠「同 entity=书:X」不算冲突——同一本书下互不相关的条目不能互相报警。"""
    store = LearnedStore(base_dir=tmp_path)
    _insert_raw(
        store, ts="2026-10-01T10:00:00", fact="《夜不收》陈冲带两桶火药",
        source="legacy", entity="书:夜不收", polarity="assert",
    )
    _insert_raw(
        store, ts="2026-10-02T10:00:00", fact="《夜不收》龙山仓未烧成",
        source="legacy", entity="书:夜不收", polarity="deny",
    )
    ctx = uf.build_user_fact_context(tmp_path, query="夜不收 火药")
    assert "冲突提示" not in ctx


# ---------------------------------------------------------------------------
# 4. 陈旧 / 近重复
# ---------------------------------------------------------------------------
def test_old_progress_entries_not_injected(tmp_path):
    store = LearnedStore(base_dir=tmp_path)
    store.append(["萧盛完成第十章定稿"], source="progress:夜不收", ts="2020-01-01T10:00:00")
    ctx = uf.build_user_fact_context(tmp_path, query="第十章")
    assert "完成第十章定稿" not in ctx


def test_near_duplicate_phrasing_collapsed(tmp_path):
    store = LearnedStore(base_dir=tmp_path)
    store.append(["2026-10-11: 萧盛决定不写《投名状》这一卷。"], source="live")
    store.append(["2026-10-11: 萧盛决定不写《投名状》这一卷/篇。"], source="live")
    ctx = uf.build_user_fact_context(tmp_path, query="投名状")
    assert ctx.count("决定不写") == 1


def test_manager_extraction_attaches_book_to_source(tmp_path, monkeypatch):
    """抽取落库前就把书维度拼进 source，否则召回侧永远分不清是哪本书的记忆。"""
    import deskmate.memory.manager as manager_mod

    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    session_file = sessions / "123.md"
    session_file.write_text(
        "## User\n《夜不收》第十一章龙山仓怎么写出压迫感\n\n## Assistant\n聊聊\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(manager_mod, "memory_llm_available", lambda: True)
    monkeypatch.setattr(
        manager_mod, "chat_memory",
        lambda *a, **k: '[{"category":"work","text":"2026-11-10: 萧盛设定龙山仓三面丘岗",'
                        '"source":"setting"}]',
    )
    mgr = manager_mod.DeskMateMemoryManager(base_dir=tmp_path)
    out = mgr._extract_single_session(session_file)
    assert out["learned_sources"] == ["setting:夜不收"]


def test_local_intake_path_contributes_known_book(tmp_path):
    """known_books 要能从 local_intake 路径里认出「16.夜不收」这类书目录。"""
    store = LearnedStore(base_dir=tmp_path)
    store.append(["[本地笔记] 夜不收资料：龙山仓三面丘岗"],
                 source="local_intake:/Users/x/Documents/已出版/16.夜不收/资料.txt")
    assert "夜不收" in uf._known_books(tmp_path)


def test_multi_version_note_when_same_anchor_has_old_and_new(tmp_path):
    """同锚点旧计划 + 新决定都在 active 时，必须给「不同时期」弱提示（lead 实测量例）。"""
    store = LearnedStore(base_dir=tmp_path)
    _insert_raw(store, ts="2026-10-04T10:00:00",
                fact="2026-09-24: 萧盛计划写第六章《投名状》",
                source="legacy", entity="书:投名状", polarity="assert")
    _insert_raw(store, ts="2026-10-12T10:00:00",
                fact="2026-10-11: 萧盛决定不写《投名状》这一卷/篇。",
                source="legacy", entity="书:投名状", polarity="deny")
    ctx = uf.build_user_fact_context(tmp_path, query="投名状 还写不写")
    assert "不同时期的记忆" in ctx
    # 弱提示块内：旧条在前、新决定在后（最后一条是较新的说法）
    notice = ctx.split("## 同一主题")[1]
    assert notice.index("计划写第六章") < notice.index("决定不写")


def test_reference_layer_truncates_long_local_notes(tmp_path):
    store = LearnedStore(base_dir=tmp_path)
    store.append(["[本地笔记] 投名状章纲：" + "长" * 800], source="local_intake:/x/20.国子监秘闻录/a.md")
    ctx = uf.build_user_fact_context(tmp_path, query="投名状 章纲")
    ref = ctx.split("## 参考层")[1]
    assert "（节选）" in ref
    assert len(ref) < 600


def test_diary_template_lines_filtered_and_deduped(tmp_path):
    import datetime as _dt

    diary = tmp_path / "diary"
    diary.mkdir()
    for offset in range(3):
        day = (_dt.date.today() - _dt.timedelta(days=offset)).isoformat()
        (diary / f"{day}.md").write_text(
            f"# {day} 周二 · 小桔的日记\n"
            "- 今天我留意到世界上的一些新动静，记下来：\n"
            "- 写稿时喜欢先跑一遍通读再动笔。\n",
            encoding="utf-8",
        )
    ctx = uf.build_user_fact_context(tmp_path, query="通读 动笔 新动静")
    assert "今天我留意到" not in ctx
    assert ctx.count("先跑一遍通读再动笔") == 1


def test_with_book_only_for_bookish_sources():
    assert ls.with_book("setting", "萧盛设定《夜不收》龙山仓三面丘岗") == "setting:夜不收"
    assert ls.with_book("progress", "萧盛完成《夜不收》第十章定稿") == "progress:夜不收"
    # 通用偏好不拼书维度：否则会被书过滤误杀
    assert ls.with_book("live", "萧哥出版过《明朝的裂变》") == "live"
    # 已带后缀不覆盖
    assert ls.with_book("setting:夜不收", "别的书") == "setting:夜不收"

def test_bare_book_name_in_query_filters(tmp_path):
    """日常提问常不带书名号（「夜不收 粮仓 怎么改」），书维度也必须生效。"""
    _make_projects(tmp_path, "夜不收", "国子监秘闻录")
    store = LearnedStore(base_dir=tmp_path)
    store.append(["《夜不收》第十一章粮仓方向改为往东"], source="progress:夜不收")
    store.append(["《国子监秘闻录》党争只写单线不写互攻"], source="setting:国子监秘闻录")
    ctx = uf.build_user_fact_context(tmp_path, query="夜不收 粮仓 怎么改")
    assert "粮仓方向" in ctx
    assert "党争只写单线" not in ctx

def test_quoted_extra_titles_do_not_break_book_attribution(tmp_path):
    """正文夹带引号专名（“张运才/张运财”）时也要能判出书；他书进度不得进本书上下文。"""
    _make_projects(tmp_path, "夜不收", "大清钱王")
    store = LearnedStore(base_dir=tmp_path)
    store.append(
        ['萧盛读完《大清钱王》第二部，发现“张运才/张运财”混用，待核定'],
        source="live",
    )
    store.append(["萧盛把《夜不收》粮仓方向改成往东"], source="progress:夜不收")
    ctx = uf.build_user_fact_context(tmp_path, query="夜不收 粮仓 怎么改")
    assert "粮仓方向" in ctx
    assert "张运才" not in ctx
