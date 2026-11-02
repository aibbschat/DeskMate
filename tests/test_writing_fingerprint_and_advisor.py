"""文风指纹分桶 + 写作顾问自动接入 / 观感沉淀 的 hermetic 回归。

三项一起验：
1. 书稿样本与对话样本分开存，写作侧指纹只认书稿桶（无书稿时明确降级告知）；
2. try_advise 的自动门槛（够长 / 像稿子 / 冷却），降级时不往聊天里塞；
3. 她的读稿观感落进 worldview 层（成功才写，失败不影响返回）。

全程用临时目录隔离，不碰真实 ~/.deskmate。
"""

import importlib

import pytest

FP_MOD = "deskmate.style_profile_store"
ADV_MOD = "deskmate.cognitive.writing_advisor"


def _fp():
    return importlib.import_module(FP_MOD)


def _adv():
    return importlib.import_module(ADV_MOD)


# ----------------------------------------------------------------------
# 1. 文风指纹分桶
# ----------------------------------------------------------------------
def test_buckets_isolate_chat_from_manuscript(tmp_path):
    m = _fp()
    path = tmp_path / "style.json"
    chat = "今天聊了很多。好的。继续。这段聊天用的全是短句。" * 5
    book = (
        "此去经年，应是良辰好景虚设。便纵有千种风情，更与何人说。"
        "此事关乎国子监生死，臣不得不言；若再迟疑，满门皆祸。"
    ) * 3

    m.update_style_profile(chat, source="对话-2026-11-01", profile_path=path, bucket="chat")
    m.update_style_profile(book, source="书稿-国子监秘闻录", profile_path=path, bucket="manuscript")

    data = m.load_profile(path)
    assert data["buckets"]["chat"]["metrics"]["n"] == 1
    assert data["buckets"]["manuscript"]["metrics"]["n"] == 1
    # 书稿口径的句子明显更长（聊天语感不得稀释书稿文风）
    assert (
        data["buckets"]["manuscript"]["metrics"]["sentences"]["avg_len"]
        > data["buckets"]["chat"]["metrics"]["sentences"]["avg_len"]
    )


def test_fingerprint_uses_manuscript_bucket_only(tmp_path):
    m = _fp()
    path = tmp_path / "style.json"
    m.update_style_profile("短句。短句。短句。", source="对话-2026-11-01", profile_path=path, bucket="chat")
    long_book = "他站在城头，看着远处连成一片的营火，忽然想起母亲临行前那句话——活着回来，比什么都强。" * 2
    m.update_style_profile(long_book, source="书稿-第一部", profile_path=path, bucket="manuscript")
    m.clear_style_fingerprint_cache()

    fp = m.format_style_fingerprint(path)
    assert "暂无书稿样本" not in fp
    assert "样本数：1" in fp


def test_fingerprint_degrades_when_no_manuscript(tmp_path):
    """只有聊天样本时，指纹明确降级告知，绝不拿聊天语感冒充文风。"""
    m = _fp()
    path = tmp_path / "style.json"
    m.update_style_profile("今天聊了很多，主要是工作和生活上的事。", source="对话-2026-11-01", profile_path=path, bucket="chat")
    m.clear_style_fingerprint_cache()
    fp = m.format_style_fingerprint(path)
    assert "暂无书稿样本" in fp


def test_manuscript_ingest_is_idempotent(tmp_path):
    """同一份书稿反复打开审阅，不能把画像刷爆（按内容去重）。"""
    m = _fp()
    path = tmp_path / "style.json"
    text = "第一章：他推门进去，屋里没有人，只有一盏将熄的灯。桌上压着一封没写完的信。"
    m.update_style_profile(text, source="书稿-A", profile_path=path, bucket="manuscript")
    n1 = m.load_profile(path)["buckets"]["manuscript"]["metrics"]["n"]
    m.update_style_profile(text, source="书稿-A", profile_path=path, bucket="manuscript")
    assert m.load_profile(path)["buckets"]["manuscript"]["metrics"]["n"] == n1


# ----------------------------------------------------------------------
# 2. 写作顾问自动接入
# ----------------------------------------------------------------------
class _FakeProfile:
    def __init__(self):
        self.stances = ["我相信：看清谁在争，比站队更重要。"]
        self.value_rankings = []
        self.thinking_frameworks = []


_ADVICE = (
    "这段最让我别扭的是：主角的犹豫没有来由。按我对权力的理解，城破前夜的动摇，"
    "往往不是怕死，是怕背锅——你没把这一层写出来，人物就空了。这不是技巧问题，是你看人还没看透。"
)
_DRAFT = (
    "第一章：主角在城破前夜犹豫是否投降。母亲让他走，他却拔剑。守将问他为何不走，他说要还债。"
    "他在城头上站了很久，风从北面来，带着血腥味。他想不明白，到底是为谁守这座城。"
) * 2


