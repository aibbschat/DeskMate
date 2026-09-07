"""观察日记行为（observation_diary）+ 理解层（understand_observations）测试。

验证「看世界 → 理解 → 落日记 → 回灌决策」闭环：
- 只取带「（外部·」溯源标的世界观察，不碰用户事实；
- 事实原文不动，理解层（LLM）把它消化成自然段落写进日记，降级则原文 bullet；
- 理解回灌 world_context（事实段 + 理解段分层），让 L2 决策有认知输入；
- 同一条观察重复 tick 去重（不写多遍）；
- 冷却节流：未到点不写；
- fail-open：记忆/日记/LLM 异常不抛。
"""

import json
from pathlib import Path

import pytest


def _seed_observations(base_dir, facts):
    """往 learned_history 写带「（外部·」标的世界观察（7 天过期）。"""
    from datetime import datetime, timedelta

    from deskmate.memory.learned_store import LearnedStore

    expiry = (datetime.now() + timedelta(days=7)).isoformat(timespec="seconds")
    LearnedStore(base_dir=base_dir).append(list(facts), valid_until=expiry)


# ----------------------------------------------------------------------
# Plan B 基础路径（understand=False 钉死 bullet，确定性）
# ----------------------------------------------------------------------
def test_writes_world_observations_to_diary(tmp_path):
    base = str(tmp_path)
    _seed_observations(base, [
        "（外部·文化产业）网文风向：番茄小说上线 AI 辅助写作工具",
        "（外部·文化产业）AI圈风向：Cursor 推出团队版订阅",
        "（外部·财经）市场动向：白酒板块今日集体下挫",
    ])
    from deskmate.memory.learned_store import LearnedStore
    LearnedStore(base_dir=base).append(["萧哥偏好用史料交叉比对"])

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    res = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": False})

    assert res["written"] == 3, res
    assert "萧哥偏好" not in "\n".join(res["observations"])

    diary_dir = Path(base) / "diary"
    files = list(diary_dir.glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "我留意到世界上的一些新动静" in text
    assert "番茄小说上线 AI 辅助写作工具" in text
    assert "Cursor 推出团队版订阅" in text
    assert "萧哥偏好用史料交叉比对" not in text


def test_dedup_across_ticks(tmp_path):
    base = str(tmp_path)
    _seed_observations(base, ["（外部·文化产业）网文风向：起点下架一批 AI 生成作品"])

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    r1 = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": False})
    assert r1["written"] == 1
    r2 = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": False})
    assert r2["written"] == 0
    assert r2["skipped"] == 1

    diary_dir = Path(base) / "diary"
    text = (list(diary_dir.glob("*.md"))[0]).read_text(encoding="utf-8")
    assert text.count("起点下架一批 AI 生成作品") == 1


def test_cooldown_skips_when_not_due(tmp_path):
    base = str(tmp_path)
    _seed_observations(base, ["（外部·文化产业）短剧风向：抖音微短剧备案新规"])

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    r0 = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": False})
    assert r0["written"] == 1

    res = run_observation_diary(base, {"cooldown_hours": 999, "max_observations": 6, "understand": False})
    assert res["written"] == 0
    assert res["due"] is False


def test_fail_open_on_memory_error(tmp_path, monkeypatch):
    base = str(tmp_path)

    import deskmate.agency.behaviors.observation_diary as mod
    import deskmate.memory.learned_store as ls

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ls.LearnedStore, "active_recent", _boom)

    res = mod.run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": False})
    assert res["written"] == 0  # fail-open


def test_no_external_fact_writes_nothing(tmp_path):
    base = str(tmp_path)
    from deskmate.memory.learned_store import LearnedStore
    LearnedStore(base_dir=base).append(["萧哥是宁波人", "写作营讲网文技巧"])

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    res = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": False})
    assert res["written"] == 0
    assert "没有新的世界观察" in res["summary"]


