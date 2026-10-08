"""user_fact_context：让小桔「库里知道」的用户事实 + 日记软规律 真正回灌到上下文。

回归点（2026-10-08 萧哥报）：此前 learned_history 用户事实与日记都没接进理解/聊天，
导致「她知道但不说」。本测试验证：
- 外部世界观察（（外部· 标）被正确排除，不冒充用户事实；
- 用户口述事实（如 姑射图→国子监第三章）按 query 召回；
- 近期日记软规律（如 假期不写稿）按 query 召回；
- 无 query 时回退最近用户事实；
- fail-open：库缺失/异常返回 ""。
"""
from __future__ import annotations

import datetime
import tempfile
from pathlib import Path

from deskmate.memory import user_fact_context as uf
from deskmate.memory.learned_store import LearnedStore


def _make_base() -> Path:
    d = Path(tempfile.mkdtemp(prefix="ufctx_"))
    return d


def test_is_user_fact():
    assert uf._is_user_fact("（外部·news）某新闻") is False
    assert uf._is_user_fact("（外部·文化产业）风向：…") is False
    assert uf._is_user_fact("《姑射图》是《国子监秘闻录》的第三章") is True
    assert uf._is_user_fact("萧哥通常在节假日不写稿") is True
    assert uf._is_user_fact("") is False


def test_query_terms():
    terms = uf._query_terms("《姑射图》 国子监 怎么回事")
    # 书名/引号内专名直接成词
    assert "姑射图" in terms
    # 非书名的中文字也由 2-gram 兜底覆盖（如 国子/子监 命中「国子监秘闻录」）
    assert "国子" in terms
    assert any(len(t) == 2 for t in terms)


def test_recall_user_fact_excludes_external():
    base = _make_base()
    store = LearnedStore(base_dir=base)
    store.append([
        "《姑射图》是《国子监秘闻录》的第三章",
        "萧哥写历史小说，已出版《明朝的裂变》",
    ], source="live")
    store.append(["（外部·news）某行业新闻摘要"], source="live")

    # 按 query 召回，应拿到用户事实、绝不包含外部观察
    ctx = uf.build_user_fact_context(base, query="姑射图 国子监 是哪一章")
    assert "姑射图" in ctx
    assert "国子监秘闻录" in ctx
    assert "（外部" not in ctx
    assert "某行业新闻" not in ctx
    # 命中用户事实块头
    assert "你跟我说过的" in ctx


def test_recall_diary_soft_rule():
    base = _make_base()
    # 日记：今天日期的 md，含软规律
    today = datetime.date.today().isoformat()
    diary_dir = base / "diary"
    diary_dir.mkdir(parents=True, exist_ok=True)
    (diary_dir / f"{today}.md").write_text(
        "# 日记\n- 国庆假期不写就不写，陪家人。\n- 今天没有太多动静。\n",
        encoding="utf-8",
    )
    ctx = uf.build_user_fact_context(base, query="六天没动笔 是不是卡住 假期")
    assert "你近期的日常" in ctx
    assert "不写" in ctx or "假期" in ctx
    assert "国庆" in ctx


def test_recent_fallback_when_no_query():
    base = _make_base()
    store = LearnedStore(base_dir=base)
    store.append([
        "《姑射图》是《国子监秘闻录》的第三章",
        "萧哥写历史小说",
    ], source="live")
    ctx = uf.build_user_fact_context(base, query="")
    assert "你跟我说过的" in ctx
    assert "姑射图" in ctx


def test_fail_open_on_missing_db():
    # 不存在的 base_dir（没有 deskmate.db）→ 不抛、返回 ""
    ctx = uf.build_user_fact_context("/nonexistent/path/xyz", query="姑射图")
    assert ctx == ""


def test_diary_snippets_match_terms():
    text = "# 日记\n- 国庆假期不写就不写，陪家人。\n- 写稿进度正常推进。\n"
    snips = uf._diary_snippets(text, ["假期", "不写"])
    joined = " ".join(snips)
    assert "国庆" in joined
    assert "不写" in joined
    assert "进度正常" not in joined  # 不命中 term 的行不取
