"""引文核验（2026-11-17 编造台词事故）：引号里的话必须逐字在正文里。

事故形态：人物专家读完 13.1 万字全书、交付小传 3983 字，判断大体站得住，却编了
一批正文根本没有的台词，还标了章号——"月落之前，国王必须死"（第四章）、
"韩国师不是要帮寡人复位"（第十五章）、"被火上的鸭子"之类。读起来像原文，
等于给作者凭空造了已写事实。

这组测试锁三件事：
1. 编的必须抓到（宁可疑标，不可放过）；
2. 真的不许冤（逐字在、只是标点不同 / 省略号拼接两段真话，都要放过）；
3. 核验只做机械比对，不改专家的话——只插标记，不删句、不改写。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import deskmate.consult as consult_mod
from deskmate.bots import citations
from deskmate.bots.reading import Chapter, Manuscript

#: 一本假书：四章，正文里**故意**有易混的句子
#:   1) "挟天子以令诸侯" 在第九章（不是第六章）
#:   2) "人肉护盾"（不是"肉盾"）
#:   3) "你杀朴智仁不是霸权吗"（没有逗号）
_SOURCE = (
    "第一章 碧蹄馆\n"
    "宁靖把手按在刀柄上，低声道：“都别动，前面有倭军。”\n"
    "那领头人摇头道：“倭军将大王擒了后便秘密藏匿起来了，目的是要挟天子以令诸侯，"
    "令我国内上下引起恐慌。”\n"
    "第二章 关帝庙\n"
    "韩孝民召号百姓一起渡江，他当然不是为了避免百姓惨遭倭军的屠杀，"
    "而是想利用他们作人肉护盾，只要渡江的人数足够多，便可以转移倭军的视线。\n"
    "第三章 温泉岭\n"
    "孔大贵抬起手摸了摸那部虬髯：“你这个人怎么说呢，本心是好的，"
    "可你在无形之中又形成了一种新的霸权，树立了新的敌人。”\n"
    "第四章 浅水湾\n"
    "“你杀朴智仁不是霸权吗？他的性命，还有他母亲的性命，你他娘的有什么资格剥夺？”\n"
)


def _manuscript() -> Manuscript:
    chapters: list[Chapter] = []
    import re

    hits = list(re.finditer(r"^第.章 .*$", _SOURCE, re.MULTILINE))
    for i, hit in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(_SOURCE)
        label = hit.group(0).split(" ")[0]
        chapters.append(Chapter(i, label, "", hit.start(), end))
    return Manuscript(
        title="假书",
        text=_SOURCE,
        chapters=tuple(chapters),
        sources=("假书.txt",),
        total=len(_SOURCE),
    )


class _Shelf:
    """只实现 manuscript() 的假书架（citations 只用这一个方法）。"""

    def __init__(self, book: str = "假书") -> None:
        self.book = book

    def manuscript(self, title: str) -> Manuscript | None:
        return _manuscript() if title == self.book else None


# --------------------------------------------------------------------------- #
# 抓到编造
# --------------------------------------------------------------------------- #
def test_fabricated_lines_are_flagged() -> None:
    """事故里那几句编的台词，一句都不许漏。"""
    text = (
        '他必须死在月落之前（第四章：“日落之后就是月升，月落之前，国王必须死”）。\n'
        '第十四章李昖醒悟：“韩国师不是要帮寡人复位，是要寡人的命。”\n'
        '他“故意让几万百姓出城，当了倭军的肉盾”。\n'
        '还有人说他“被架在火上的鸭子”，后来“汤武革命”也成了他的旗号。'
    )
    report = citations.check(text, _manuscript())
    missing = " ".join(c.inner for c in report.unverified)
    for fake in ("月落", "韩国师", "肉盾", "被架在火上的鸭子", "汤武革命"):
        assert fake in missing, f"漏抓编造引文：{fake}"


def test_inline_marker_is_inserted_after_the_quote() -> None:
    """长引文就地标注；标记贴在右引号后，专家的句子一个字不动。"""
    text = '他说：“这句话正文里根本没有出现过，纯属瞎编。”然后继续。'
    report = citations.check(text, _manuscript())
    assert "纯属瞎编" in report.annotated, "不许删句"
    assert "”" + citations.UNVERIFIED_MARK in report.annotated
    assert "然后继续。" in report.annotated


def test_short_quotes_only_go_to_the_summary() -> None:
    """四五个字的短引（“汤武革命”“明君之问”）不就地标——正文里标记太多会被淹掉，
    但清单里必须在案。"""
    text = '他说要“汤武革命”，也说“明君之问”。'
    report = citations.check(text, _manuscript())
    assert "汤武革命" in [c.inner for c in report.unverified]
    assert report.annotated == text, "短引不该就地标（正文里一个标记都不加）"
    body = citations.summary(report)
    assert "汤武革命" in body


# --------------------------------------------------------------------------- #
# 真的不许冤
# --------------------------------------------------------------------------- #
def test_verbatim_quote_with_punctuation_drift_passes() -> None:
    """只差标点（逗号有无、引号全半角）算逐字命中——比对不拿标点冤枉人。"""
    text = '孔大贵说：“你杀朴智仁，不是霸权吗？”'
    report = citations.check(text, _manuscript())
    assert len(report.unverified) == 0, [c.inner for c in report.unverified]
    assert report.annotated == text, "命中就不许动文本"


def test_ellipsis_joining_two_real_passages_is_not_flagged() -> None:
    """省略号拼两段真话是正常引用：每一段各自在正文里就行。"""
    text = '他承认“本心是好的……又形成了一种新的霸权”。'
    report = citations.check(text, _manuscript())
    assert report.unverified == []


def test_chapter_attribution_is_reported_when_wrong() -> None:
    """引文找到了但章号写错：不进「未见」，进「章号不符」。"""
    text = '第六章孔大贵点破：“挟天子以令诸侯。”'
    report = citations.check(text, _manuscript())
    assert report.unverified == []
    assert [c.inner for c in report.mismatched] == ["挟天子以令诸侯。"]
    assert report.mismatched[0].claimed == "第六章"
    assert report.mismatched[0].chapter == "第一章"


def test_chapter_claim_is_accepted_when_the_line_really_lives_there() -> None:
    """引文确实也在那个章里出现，就不许报「章号不符」（一句话可能跨章重提）。"""
    text = '第四章：“你杀朴智仁不是霸权吗？”'
    report = citations.check(text, _manuscript())
    assert report.mismatched == []


# --------------------------------------------------------------------------- #
# quote_check 工具（给专家自己先核）
# --------------------------------------------------------------------------- #
def test_quote_check_tool_reports_found_and_chapter() -> None:
    tool = citations.make_quote_check_tool(_Shelf(), "假书")
    out = tool(["你杀朴智仁不是霸权吗", "月落之前国王必须死"])
    assert out["checked"] == 2 and out["missing"] == 1
    rows = {r["quote"]: r for r in out["results"]}
    assert rows["你杀朴智仁不是霸权吗"]["found"] is True
    assert rows["你杀朴智仁不是霸权吗"]["chapter"] == "第四章"
    assert rows["月落之前国王必须死"]["found"] is False
    assert "不要加引号" in out["hint"]


def test_quote_check_tool_survives_unknown_book() -> None:
    tool = citations.make_quote_check_tool(_Shelf(), "查无此书")
    out = tool(["随便一句话"])
    assert "error" in out, "解析不到书稿要明说，不能假装核过了"


def test_quote_check_tool_strips_quote_marks() -> None:
    """模型常把整段带引号塞进来；工具自己剥，别让它因为引号被判未见。"""
    tool = citations.make_quote_check_tool(_Shelf(), "假书")
    out = tool(['“人肉护盾”'])
    assert out["results"][0]["found"] is True


# --------------------------------------------------------------------------- #
# 接进咨询链路
# --------------------------------------------------------------------------- #
def test_verify_citations_annotates_text_and_returns_stats() -> None:
    body = '他说：“月落之前，国王必须死。”'
    text, stats = consult_mod._verify_citations(body, _Shelf(), "假书")
    assert citations.UNVERIFIED_MARK in text
    assert stats["unverified"] == 1
    assert stats["total"] == 1
    assert "## 引文核验" in text


def test_verify_citations_leaves_clean_text_alone() -> None:
    body = '他说：“你杀朴智仁不是霸权吗？”'
    text, stats = consult_mod._verify_citations(body, _Shelf(), "假书")
    assert text == body, "全对的时候不许往正文里加东西"
    assert stats["unverified"] == 0


def test_verify_citations_never_raises_on_broken_shelf() -> None:
    class _Broken:
        def manuscript(self, title: str) -> Any:
            raise RuntimeError("书架炸了")

    text, stats = consult_mod._verify_citations("原文", _Broken(), "假书")
    assert text == "原文" and stats == {}


def test_prompt_teaches_quote_discipline() -> None:
    text = consult_mod._prompt("character", "写小传", "假书", 900)
    assert "quote_check" in text, "必须点名自检工具"
    assert "正文未见" in text
    assert "不要加引号" in text, "纪律要写清：核不到就去掉引号改成转述"


def test_result_message_warns_before_the_expert_text() -> None:
    """小桔要在读到正文**之前**就知道有编造——警告放最前面。"""
    run = {
        "id": "c-x",
        "book": "假书",
        "elapsed": 12.0,
        "results": [
            {
                "expert": "character",
                "name": "人物专家",
                "status": "ok",
                "text": "小传……",
                "citations": {"total": 9, "found": 6, "unverified": 3, "mismatched": 1},
            }
        ],
    }
    msg = consult_mod.result_message(run)
    assert "引文核验" in msg
    assert "3 处引文正文未见原句" in msg
    assert "1 处章号与正文不符" in msg
    assert msg.index("引文核验") < msg.index("小传……"), "警告必须在正文之前"


def test_audit_records_citation_numbers(tmp_path: Path) -> None:
    run = {"id": "c-a", "elapsed": 10.0, "model": "scripted"}
    path = consult_mod._write_audit(
        [
            {
                "expert": "character",
                "name": "人物专家",
                "status": "ok",
                "text": "小传",
                "citations": {"total": 9, "found": 6, "unverified": 3, "mismatched": 1},
            }
        ],
        "简报",
        "假书",
        run,
        tmp_path / "audit",
    )
    assert path is not None
    text = Path(path).read_text(encoding="utf-8")
    assert "引文核验：9 处引号，6 处逐字命中，3 处正文未见，1 处章号不符" in text


def test_expert_engine_exposes_quote_check(tmp_path: Path) -> None:
    """专家必须拿得到自检工具，否则事后标注是它唯一的老师。"""
    from tests.test_expert_consult import _ScriptedProvider, _shelf

    eng = consult_mod._build_expert_engine(
        "character", _ScriptedProvider(), "scripted", _shelf(tmp_path), "", "假书"
    )
    assert "quote_check" in set(eng.registry.names())


@pytest.mark.parametrize("bad", [None, ""])
def test_verify_citations_noops_without_book(bad: Any) -> None:
    text, stats = consult_mod._verify_citations("原文", _Shelf(), bad)
    assert text == "原文" and stats == {}


def test_marker_goes_outside_a_trailing_close_paren() -> None:
    """“……”（第六章点破：……）这种嵌套，标记要贴在括号外，别插出双层括号。"""
    text = '（第六章点破：“这句话正文根本没有”）'
    report = citations.check(text, _manuscript())
    assert "）" + citations.UNVERIFIED_MARK in report.annotated
    assert "（正文未见原句））" not in report.annotated


def test_quote_check_tool_accepts_book_override() -> None:
    """派活时没带书名（专家自己 list_books 找的）也要能核。"""
    tool = citations.make_quote_check_tool(_Shelf(), "")
    assert "error" in tool(["人肉护盾"])
    out = tool(["人肉护盾"], "假书")
    assert out["results"][0]["found"] is True


def test_check_is_idempotent_on_delivered_text() -> None:
    """核过的文本再核一遍，不该再长出新账（2026-11-17 实测雪球）。

    投递文本 = 原文（含就地标记）+ 文末清单。清单里列的就是「未见」的句子，
    如果 `scan` 把它们再当引文扫一遍，同一份交付物每过一道就多一批：
    实测 53 处 → 72 处、39 条 → 55 条。这条锁住「清单与已标引文不再复扫」。
    """
    text = '他写道：“这句话正文根本没有，纯属拼接。”又说：“人肉护盾”才是原文。'
    first = citations.check(text, _manuscript())
    delivered = first.annotated + "\n\n" + citations.summary(first)
    second = citations.check(delivered, _manuscript())
    third = citations.check(second.annotated + "\n\n" + citations.summary(second), _manuscript())

    assert first.total == 2  # 一处编的、一处真的
    assert len(first.unverified) == 1
    # 再核不会变大：编的那处已被标记跳过，清单里的引号也被 SUMMARY_HEADING 截断
    assert second.total <= first.total
    assert second.total == third.total
    assert len(second.unverified) == len(third.unverified)