# ----------------------------------------------------------------------
# 理解层（understand=True，monkeypatch LLM 以确定性）
# ----------------------------------------------------------------------
def test_understanding_writes_natural_diary(tmp_path, monkeypatch):
    base = str(tmp_path)
    _seed_observations(base, ["（外部·财经）市场动向：白酒板块今日集体下挫"])

    import deskmate.agency.behaviors.understand_observations as uo

    para = "今天看到白酒板块跌了，让我想到你前阵说消费疲弱的那段。市场情绪还是紧，你写人物时那种体感也许能用上。"
    # 新观察日记调 ``decide_and_digest``（新主接口）；同时保留旧 monkeypatch
    # ``understand_observations`` 以确保旧薄包装仍工作（向后兼容）。
    monkeypatch.setattr(
        uo, "decide_and_digest",
        lambda facts, at="", temperature=0.7: [{"body": para, "fact_keys": ["k"]}],
    )
    monkeypatch.setattr(uo, "understand_observations",
                        lambda facts, at="", temperature=0.7: para)

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    res = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": True})
    assert res["written"] == 1
    assert res["understood"] is True

    text = (list((Path(base) / "diary").glob("*.md"))[0]).read_text(encoding="utf-8")
    assert para in text                        # 自然段落（小桔消化后的视角）写入
    assert "我留意到世界上的一些新动静" not in text  # 不走 bullet 模板
    # 注：日记呈现是理解层自然转述；事实原文仍原样保留在 learned_history
    # 与 world_context 事实段（见 test_understanding_saved_for_world_context），供溯源/决策。


def test_understanding_fallback_to_bullets(tmp_path, monkeypatch):
    base = str(tmp_path)
    _seed_observations(base, ["（外部·财经）市场动向：白酒板块今日集体下挫"])

    import deskmate.agency.behaviors.understand_observations as uo

    # 新主接口返回空 list → 走降级 bullet；旧薄包装同步置 None（双保险）。
    monkeypatch.setattr(uo, "decide_and_digest", lambda *a, **k: [])
    monkeypatch.setattr(uo, "understand_observations", lambda *a, **k: None)

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    res = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": True})
    assert res["understood"] is False
    text = (list((Path(base) / "diary").glob("*.md"))[0]).read_text(encoding="utf-8")
    assert "我留意到世界上的一些新动静" in text  # 降级 bullet


def test_understanding_saved_for_world_context(tmp_path, monkeypatch):
    base = str(tmp_path)
    _seed_observations(base, ["（外部·财经）市场动向：白酒板块今日集体下挫"])

    import deskmate.agency.behaviors.understand_observations as uo

    para = "今天看到白酒板块跌了，让我想到你前阵说消费疲弱。市场情绪还是紧。"
    monkeypatch.setattr(uo, "decide_and_digest",
                        lambda facts, at="", temperature=0.7: [{"body": para, "fact_keys": ["k1"]}])

    from deskmate.agency.behaviors.observation_diary import run_observation_diary
    from deskmate.memory.world_context import build_world_context

    run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": True})
    ctx = build_world_context(base)
    assert "她观察到的世界" in ctx        # 事实段仍在
    assert "白酒板块今日集体下挫" in ctx  # 事实原文原样保留（溯源/决策依据）
    assert "她对世界的理解" in ctx        # 理解段已回灌
    assert para in ctx                    # 理解内容进入决策上下文


def test_understanding_jsonl_dedup_keys(tmp_path, monkeypatch):
    """理解落盘时应带 fact_keys（去重 key），供 world_context 关联。"""
    base = str(tmp_path)
    _seed_observations(base, ["（外部·财经）市场动向：白酒板块今日集体下挫"])

    import deskmate.agency.behaviors.understand_observations as uo

    monkeypatch.setattr(
        uo, "decide_and_digest",
        lambda facts, at="", temperature=0.7: [
            {"body": "白酒跌了，想到你说的消费。", "fact_keys": ["kk"]},
        ],
    )

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": True})
    jsonl = Path(base) / "agency" / "observation_understanding.jsonl"
    assert jsonl.is_file()
    rec = json.loads(jsonl.read_text(encoding="utf-8").strip().splitlines()[0])
    assert rec["fact_keys"] and isinstance(rec["fact_keys"], list)
    # 2026-09-07 改造：段内容放在 ``body`` 字段（不是旧 ``understanding``）
    assert rec["body"]
    # 带 entry_seq 便于回查
    assert rec["entry_seq"]


