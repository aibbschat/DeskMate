"""日记→情感画像提取器测试。"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from deskmate.cognitive.profile import CognitiveProfileManager
from deskmate.cognitive.emotion_extract import (
    extract_diary_into_emotion,
    _emotion_rule_extract,
    maybe_extract_on_tick,
)
from deskmate.cognitive.models import SourceRef


class _FakeDiaryStore:
    """注入临时日记存储（避免碰真实库）。"""

    def __init__(self, posts):
        self._posts = posts

    def get_posts(self, limit=200):
        return {"items": self._posts, "next_cursor": None}


def _diary(pid, content, created="2026-09-01T10:00:00"):
    return {"id": pid, "author": "me", "content": content, "createdAt": created}


def test_emotion_rule_extract_writes_emotional_layer():
    """情感规则应写入 layer='emotional' 的画像，且不碰认知层。"""
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"
    pm = CognitiveProfileManager(db, layer="emotional")

    post = _diary(
        "p1",
        "下午第三章写顺了，真好！写起来很顺利很舒服，中间卡住的时候重读史料就能找回感觉。",
    )
    n = _emotion_rule_extract(post, pm)
    assert n >= 3, f"应提取至少3条（正向+回血），实际 {n}"

    stances = [s for s in pm.get().stances]
    assert len(stances) >= 3
    topics = [s.topic for s in stances]
    assert any("写作顺畅" in t for t in topics), topics
    assert any("回血方式" in t for t in topics), topics

    # 全部带情感层 tag
    for s in stances:
        tags = list(s.tags or [])
        assert "情感层" in tags, f"条目 {s.id} 缺情感层 tag: {tags}"
    import shutil

    shutil.rmtree(tmp)
    print("OK test_emotion_rule_extract_writes_emotional_layer")


def test_emotion_extract_idempotent_and_isolated():
    """情感提取应按 post id 幂等、且独立于认知层（互不污染）。"""
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"

    cog = CognitiveProfileManager(db)  # 认知层
    emo = CognitiveProfileManager(db, layer="emotional")  # 情感层

    posts = [
        _diary("p1", "写作受阻，很烦，找不到感觉，卡住了。"),
        _diary("p2", "看到社会不平之事很郁闷，只能借写纪实书抒发。"),
    ]
    store = _FakeDiaryStore(posts)

    res1 = extract_diary_into_emotion(pm=emo, diary_store=store)
    assert res1["new_posts"] == 2, res1
    assert res1["emotion_items"] >= 2, res1

    # 认知层不受影响（emotional 不写入 cognitive）
    assert len(cog.get().stances) == 0, "情感提取不应污染认知层"

    # 幂等：重跑无新帖
    res2 = extract_diary_into_emotion(pm=emo, diary_store=store)
    assert res2["new_posts"] == 0, res2

    import shutil

    shutil.rmtree(tmp)
    print("OK test_emotion_extract_idempotent_and_isolated")


def test_emotion_rule_captures_creative_regret():
    """创作理想落差异（想写爽文但考据后不可能）应被捕捉为深层情绪。"""
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"
    pm = CognitiveProfileManager(db, layer="emotional")

    post = _diary(
        "p9",
        "这本书我一直有个遗憾，本来设定想写成爽文，但考据后发现历史体制下小人物想要爽是不可能的，这让我感到忧伤、无奈。",
    )
    n = _emotion_rule_extract(post, pm)
    assert n >= 1, f"应捕捉创作理想落差，实际 {n}"
    topics = [s.topic for s in pm.get().stances]
    assert any("创作理想落差" in t for t in topics), topics
    import shutil

    shutil.rmtree(tmp)
    print("OK test_emotion_rule_captures_creative_regret")


def test_emotion_layer_separate_from_cognitive():
    """同一库中 cognitive 与 emotional 层互不覆盖。"""
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"

    cog = CognitiveProfileManager(db)
    emo = CognitiveProfileManager(db, layer="emotional")

    cog.add_stance("测试", "认知立场", SourceRef(type="diary", title="t", excerpt="e"))
    emo.add_stance("情感触发·测试", "情感条目", SourceRef(type="diary", title="t", excerpt="e"))

    assert len(cog.get().stances) == 1
    assert len(emo.get().stances) == 1
    assert cog.get().stances[0].topic == "测试"
    assert emo.get().stances[0].topic == "情感触发·测试"

    # 重新加载后仍分离
    cog2 = CognitiveProfileManager(db)
    emo2 = CognitiveProfileManager(db, layer="emotional")
    assert len(cog2.get().stances) == 1 and cog2.get().stances[0].topic == "测试"
    assert len(emo2.get().stances) == 1 and emo2.get().stances[0].topic == "情感触发·测试"

    import shutil

    shutil.rmtree(tmp)
    print("OK test_emotion_layer_separate_from_cognitive")


def test_maybe_extract_on_tick_throttles():
    """maybe_extract_on_tick 应首次提取、节流内二次跳过；且幂等不污染认知层。"""
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"
    emo = CognitiveProfileManager(db, layer="emotional")
    cog = CognitiveProfileManager(db)  # 认知层
    posts = [
        _diary("p1", "写作受阻，很烦，找不到感觉，卡住了。"),
        _diary("p2", "下午写顺了，真好，安静独处让我放松舒服。"),
    ]
    store = _FakeDiaryStore(posts)

    r1 = maybe_extract_on_tick(interval_hours=4, pm=emo, diary_store=store)
    assert r1 is not None, "首次应执行提取"
    assert r1["new_posts"] == 2, r1
    assert r1["emotion_items"] >= 2, r1

    # 节流未到 → 应返回 None（不重复扫库）
    r2 = maybe_extract_on_tick(interval_hours=4, pm=emo, diary_store=store)
    assert r2 is None, f"节流内应跳过，实际 {r2}"

    # 认知层不受影响
    assert len(cog.get().stances) == 0, "情感提取不应污染认知层"

    import shutil

    shutil.rmtree(tmp)
    print("OK test_maybe_extract_on_tick_throttles")


if __name__ == "__main__":
    test_emotion_rule_extract_writes_emotional_layer()
    test_emotion_extract_idempotent_and_isolated()
    test_emotion_layer_separate_from_cognitive()
    test_maybe_extract_on_tick_throttles()
    print("全部情感提取测试通过")