def _ready(monkeypatch):
    monkeypatch.setattr(f"{ADV_MOD}.load_cognitive_profile", lambda *a, **k: _FakeProfile())
    monkeypatch.setattr(
        f"{ADV_MOD}.load_worldview_beliefs", lambda base_dir: "我相信：看清谁在争，比站队更重要。"
    )
    monkeypatch.setattr(f"{ADV_MOD}.memory_llm_available", lambda: True)
    monkeypatch.setattr(f"{ADV_MOD}.chat_memory", lambda *a, **k: _ADVICE)


def test_try_advise_skips_short_text(tmp_path):
    out = _adv().try_advise(str(tmp_path), "写完了。")
    assert out["triggered"] is False
    assert out["reason"] == "too_short"


def test_try_advise_skips_plain_chitchat(tmp_path):
    chatter = "我觉得今天状态一般，想先休息一下，晚点再跟你聊，最近确实有点累，" * 6
    out = _adv().try_advise(str(tmp_path), chatter)
    assert out["triggered"] is False
    assert out["reason"] == "not_draft"


def test_try_advise_triggers_then_cools_down(monkeypatch, tmp_path):
    _ready(monkeypatch)
    monkeypatch.setattr(
        f"{ADV_MOD}._persist_judgment", lambda base, kind, draft, advice: True
    )
    first = _adv().try_advise(str(tmp_path), _DRAFT)
    assert first["triggered"] is True
    assert first["advice"] == _ADVICE
    assert first["kind"] == "general"  # 纯叙事段落没有「情节/人物」等词，按一般写作读

    second = _adv().try_advise(str(tmp_path), _DRAFT)
    assert second["triggered"] is False
    assert second["reason"] == "cooldown"


def test_try_advise_hides_degraded_state(monkeypatch, tmp_path):
    """降级（没 provider / 没世界观）时不往聊天里塞话，只返回 triggered=False。"""
    monkeypatch.setattr(f"{ADV_MOD}.load_cognitive_profile", lambda *a, **k: _FakeProfile())
    monkeypatch.setattr(
        f"{ADV_MOD}.load_worldview_beliefs", lambda base_dir: "我相信：看清谁在争，比站队更重要。"
    )
    monkeypatch.setattr(f"{ADV_MOD}.memory_llm_available", lambda: False)
    out = _adv().try_advise(str(tmp_path), _DRAFT)
    assert out["triggered"] is False
    assert out["reason"] == "no_provider"


# ----------------------------------------------------------------------
# 3. 观感沉淀
# ----------------------------------------------------------------------
def test_advise_persists_judgment_on_success(monkeypatch, tmp_path):
    m = _adv()
    _ready(monkeypatch)
    seen = []

    def _fake_persist(base, kind, draft, advice):
        seen.append((kind, advice))
        return True

    monkeypatch.setattr(f"{ADV_MOD}._persist_judgment", _fake_persist)
    res = m.advise(str(tmp_path), _DRAFT, "plot")
    assert res["ok"] is True
    assert res["judgment_persisted"] is True
    assert seen == [("plot", _ADVICE)]


def test_advise_does_not_persist_when_degraded(monkeypatch, tmp_path):
    """没世界观 / 没 provider / 没看法 —— 都不该写她的「观感」（不编造立场）。"""
    m = _adv()
    monkeypatch.setattr(f"{ADV_MOD}.memory_llm_available", lambda: False)
    monkeypatch.setattr(
        f"{ADV_MOD}._persist_judgment",
        lambda *a, **k: pytest.fail("降级路径不应沉淀观感"),
    )
    res = m.advise(str(tmp_path), _DRAFT, "plot")
    assert res["judgment_persisted"] is False


# ----------------------------------------------------------------------
# 4. 创作对话流接入
# ----------------------------------------------------------------------
def test_chat_flow_recognizes_advisor_intent():
    """「你读读这段 / 给点看法」→ advise 意图（而不是被当成普通闲聊/灵感）。"""
    from deskmate.creation.chat_flow import parse_intent

    assert parse_intent("你读读这段：城破前的那个晚上，他一直在犹豫。", {}).type == "advise"
    assert parse_intent("给点看法", {}).type == "advise"


def test_chat_flow_long_draft_looks_like_draft():
    """长段落且像稿子 → 自动读稿；普通长闲聊不触发。"""
    m = _adv()
    assert m.looks_like_draft(_DRAFT) is True
    assert m.looks_like_draft("我觉得今天状态一般，想先休息一下，晚点再跟你聊，" * 6) is False