def test_understand_observations_parses_domain_and_strips_prefix():
    from deskmate.agency.behaviors.understand_observations import _parse_fact

    clean, domain = _parse_fact("（外部·文化产业）网文风向：番茄上线 AI 工具")
    assert clean == "网文风向：番茄上线 AI 工具"
    assert domain == "文化产业"

    clean2, domain2 = _parse_fact("（外部·tech）AI 新模型发布")
    assert clean2 == "AI 新模型发布"
    assert domain2 == "科技"


def test_parse_fact_handles_suffix_marker():
    """回归：后缀式溯源标「……（外部·news）」旧实现会把 clean 解析成**空串**。

    旧逻辑按「第一个 ）」切，后缀式标记后面没有内容 → clean = ""，
    理解层等于拿到空事实去消化。
    """
    from deskmate.agency.behaviors.understand_observations import _parse_fact

    clean, domain = _parse_fact("伊朗革命卫队称打击了美航母（外部·news）")
    assert clean == "伊朗革命卫队称打击了美航母"
    assert domain == "要闻"

    # 前缀式仍保持原行为（不回归）
    clean2, domain2 = _parse_fact("（外部·文化产业）网文风向：番茄上线 AI 工具")
    assert clean2 == "网文风向：番茄上线 AI 工具"
    assert domain2 == "文化产业"


def test_observation_diary_picks_up_suffix_marked_observations(tmp_path):
    """回归：后缀式标记的世界观察也要能进观察日记（与 world_context 判定一致）。"""
    from deskmate.agency.behaviors.observation_diary import run_observation_diary
    from deskmate.memory.learned_store import LearnedStore

    base = str(tmp_path)
    LearnedStore(base_dir=base).append(["郑钦文晋级美网女单四强（外部·news）"])

    res = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6,
                                       "understand": False})
    assert res["written"] == 1
    text = (list((tmp_path / "diary").glob("*.md"))[0]).read_text(encoding="utf-8")
    assert "郑钦文晋级美网女单四强" in text


# ----------------------------------------------------------------------
# 2026-09-07 改造：让小桔自己选 + 独立成段 + 每日篇数不限
# ----------------------------------------------------------------------
def test_understand_split_paragraphs_by_blank_line():
    """段间空行 → 切出多段（自然中文段落也常这么写）。"""
    from deskmate.agency.behaviors.understand_observations import _split_paragraphs

    text = (
        "今天看到美伊在霍尔木兹海峡对峙持续升级，局势比想象的还紧。\n\n"
        "碳酸锂一周跌了 15%，看得出市场情绪在变凉，跟你说的消费疲弱那个判断\n"
        "对得上。\n\n"
        "GPT6 那条微博有意思，打码设备居然是中国制造——最前沿和最传统撞在一起了。"
    )
    segs = _split_paragraphs(text)
    assert len(segs) == 3
    assert segs[0].startswith("今天看到美伊")
    assert segs[1].startswith("碳酸锂一周跌了")
    assert segs[2].startswith("GPT6 那条微博")


def test_understand_split_collapses_single_segment_when_no_blank():
    """没有空行 → 切出 1 段（不强行切）。"""
    from deskmate.agency.behaviors.understand_observations import _split_paragraphs

    text = "今天看到一条新闻，让我想到一句话。"
    segs = _split_paragraphs(text)
    assert len(segs) == 1


def test_understand_filter_drops_baillout_and_short():
    """长度闸门：< 30 字 / 出现「素材不足」/ > 1500 字 → 丢弃。"""
    from deskmate.agency.behaviors.understand_observations import _filter_segments

    # 太短：丢
    assert _filter_segments(["嗯嗯"]) == []
    # 含弃权词：丢
    assert _filter_segments(["素材不足"]) == []
    # 长度够：留
    keep = _filter_segments(["今天看到一条国际新闻让我想到——你的小说里也写过类似的场面。"])
    assert len(keep) == 1
    # 太长：丢（防模型跑野）
    assert _filter_segments(["啊" * 3000]) == []


