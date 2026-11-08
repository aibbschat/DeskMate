"""2026-11-08 来源闸门回归：别让其他地方的讨论当作圣经铁律。

萧哥两条拍板：
1. **粗纲／大纲降级为「只进索引」** —— 它们本就只是参考，若还按旧章纲／旧
   结局去想，脑洞就停在既有层面；降级后点名补读仍放行（不削真 Agent）。
2. **清库 + 来源过滤** —— 只有书稿正文逐字直引且核验通过的条目才是事实；
   圆桌／简报／讨论／大纲／推断，哪怕被误标成 tier=manuscript + confirmed，
   也不许升格成设定或情节铁律。

现场坏档同型：22 条「圆桌综合研判报告」来源，其中 10 条被伪装成
tier=manuscript / status=confirmed / confidence=1.0，evidence 却是
「孔大贵之死已是情感峰值」这类讨论判断语——单看 tier 拦不住，故双闸门。
"""
from __future__ import annotations

from pathlib import Path

from deskmate.bots import experience, materials, reading
from deskmate.novel_memory import entities as ent


# ---------------------------------------------------------------- 1. 规划类降级
def _shelf(root: Path) -> reading.BookShelf:
    book = root / "16.夜不收"
    book.mkdir(parents=True, exist_ok=True)
    (book / "夜不收 粗纲.txt").write_text("粗纲标记：四人按死序全死", encoding="utf-8")
    (book / "夜不收大纲.txt").write_text("大纲标记：第十六章无人还", encoding="utf-8")
    (book / "脑洞汇总.md").write_text("脑洞标记：旧规划汇总", encoding="utf-8")
    (book / "夜不收：绝命救援.txt").write_text(
        "正文标记：三人跳上船，直冲江心", encoding="utf-8"
    )
    (book / "《夜不收》授权协议.txt").write_text("协议标记", encoding="utf-8")
    return reading.BookShelf(root, max_chars=8000)


def test_classify_rules_are_shared() -> None:
    """分类规则单一事实源：materials 与 BookShelf.read 必须同源，不许漂移。"""
    assert materials.classify is reading.classify
    assert reading.classify("夜不收：绝命救援.docx") == 0
    assert reading.classify("夜不收 粗纲.docx") == 1
    assert reading.classify("夜不收大纲.docx") == 1
    assert reading.classify("脑洞汇总.md") == 1
    assert reading.classify("《夜不收》授权协议.txt") == 2


def test_reference_docs_not_in_material_body(tmp_path: Path) -> None:
    """群共享 body 只装已写正文；粗纲/大纲/脑洞/协议只进索引。"""
    pack = materials.RoomMaterials(
        _shelf(tmp_path), style_loader=lambda: {}
    ).prepare("r1", "先读《夜不收》")
    assert pack is not None
    assert "正文标记" in pack.body, "正文 docx 一份不减，仍全量原文"
    for mark in ("粗纲标记", "大纲标记", "脑洞标记", "协议标记"):
        assert mark not in pack.body, f"{mark} 不应进群共享正文"
    assert "夜不收 粗纲.txt" in pack.index
    assert "夜不收大纲.txt" in pack.index
    assert "不要拿它们当既定框架" in pack.prompt_block()


def test_read_book_empty_skips_reference_but_named_can_read(tmp_path: Path) -> None:
    """留空补读也不吃旧规划；点名补读主权必须保留（不削真 Agent）。"""
    shelf = _shelf(tmp_path)
    empty = shelf.read("《夜不收》")
    assert "正文标记" in empty
    assert "粗纲标记" not in empty and "大纲标记" not in empty
    assert "粗纲标记" in shelf.read("《夜不收》", chapter="粗纲")


# ---------------------------------------------------------------- 2. 来源闸门
def _rec(
    name: str,
    *,
    tier: str = ent.TIER_MANUSCRIPT,
    status: str = ent.STATUS_CONFIRMED,
    source_file: str = "夜不收：绝命救援.docx",
    evidence: str = "三人跳上船，直冲江心",
) -> ent.Record:
    return ent.Record(
        kind=ent.KIND_CHARACTER, name=name, status=status, confidence=1.0,
        tier=tier, evidence=evidence, source_file=source_file,
    )


