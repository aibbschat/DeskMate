"""结构化人物圣经（v2）的回归测试：证据核验闸门 + 确定性 upsert + 渲染。

本文件的头号用例直接复刻 2026-08-07 的真实事故：圆桌讨论《国子监秘闻录》时，
各人格一本正经地讨论"李慕白与沈清音的禁书之约"——而这两个人名在正文里 0 命中，
是抽取 LLM 借正文中真实存在的"学正""子时"两个词脑补出来的整条情节线。
v1 没有任何环节能拦住它，还会让它随每次增量更新越滚越牢。

这些测试全部不调用 LLM（核验与合并都是确定性逻辑），可以放心当回归护栏。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deskmate.novel_memory import entities as ent
from deskmate.novel_memory.core import (
    _loads_lenient,
    _records_from_json,
    _batch_source_file,
    read_bible,
    bible_stats,
)


# 一段"正文"：里面有真实人物「周砚」「陆先生」，也有真实词「学正」「子时」，
# 但绝对没有「李慕白」「沈清音」「禁书之约」。
REAL_TEXT = """
第一章 夜巡

新来的学正抬手按住书页，眉头微皱。周砚站在阶下，不敢抬头。
陆先生自廊下走来，低声道：子时之后，藏书阁不得入内。
周砚应了一声，心里却记挂着白日里那桩公案。
"""


def _rec(**kw) -> ent.Record:
    kw.setdefault("kind", ent.KIND_CHARACTER)
    return ent.Record(**kw)


# ---------------------------------------------------------------------------
# 核验闸门：幻觉必须被拦住
# ---------------------------------------------------------------------------


class TestVerificationGate:
    def test_rejects_hallucinated_characters(self):
        """真实事故回归：正文 0 命中的人名必须被判为幻觉。"""
        recs = [
            _rec(name="李慕白", detail="国子监学正", evidence="新来的学正抬手按住书页"),
            _rec(name="沈清音", detail="藏书阁女官", evidence="子时之后，藏书阁不得入内"),
        ]
        out = ent.verify_records(recs, source_text=REAL_TEXT, full_text=REAL_TEXT)
        assert all(r.status == ent.STATUS_REJECTED for r in out)
        # 拦截原因必须说清是"正文里没这个人"，而不是含糊其辞
        assert all("不存在该专名" in r.reject_reason for r in out)

    def test_rejects_hallucinated_relation(self):
        """「李慕白与沈清音的禁书之约」这条关系必须整条被砍。"""
        rec = ent.Record(
            kind=ent.KIND_RELATION, subject="李慕白", object="沈清音",
            rel_type="暧昧", detail="约定三日后再议禁书",
            evidence="新来的学正抬手按住书页",  # 引文是真的，但人是编的
        )
        out = ent.verify_records([rec], source_text=REAL_TEXT, full_text=REAL_TEXT)
        assert out[0].status == ent.STATUS_REJECTED

    def test_accepts_real_character_with_verbatim_evidence(self):
        rec = _rec(name="周砚", detail="监生", evidence="周砚站在阶下，不敢抬头")
        out = ent.verify_records([rec], source_text=REAL_TEXT, full_text=REAL_TEXT)
        assert out[0].status == ent.STATUS_CONFIRMED
        assert out[0].confidence == 1.0

    def test_punctuation_differences_still_confirmed(self):
        """引文只改了标点/空白 → 归一化后仍算逐字命中，不该误杀。"""
        rec = _rec(name="陆先生", detail="师长", evidence="陆先生自廊下走来低声道")
        out = ent.verify_records([rec], source_text=REAL_TEXT, full_text=REAL_TEXT)
        assert out[0].status == ent.STATUS_CONFIRMED

    def test_fabricated_evidence_rejected(self):
        """人是真的，但引文是编的 → 仍要拦下（防止真人身上挂假事实）。"""
        rec = _rec(
            name="周砚", detail="其实是刺客",
            evidence="周砚拔剑刺向学正，血溅五步之内当场毙命",
        )
        out = ent.verify_records([rec], source_text=REAL_TEXT, full_text=REAL_TEXT)
        assert out[0].status == ent.STATUS_REJECTED
        assert "找不到该引证" in out[0].reject_reason

    def test_missing_evidence_rejected(self):
        rec = _rec(name="周砚", detail="监生", evidence="")
        out = ent.verify_records([rec], source_text=REAL_TEXT, full_text=REAL_TEXT)
        assert out[0].status == ent.STATUS_REJECTED
        assert "引证" in out[0].reject_reason

    def test_character_appearing_elsewhere_in_book_passes_name_check(self):
        """专名核验看全书，引文核验看本批——两者分工不能混淆。"""
        other_chapter = "第二章\n\n沈砚秋推门而入，与周砚相视一笑。"
        rec = ent.Record(
            kind=ent.KIND_RELATION, subject="沈砚秋", object="周砚",
            rel_type="友", detail="相识",
            evidence="沈砚秋推门而入，与周砚相视一笑",
        )
        out = ent.verify_records(
            [rec], source_text=other_chapter, full_text=REAL_TEXT + other_chapter
        )
        assert out[0].status == ent.STATUS_CONFIRMED

    def test_settings_and_events_also_gated(self):
        good = ent.Record(
            kind=ent.KIND_SETTING, name="藏书阁",
            detail="夜间禁入", evidence="子时之后，藏书阁不得入内",
        )
        bad = ent.Record(
            kind=ent.KIND_EVENT, name="边境失守",
            detail="北境三城陷落", evidence="北境三城于同一夜相继陷落",
        )
        out = ent.verify_records([good, bad], source_text=REAL_TEXT, full_text=REAL_TEXT)
        assert out[0].status == ent.STATUS_CONFIRMED
        assert out[1].status == ent.STATUS_REJECTED


# ---------------------------------------------------------------------------
# 确定性 upsert：取代「LLM 整篇重写」
# ---------------------------------------------------------------------------


class TestDeterministicUpsert:
    @pytest.fixture()
    def conn(self, tmp_path: Path):
        c = ent.connect(tmp_path / ".deskmate")
        yield c
        c.close()

    def test_rejected_records_never_enter_db(self, conn):
        recs = ent.verify_records(
            [_rec(name="李慕白", evidence="新来的学正抬手按住书页")],
            source_text=REAL_TEXT, full_text=REAL_TEXT,
        )
        counts = ent.upsert_records(conn, recs)
        assert counts["rejected"] == 1
        assert ent.list_records(conn) == []

    def test_newer_chapter_overrides_older(self, conn):
        ent.upsert_records(conn, [_rec(
            name="周砚", detail="监生", evidence="周砚站在阶下，不敢抬头",
            source_seq=1, status=ent.STATUS_CONFIRMED,
        )])
        ent.upsert_records(conn, [_rec(
            name="周砚", detail="已升任学录", evidence="周砚站在阶下，不敢抬头",
            source_seq=9, status=ent.STATUS_CONFIRMED,
        )])
        rows = ent.list_records(conn, kind=ent.KIND_CHARACTER)
        assert len(rows) == 1
        assert rows[0]["detail"] == "已升任学录"

    def test_older_chapter_does_not_regress_newer(self, conn):
        ent.upsert_records(conn, [_rec(
            name="周砚", detail="已升任学录", evidence="x" * 10,
            source_seq=9, status=ent.STATUS_CONFIRMED,
        )])
        ent.upsert_records(conn, [_rec(
            name="周砚", detail="监生", evidence="y" * 10,
            source_seq=1, status=ent.STATUS_CONFIRMED,
        )])
        rows = ent.list_records(conn, kind=ent.KIND_CHARACTER)
        assert rows[0]["detail"] == "已升任学录"

    def test_relation_is_undirected(self, conn):
        """A→B 与 B→A 必须落到同一条，否则同一对人物会存出两份互相打架的关系。"""
        ent.upsert_records(conn, [ent.Record(
            kind=ent.KIND_RELATION, subject="周砚", object="陆先生",
            rel_type="上下级", evidence="x" * 10, source_seq=1,
            status=ent.STATUS_CONFIRMED,
        )])
        ent.upsert_records(conn, [ent.Record(
            kind=ent.KIND_RELATION, subject="陆先生", object="周砚",
            rel_type="敌", evidence="y" * 10, source_seq=5,
            status=ent.STATUS_CONFIRMED,
        )])
        rows = ent.list_records(conn, kind=ent.KIND_RELATION)
        assert len(rows) == 1
        assert rows[0]["rel_type"] == "敌"

    def test_relation_flip_is_recorded_in_history(self, conn):
        """关系翻转要留痕——「先敌后友」是情节，不是错误，不能被静默抹掉。"""
        ent.upsert_records(conn, [ent.Record(
            kind=ent.KIND_RELATION, subject="周砚", object="陆先生",
            rel_type="敌", evidence="x" * 10, source_seq=1,
            status=ent.STATUS_CONFIRMED,
        )])
        ent.upsert_records(conn, [ent.Record(
            kind=ent.KIND_RELATION, subject="周砚", object="陆先生",
            rel_type="友", evidence="y" * 10, source_seq=8,
            status=ent.STATUS_CONFIRMED,
        )])
        rows = ent.list_records(conn, kind=ent.KIND_RELATION)
        hist = json.loads(rows[0]["history"])
        assert any(h["from"] == "敌" and h["to"] == "友" for h in hist)

    def test_delete_single_record(self, conn):
        """v1 做不到的事：精确删掉一条错误事实，而不必重写整篇圣经。"""
        r = _rec(name="周砚", evidence="x" * 10, status=ent.STATUS_CONFIRMED)
        ent.upsert_records(conn, [r])
        assert ent.delete_record(conn, r.id) is True
        assert ent.list_records(conn) == []

    def test_revoke_unsupported_shrinks_bible(self, conn):
        """正文改稿后人物被写没了，圣经要跟着收缩——v1 只增不减。"""
        ent.upsert_records(conn, [
            _rec(name="周砚", evidence="x" * 10, status=ent.STATUS_CONFIRMED),
            _rec(name="某被删角色", evidence="y" * 10, status=ent.STATUS_CONFIRMED),
        ])
        revoked = ent.revoke_unsupported(conn, REAL_TEXT)
        assert revoked == [f"{ent.KIND_CHARACTER}:某被删角色"]
        names = [r["name"] for r in ent.list_records(conn)]
        assert names == ["周砚"]


# ---------------------------------------------------------------------------
# 渲染：LLM 无法在这一步塞进新东西
# ---------------------------------------------------------------------------


class TestRender:
    def test_render_excludes_rejected_and_includes_confirmed(self, tmp_path: Path):
        conn = ent.connect(tmp_path / ".deskmate")
        recs = ent.verify_records(
            [
                _rec(name="周砚", detail="监生", evidence="周砚站在阶下，不敢抬头"),
                _rec(name="李慕白", detail="学正", evidence="新来的学正抬手按住书页"),
            ],
            source_text=REAL_TEXT, full_text=REAL_TEXT,
        )
        ent.upsert_records(conn, recs)
        docs = ent.render_bible(conn)
        conn.close()

        blob = "\n".join(docs.values())
        assert "周砚" in blob
        assert "李慕白" not in blob          # 幻影不得出现在任何一份圣经里
        assert set(docs) == {"CHARACTERS", "OUTLINE", "TIMELINE", "NOTES"}
        assert docs["CHARACTERS"].splitlines()[1].strip() == "## CHARACTERS"

    def test_pending_marked_explicitly(self, tmp_path: Path):
        conn = ent.connect(tmp_path / ".deskmate")
        # 转述式引文：多数滑窗能命中，判 pending
        rec = _rec(
            name="周砚",
            detail="监生",
            evidence="周砚站在阶下不敢抬头看那位新来的学正大人",
        )
        out = ent.verify_records([rec], source_text=REAL_TEXT, full_text=REAL_TEXT)
        if out[0].status == ent.STATUS_PENDING:
            ent.upsert_records(conn, out)
            docs = ent.render_bible(conn)
            assert "待确认" in docs["CHARACTERS"] or "待确认" in docs["NOTES"]
        conn.close()


# ---------------------------------------------------------------------------
# JSON 解析容错（模型输出千奇百怪，但绝不能靠"猜"来补数据）
# ---------------------------------------------------------------------------


class TestLenientJson:
    def test_plain_json(self):
        assert _loads_lenient('{"characters": []}') == {"characters": []}

    def test_fenced_json(self):
        raw = '好的，结果如下：\n```json\n{"characters": [{"name": "周砚"}]}\n```\n'
        assert _loads_lenient(raw)["characters"][0]["name"] == "周砚"

    def test_prefixed_chatter(self):
        raw = '这是抽取结果：{"settings": [{"name": "藏书阁"}]}'
        assert _loads_lenient(raw)["settings"][0]["name"] == "藏书阁"

    def test_garbage_returns_empty_not_guess(self):
        """解析不了就返回空——宁可这批没产出，也不许猜出数据。"""
        assert _loads_lenient("模型今天不想干活") == {}
        assert _loads_lenient("") == {}


class TestRecordsFromJson:
    def test_maps_all_kinds(self):
        data = {
            "characters": [{"name": "周砚", "aliases": ["砚哥"], "detail": "监生",
                            "evidence": "周砚站在阶下"}],
            "relations": [{"subject": "周砚", "object": "陆先生", "type": "上下级",
                           "detail": "受教", "evidence": "陆先生自廊下走来"}],
            "events": [{"title": "夜巡", "detail": "子时禁入", "evidence": "子时之后"}],
            "settings": [{"name": "藏书阁", "detail": "夜间禁入", "evidence": "藏书阁不得入内"}],
        }
        recs = _records_from_json(data, seq=3, source_file="ch1.txt")
        kinds = sorted(r.kind for r in recs)
        assert kinds == ["character", "event", "relation", "setting"]
        assert all(r.source_seq == 3 and r.source_file == "ch1.txt" for r in recs)

    def test_skips_self_relation_and_nameless(self):
        data = {
            "characters": [{"name": "", "evidence": "x"}],
            "relations": [{"subject": "周砚", "object": "周砚", "evidence": "x"}],
        }
        assert _records_from_json(data, seq=1, source_file="") == []

    def test_batch_source_file_extracted(self):
        batch = "# 文件：卷一/第三章.txt\n\n正文正文"
        assert _batch_source_file(batch) == "卷一/第三章.txt"


# ---------------------------------------------------------------------------
# read_bible 的 v2 优先 / v1 回退
# ---------------------------------------------------------------------------


class TestReadBibleCompat:
    def test_prefers_entities_db_over_legacy_markdown(self, tmp_path: Path):
        bible_dir = tmp_path / ".deskmate"
        bible_dir.mkdir()
        # 旧散文圣经里躺着幻影人物（正是本次事故的现场状态）
        (bible_dir / "CHARACTERS.md").write_text(
            "## CHARACTERS\n- 李慕白 & 沈清音：禁书之约", encoding="utf-8"
        )
        conn = ent.connect(bible_dir)
        ent.upsert_records(conn, [_rec(
            name="周砚", detail="监生", evidence="周砚站在阶下，不敢抬头",
            status=ent.STATUS_CONFIRMED, source_seq=1,
        )])
        conn.close()

        docs = read_bible(str(tmp_path))
        assert "周砚" in docs["CHARACTERS"]
        assert "李慕白" not in docs["CHARACTERS"]   # 结构化数据接管，旧污染不再外泄
        assert bible_stats(str(tmp_path))["version"] == "v2"

    def test_falls_back_to_legacy_when_no_entities(self, tmp_path: Path):
        bible_dir = tmp_path / ".deskmate"
        bible_dir.mkdir()
        (bible_dir / "CHARACTERS.md").write_text("## CHARACTERS\n- 周砚", encoding="utf-8")
        docs = read_bible(str(tmp_path))
        assert "周砚" in docs["CHARACTERS"]
        assert bible_stats(str(tmp_path))["version"] == "v1"

    def test_no_bible_at_all(self, tmp_path: Path):
        assert read_bible(str(tmp_path)) == {}
        assert bible_stats(str(tmp_path))["version"] == "none"


# ---------------------------------------------------------------------------
# 端到端：抽取 LLM 就算继续产生幻觉，也进不了最终圣经
# ---------------------------------------------------------------------------


class TestBuildBibleEndToEnd:
    """把抽取模型换成"一定会说谎"的假模型，验证整条链路的防线。

    这是最贴近真实事故的一层测试：v1 在同样的模型输出下会把幻影写进圣经，
    v2 必须做到最终产物里一个字都找不到。
    """

    @pytest.fixture()
    def novel(self, tmp_path: Path) -> Path:
        (tmp_path / "第一章.txt").write_text(REAL_TEXT, encoding="utf-8")
        return tmp_path

    def test_hallucination_never_reaches_bible(self, novel: Path, monkeypatch):
        liar_output = json.dumps({
            "characters": [
                {"name": "周砚", "aliases": [], "detail": "监生",
                 "evidence": "周砚站在阶下，不敢抬头"},
                # 以下两位纯属虚构，正文 0 命中
                {"name": "李慕白", "aliases": [], "detail": "国子监学正",
                 "evidence": "新来的学正抬手按住书页"},
                {"name": "沈清音", "aliases": [], "detail": "藏书阁女官",
                 "evidence": "子时之后，藏书阁不得入内"},
            ],
            "relations": [
                {"subject": "李慕白", "object": "沈清音", "type": "暧昧",
                 "detail": "禁书之约，三日后再会",
                 "evidence": "新来的学正抬手按住书页"},
            ],
            "events": [
                {"title": "夜巡禁令", "detail": "子时后藏书阁不得入内",
                 "evidence": "子时之后，藏书阁不得入内"},
            ],
            "settings": [
                {"name": "藏书阁", "detail": "夜间禁入",
                 "evidence": "子时之后，藏书阁不得入内"},
            ],
        }, ensure_ascii=False)

        import deskmate.novel_memory.core as core

        monkeypatch.setattr(core, "_chat", lambda *a, **k: liar_output)
        docs = core.build_bible(str(novel))

        blob = "\n".join(docs.values())
        assert "李慕白" not in blob
        assert "沈清音" not in blob
        assert "禁书之约" not in blob
        # 真实内容照常保留，不能因为收紧而把好东西也砍光
        assert "周砚" in blob
        assert "藏书阁" in blob

        st = core.bible_stats(str(novel))
        assert st["version"] == "v2"

    def test_incremental_keeps_old_facts_and_gates_new(self, novel: Path, monkeypatch):
        """增量更新既不能丢掉旧的真事实，也不能放进新的假事实。"""
        import deskmate.novel_memory.core as core

        first = json.dumps({
            "characters": [{"name": "周砚", "aliases": [], "detail": "监生",
                            "evidence": "周砚站在阶下，不敢抬头"}],
            "relations": [], "events": [], "settings": [],
        }, ensure_ascii=False)
        monkeypatch.setattr(core, "_chat", lambda *a, **k: first)
        core.build_bible(str(novel))

        # 写入新章节：真人物「沈砚秋」+ 假人物「李慕白」
        new_chapter = "第二章\n\n沈砚秋推门而入，与周砚相视一笑。"
        (novel / "第二章.txt").write_text(new_chapter, encoding="utf-8")

        second = json.dumps({
            "characters": [
                {"name": "沈砚秋", "aliases": [], "detail": "新任书吏",
                 "evidence": "沈砚秋推门而入，与周砚相视一笑"},
                {"name": "李慕白", "aliases": [], "detail": "学正",
                 "evidence": "沈砚秋推门而入，与周砚相视一笑"},
            ],
            "relations": [], "events": [], "settings": [],
        }, ensure_ascii=False)
        monkeypatch.setattr(core, "_chat", lambda *a, **k: second)
        docs = core.incremental_bible_update(str(novel))

        blob = "\n".join(docs.values())
        assert "周砚" in blob        # 旧事实没被增量冲掉
        assert "沈砚秋" in blob      # 新的真事实进来了
        assert "李慕白" not in blob  # 新的假事实被拦住