def test_decide_and_digest_returns_segments(tmp_path, monkeypatch):
    """新主接口：LLM 自选 → 多段 → list[dict] 返回。

    各段必须 >= ``_MIN_SEG_CHARS`` 才能过闸门——这里每段 >= 30 字。
    """
    import deskmate.agency.behaviors.understand_observations as uo

    seg1 = "今天看到美伊在霍尔木兹海峡对峙升级，让我想到边界感的脆弱——这个世界从来不是按画好的线走的。"
    seg2 = "碳酸锂一周跌了 15%，市场情绪在变凉，这跟历史上的某些周期很像，可以记下做后面参考。"
    seg3 = "深圳 3D 打印占了全球九成市场，最前沿和最传统撞在一起，挺有意思的一件事。"

    # ``chat_memory`` 是函数内 import，必须 patch 源头模块。
    monkeypatch.setattr(
        "deskmate.memory.llm.chat_memory",
        lambda *a, **k: f"{seg1}\n\n{seg2}\n\n{seg3}",
    )
    monkeypatch.setattr("deskmate.memory.llm.memory_llm_available", lambda: True)

    facts = [
        {"fact": "（外部·要闻）美伊对峙"},
        {"fact": "（外部·财经）碳酸锂跌"},
        {"fact": "（外部·科技）3D 打印"},
    ]
    segs = uo.decide_and_digest(facts)
    assert len(segs) == 3
    assert segs[0]["body"].startswith("今天看到美伊")
    assert segs[2]["body"].startswith("深圳")
    # 每个段都关联整批 fact_keys（不去做语义映射）
    assert len(segs[0]["fact_keys"]) == 3


def test_decide_and_digest_empty_list_when_bail(tmp_path, monkeypatch):
    """LLM 显式说「素材不足」 → []（不是 None，是 list，调用方走降级）。"""
    import deskmate.agency.behaviors.understand_observations as uo

    monkeypatch.setattr("deskmate.memory.llm.chat_memory", lambda *a, **k: "素材不足")
    monkeypatch.setattr("deskmate.memory.llm.memory_llm_available", lambda: True)

    segs = uo.decide_and_digest([{"fact": "（外部·新闻）某条新闻"}])
    assert segs == []


def test_decide_and_digest_empty_list_when_provider_unavailable(monkeypatch):
    """没配 provider → []（按降级路径写原文 bullet）。"""
    import deskmate.agency.behaviors.understand_observations as uo

    monkeypatch.setattr("deskmate.memory.llm.memory_llm_available", lambda: False)
    segs = uo.decide_and_digest([{"fact": "某事实"}])
    assert segs == []


def test_decide_and_digest_swallows_exception(monkeypatch):
    """LLM 异常 → []（绝不阻断观察日记）。"""
    import deskmate.agency.behaviors.understand_observations as uo

    monkeypatch.setattr("deskmate.memory.llm.memory_llm_available", lambda: True)

    def _boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr("deskmate.memory.llm.chat_memory", _boom)
    segs = uo.decide_and_digest([{"fact": "某事实"}])
    assert segs == []


