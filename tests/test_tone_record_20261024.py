"""语气层注入（2026-10-24）单测：只递她自己的话，不递数值/标签/分档。"""

from deskmate.growth.tone_record import build_tone_record


class _FakeEngine:
    def __init__(self, desc, narratives):
        self._desc = desc
        self._narratives = narratives

    def get_description(self):
        return self._desc

    def recent_narratives(self, limit=2):
        return self._narratives


def test_delivers_her_own_words_and_strips_meta():
    out = build_tone_record(
        engine=_FakeEngine(
            "我以为我会高兴，可那句话还搁在心里（原因：被否了）",
            ["上次他说以后要主动提，我答应了还没做到"],
        )
    )
    assert "我以为我会高兴，可那句话还搁在心里" in out
    assert "（原因：被否了）" not in out
    assert "上次他说以后要主动提" in out
    assert "不是命令" in out
    assert "以你当下的感觉为准" in out


def test_dedupes_monologue_vs_narrative():
    out = build_tone_record(
        engine=_FakeEngine("同一句话", ["同一句话"])
    )
    assert out.count("同一句话") == 1


def test_empty_record_is_empty_and_never_crashes():
    assert build_tone_record(engine=_FakeEngine("", [])) == ""

    class _Broken:
        def get_description(self):
            raise RuntimeError("boom")

        def recent_narratives(self, limit=2):
            raise RuntimeError("boom")

    assert build_tone_record(engine=_Broken()) == ""