def test_source_guard_blocks_roundtable_and_forged_manuscript(tmp_path: Path) -> None:
    """tier 主闸 + source_file 兜底，两条都要能拦住「讨论变铁律」。"""
    conn = ent.connect(tmp_path / ".deskmate")
    ent.upsert_records(conn, [
        _rec("正文人物"),
        _rec("圆桌人物", tier=ent.TIER_ROUNDTABLE, source_file="圆桌综合研判报告"),
        _rec("伪装人物", source_file="圆桌综合研判报告"),   # 坏档同型：伪造 manuscript
        _rec("简报推断", source_file="进度简报-2026-11-06.md"),
    ])
    by_name = {
        r["name"]: r for r in conn.execute("select * from entities")
    }
    assert ent.is_canon_fact(by_name["正文人物"]) is True
    assert ent.is_canon_fact(by_name["圆桌人物"]) is False, "tier 主闸"
    assert ent.is_canon_fact(by_name["伪装人物"]) is False, "来源名兜底"
    assert ent.is_canon_fact(by_name["简报推断"]) is False, "简报判断不是正文事实"

    docs = ent.render_bible(conn)
    blob = "\n".join(docs.values())
    assert "正文人物" in docs["CHARACTERS"]
    for name in ("圆桌人物", "伪装人物", "简报推断"):
        assert name not in blob, f"{name} 不得渲染进任何一份圣经"
    assert "未收录（非书稿正文来源" in docs["NOTES"]
    assert "另有 3 条" in docs["NOTES"]
    # 既有契约：banner 单行、第二行仍是标题（test_deskmate_bible_entities 锁了）
    assert docs["CHARACTERS"].splitlines()[1].strip() == "## CHARACTERS"
    assert "来源纪律" in docs["CHARACTERS"].splitlines()[0]
    conn.close()


def test_empty_source_file_kept_as_manuscript(tmp_path: Path) -> None:
    """早期正文抽取数据 source_file 为空但带逐字引文，不能被白名单误杀。"""
    conn = ent.connect(tmp_path / ".deskmate")
    ent.upsert_records(conn, [_rec("早期正文人物", source_file="")])
    row = conn.execute("select * from entities").fetchone()
    assert ent.is_canon_fact(row) is True
    assert "早期正文人物" in ent.render_bible(conn)["CHARACTERS"]
    conn.close()


def test_demote_discussion_confirmations_only_demotes() -> None:
    """回流复核：讨论里编的引文降 pending，正文真有的保 confirmed，只降不放。"""
    real = "三人跳上船，船只如箭一般直冲江心，孔大贵用命给他们争取时间。"
    a = _rec("讨论编的", evidence="孔大贵之死已是情感峰值")
    b = _rec("正文真有", evidence="三人跳上船，船只如箭一般直冲江心")
    assert ent.demote_discussion_confirmations([a, b], real) == 1
    assert a.status == ent.STATUS_PENDING and a.needs_review is True
    assert b.status == ent.STATUS_CONFIRMED
    # 正文暂读不到时不动作（与 P1-5 同思路，避免误杀）
    c = _rec("暂不可核", evidence="讨论里编的话")
    assert ent.demote_discussion_confirmations([c], "") == 0
    assert c.status == ent.STATUS_CONFIRMED


# ------------------------------------------------------- 3. 作者裁定独立成文
def test_author_ruling_is_first_in_experience(tmp_path: Path) -> None:
    """作者裁定效力最高、独立成文，必须排在经验层第一节且进补读索引。"""
    d = tmp_path / "16.夜不收" / ".deskmate"
    d.mkdir(parents=True)
    (d / "AUTHOR_RULING.md").write_text(
        "# 作者裁定\n三人一个也不能死，让他们活着回。", encoding="utf-8"
    )
    (d / "CHARACTERS.md").write_text("孔大贵：正文事实", encoding="utf-8")
    (d / "进度简报-2026-11-06.md").write_text("今日进度", encoding="utf-8")
    block = experience.collect_experience(
        tmp_path / "16.夜不收", style_loader=lambda: {}
    )
    assert block.has_any
    assert block.text.startswith("## 作者裁定"), "作者裁定必须是经验层第一节"
    assert "三人一个也不能死" in block.text
    assert any("AUTHOR_RULING" in x for x in block.index_lines)