def test_observation_diary_writes_multiple_entries_when_llm_picks_several(tmp_path, monkeypatch):
    """LLM 选多条 → 每天写多则（每段独立成则，独立 eid）。

    这是 2026-09-07 萧哥拍板"每日篇数不限"的核心契约。
    """
    import deskmate.agency.behaviors.observation_diary as od
    import deskmate.agency.behaviors.understand_observations as uo

    # 注入 3 条世界观察
    _seed_observations(tmp_path, [
        "（外部·要闻）伊朗革命卫队称打击了美航母",
        "（外部·财经）碳酸锂一周跌了 15%",
        "（外部·科技）GPT6 宣传片打码设备是中国造",
    ])
    # LLM 决定挑前两条分别消化
    monkeypatch.setattr(uo, "decide_and_digest", lambda facts, at="", temperature=0.7: [
        {"body": "今天看到美伊对峙的事，我想到你说的边界感。", "fact_keys": ["k1", "k2", "k3"]},
        {"body": "碳酸锂跌了，让我想到你说的消费疲弱那段。", "fact_keys": ["k1", "k2", "k3"]},
    ])

    res = od.run_observation_diary(str(tmp_path), {
        "cooldown_hours": 0, "max_observations": 20, "understand": True,
    })
    assert res["written"] == 2
    assert res["understood"] is True
    assert len(res["segments"]) == 2

    # 每段独立成则：日记文件应有 2 个 ## 小节
    text = (list((tmp_path / "diary").glob("*.md"))[0]).read_text(encoding="utf-8")
    assert text.count("## ") >= 2
    # 两段内容都进文件了
    assert "美伊对峙的事" in text
    assert "碳酸锂跌了" in text
    # 没有拼成一段（不再"强行揉和"）
    # 不要断言绝对不存在"今天看到……碳酸锂"在同一段——但段与段之间有空行
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    # 把每段按 ## 切：第一个 ## 是标题，剩下 ## 也是标题
    # 这里简单验：找到至少一段里只有「美伊对峙」没有「碳酸锂」
    has_first_alone = any(
        "美伊对峙的事" in p and "碳酸锂" not in p for p in paragraphs
    )
    has_second_alone = any(
        "碳酸锂跌了" in p and "美伊" not in p for p in paragraphs
    )
    assert has_first_alone and has_second_alone


def test_observation_diary_writes_nothing_when_llm_says_bail(tmp_path, monkeypatch):
    """LLM 显式说「素材不足」→ 走降级 bullet（不平添空日记）。"""
    import deskmate.agency.behaviors.observation_diary as od
    import deskmate.agency.behaviors.understand_observations as uo

    _seed_observations(tmp_path, [
        "（外部·要闻）某条不痛不痒的消息",
    ])
    monkeypatch.setattr(uo, "decide_and_digest", lambda *a, **k: [])

    res = od.run_observation_diary(str(tmp_path), {
        "cooldown_hours": 0, "max_observations": 20, "understand": True,
    })
    # 走降级 bullet（依然写一条事实 bullet）
    assert res["written"] == 1
    assert res["understood"] is False
    text = (list((tmp_path / "diary").glob("*.md"))[0]).read_text(encoding="utf-8")
    assert "我留意到世界上的一些新动静" in text  # bullet 模板
    assert "某条不痛不痒的消息" in text


def test_understand_observations_legacy_wrapper_returns_first_segment(monkeypatch):
    """旧接口 understand_observations（薄包装）→ 仍返回首段（向后兼容）。

    已有测试 monkeypatch 这个函数，验证兼容契约不动。
    """
    import deskmate.agency.behaviors.understand_observations as uo

    monkeypatch.setattr(uo, "decide_and_digest", lambda *a, **k: [
        {"body": "第一段内容，跟你的写作有关。", "fact_keys": ["a", "b"]},
        {"body": "第二段又是另一件事。", "fact_keys": ["a", "b"]},
    ])
    out = uo.understand_observations([{"fact": "（外部·要闻）某事实"}])
    assert out == "第一段内容，跟你的写作有关。"


def test_understand_observations_legacy_wrapper_returns_none_when_bail(monkeypatch):
    """弃权 → 旧接口仍返回 None（不是空字符串，调用方按 None 判定降级）。"""
    import deskmate.agency.behaviors.understand_observations as uo

    monkeypatch.setattr(uo, "decide_and_digest", lambda *a, **k: [])
    out = uo.understand_observations([{"fact": "（外部·要闻）某事实"}])
    assert out is None


# ----------------------------------------------------------------------
# 2026-09-07 巡检修复：取最新 / dropped 真实统计 / 写盘失败刷冷却
# ----------------------------------------------------------------------
def test_observation_diary_picks_newest_first(tmp_path):
    """P0 修复：观察日记取**最新**的世界观察，不是最旧的。

    ``active_recent`` 返回时间正序（最老在前），旧实现正序遍历 + break 会先
    取到库里最旧的积压旧闻，最新的排在最后好几轮 tick 才轮得到。修复后反序，
    最新优先——「看世界 → 记当下」。
    """
    from deskmate.memory.learned_store import LearnedStore
    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    base = str(tmp_path)
    # 两条旧的（ts 早）
    LearnedStore(base_dir=base).append(
        ["（外部·news）旧新闻甲", "（外部·news）旧新闻乙"],
        ts="2026-09-01T08:00:00",
    )
    # 两条新的（ts 晚）
    LearnedStore(base_dir=base).append(
        ["（外部·news）新新闻丙", "（外部·news）新新闻丁"],
        ts="2026-09-07T08:00:00",
    )

    res = run_observation_diary(base, {
        "cooldown_hours": 0, "max_observations": 2, "understand": False,
    })
    assert res["written"] == 2
    text = (list((tmp_path / "diary").glob("*.md"))[0]).read_text(encoding="utf-8")
    assert "新新闻丙" in text
    assert "新新闻丁" in text
    assert "旧新闻甲" not in text
    assert "旧新闻乙" not in text


def test_observation_diary_reports_dropped_count(tmp_path, monkeypatch):
    """P0 修复：LLM 只挑一部分时，dropped_by_llm 真实反映丢弃数（不再恒 0）。

    旧实现把整批 fact_keys 关联到每段，kept 恒等于全集 → dropped 永远 0。
    修复后用段数近似：6 条交给她、她挑 2 段 → dropped == 4。
    """
    import deskmate.agency.behaviors.observation_diary as od
    import deskmate.agency.behaviors.understand_observations as uo

    _seed_observations(tmp_path, [
        "（外部·要闻）新闻一",
        "（外部·要闻）新闻二",
        "（外部·要闻）新闻三",
        "（外部·要闻）新闻四",
        "（外部·要闻）新闻五",
        "（外部·要闻）新闻六",
    ])
    monkeypatch.setattr(uo, "decide_and_digest", lambda *a, **k: [
        {"body": "今天看到新闻一，让我想到你写的那一段，跟你的题材对得上。", "fact_keys": ["k"]},
        {"body": "新闻二也值得记一笔，跟你在意的那个方向有关。", "fact_keys": ["k"]},
    ])

    res = od.run_observation_diary(str(tmp_path), {
        "cooldown_hours": 0, "max_observations": 20, "understand": True,
    })
    assert res["written"] == 2
    assert res["dropped_by_llm"] == 4     # 6 交给她，挑 2 段，丢 4 条
    assert "4 条不记也罢" in res["summary"]


def test_observation_diary_refreshes_cooldown_on_write_failure(tmp_path, monkeypatch):
    """P1 修复：写盘失败也要刷冷却 + 标已记，避免下一 tick 重复调 LLM。

    旧实现 ``if written_now:`` 才落 state/冷却——写盘失败（written=0）时两样
    都不落，下一 tick 重跑、反复调 LLM、反复空转。
    """
    import deskmate.agency.behaviors.observation_diary as od
    import deskmate.agency.behaviors.understand_observations as uo
    from deskmate.growth.diary import DiaryWriter

    _seed_observations(tmp_path, ["（外部·要闻）某条新闻"])

    monkeypatch.setattr(uo, "decide_and_digest", lambda *a, **k: [
        {"body": "今天看到一条新闻，值得我记下来想一想。", "fact_keys": ["k"]},
    ])
    # 写盘失败：append_observation_natural 返回 seq=0
    monkeypatch.setattr(
        DiaryWriter, "append_observation_natural",
        lambda self, date, text, at=None: {"date": date, "path": "", "seq": 0, "written": 0},
    )

    res = od.run_observation_diary(str(tmp_path), {
        "cooldown_hours": 0, "max_observations": 20, "understand": True,
    })
    assert res["written"] == 0

    # 冷却必须已刷（修复前 written=0 不落，文件无 ts）
    cooldown = od._load_json(od._cooldown_path(str(tmp_path)))
    assert "ts" in cooldown
    # 状态也标已记（下一 tick 不会重复提交同一批）
    state = od._load_json(od._state_path(str(tmp_path)))
    assert len(state.get("keys", [])) == 1
