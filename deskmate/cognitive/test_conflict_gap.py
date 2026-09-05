"""
冲突驱动 gap 再生（方案 C）测试。

核心命题三件事：
1. 真冲突能生成 gap（否则好奇心还是死的）；
2. 同极性/情绪起伏**绝不**生成 gap（否则就是历史上踩过的伪 gap 僵尸）；
3. 对真实存量画像跑扫描不产生伪 gap（上线安全底线）。
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from deskmate.cognitive.profile import CognitiveProfileManager
from deskmate.cognitive.models import SourceRef
from deskmate.cognitive import conflict_gap


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------
class _FakeDiary:
    """最小日记存储替身，接口对齐 DiaryStore.get_posts。"""

    def __init__(self, posts):
        self._posts = posts

    def get_posts(self, limit=200):
        return {"items": self._posts[:limit]}


class _BoomDiary:
    def get_posts(self, limit=200):
        raise RuntimeError("日记库炸了")


def _pm(tmpdir, name="t.db"):
    return CognitiveProfileManager(Path(tmpdir) / name, layer="emotional")


def _src(t="d1"):
    return SourceRef(type="diary", title=t, excerpt="节选", date="2026-09-04")


def _seed_care_support(pm):
    """既有画像：关怀偏好 = support 向（希望被关怀，源自萧哥真实访谈回答）。"""
    pm.add_stance(
        "情感·关怀偏好",
        "写作的人是孤独的，所以我喜欢被关怀，被温柔的对待，被理解。",
        _src("iv1"),
        tags=["访谈", "情感层", "care_preference"],
        category="访谈",
    )
    pm.save()
    return pm


# ----------------------------------------------------------------------
# 1. 真冲突 → 生成 gap
# ----------------------------------------------------------------------
def test_reverse_polarity_spawns_gap():
    tmp = tempfile.mkdtemp()
    try:
        pm = _seed_care_support(_pm(tmp))
        # 新日记出现相反信号：想独处、别打扰
        diary = _FakeDiary([
            {"id": "p1", "content": "今天谁也别来烦我，我想一个人静静，自己消化一下。"},
        ])
        res = conflict_gap.scan_and_spawn_gaps(pm=pm, diary_store=diary)

        assert res["new_posts"] == 1, res
        assert res["gaps_added"] == 1, res

        gaps = [g for g in pm.get().cognitive_gaps if g.status == "open"]
        assert len(gaps) == 1
        assert gaps[0].topic == "画像冲突·关怀偏好"
        assert "想法变了" in gaps[0].reason, gaps[0].reason
        assert gaps[0].priority == 0.8, "冲突是高优 gap：一个即触发访谈"
        assert gaps[0].related_items, "应关联被质疑的既有条目"
    finally:
        shutil.rmtree(tmp)
    print("OK test_reverse_polarity_spawns_gap")


# ----------------------------------------------------------------------
# 2. 同极性 → 绝不生成 gap（防伪 gap 第一道闸）
# ----------------------------------------------------------------------
def test_same_polarity_no_gap():
    tmp = tempfile.mkdtemp()
    try:
        pm = _seed_care_support(_pm(tmp))
        # 同向日记：依然希望被关怀 → 只是补充证据
        diary = _FakeDiary([
            {"id": "p1", "content": "今天写到很晚，还是希望有人关心我一下，被理解真的重要。"},
        ])
        res = conflict_gap.scan_and_spawn_gaps(pm=pm, diary_store=diary)

        assert res["new_posts"] == 1
        assert res["gaps_added"] == 0, "同极性是补充证据，绝不能生成 gap"
        assert not [g for g in pm.get().cognitive_gaps if g.status == "open"]
    finally:
        shutil.rmtree(tmp)
    print("OK test_same_polarity_no_gap")


# ----------------------------------------------------------------------
# 3. 情绪正负起伏不算冲突（只检测偏好类维度）
# ----------------------------------------------------------------------
def test_emotional_trigger_swing_ignored():
    tmp = tempfile.mkdtemp()
    try:
        pm = _pm(tmp)
        pm.add_stance(
            "情感触发·写作顺畅",
            "写作顺利、章节写顺时情绪明显变好，创作进入状态是最愉悦的时刻。",
            _src("d1"),
            tags=["情感层", "emotional_trigger", "positive"],
            category="情感触发点",
        )
        pm.save()
        # 今天郁闷 —— 情绪起伏是情境性的，不是偏好改变
        diary = _FakeDiary([
            {"id": "p1", "content": "今天卡住了，压力很大，郁闷。"},
        ])
        res = conflict_gap.scan_and_spawn_gaps(pm=pm, diary_store=diary)

        assert res["gaps_added"] == 0, "情绪起伏属噪声，问了也白问"
    finally:
        shutil.rmtree(tmp)
    print("OK test_emotional_trigger_swing_ignored")


# ----------------------------------------------------------------------
# 4. 幂等：同一冲突不重复堆积 gap
# ----------------------------------------------------------------------
def test_idempotent_no_duplicate():
    tmp = tempfile.mkdtemp()
    try:
        pm = _seed_care_support(_pm(tmp))
        diary = _FakeDiary([
            {"id": "p1", "content": "别来烦我，我想一个人静静。"},
            {"id": "p2", "content": "今天不想被打扰，让我静一静。"},
        ])
        res = conflict_gap.scan_and_spawn_gaps(pm=pm, diary_store=diary)

        assert res["gaps_added"] == 1, f"同维度冲突只应生成一个，实际 {res['gaps_added']}"
        assert res["conflicts"] == 1, (
            "两条都反向，但第二条因同维度已存在待澄清 gap 而被去重（不重复堆积）"
        )

        # 第二轮：两帖都已处理 → 不再新增
        res2 = conflict_gap.scan_and_spawn_gaps(pm=pm, diary_store=diary)
        assert res2["new_posts"] == 0, "已扫描的日记不应重复处理"
        assert res2["gaps_added"] == 0
        assert len([g for g in pm.get().cognitive_gaps if g.status == "open"]) == 1
    finally:
        shutil.rmtree(tmp)
    print("OK test_idempotent_no_duplicate")


# ----------------------------------------------------------------------
# 5. 只扫新日记
# ----------------------------------------------------------------------
def test_only_scans_new_posts():
    tmp = tempfile.mkdtemp()
    try:
        pm = _seed_care_support(_pm(tmp))
        first = _FakeDiary([{"id": "p1", "content": "别来烦我，一个人静静。"}])
        conflict_gap.scan_and_spawn_gaps(pm=pm, diary_store=first)
        assert len([g for g in pm.get().cognitive_gaps if g.status == "open"]) == 1

        # 同一批 + 新帖：只有新帖参与检测
        second = _FakeDiary([
            {"id": "p1", "content": "别来烦我，一个人静静。"},
            {"id": "p2", "content": "今天写顺了，很开心。"},  # 无冲突
        ])
        res = conflict_gap.scan_and_spawn_gaps(pm=pm, diary_store=second)
        assert res["new_posts"] == 1, "p1 应已处理，只剩 p2"
        assert res["gaps_added"] == 0
    finally:
        shutil.rmtree(tmp)
    print("OK test_only_scans_new_posts")


# ----------------------------------------------------------------------
# 6. 上线安全底线：对真实存量画像扫描，不产生伪 gap
# ----------------------------------------------------------------------
_REAL_SNAPSHOT = [
    ("情感·回血方式",
     "情绪低落时，我会看一些国外优秀的影视剧、读书、听音乐或看电影。也会偶尔去外面独处，走走，吹吹风。",
     ["访谈", "认知", "情感层", "recovery_pattern"], "访谈"),
    ("情感·关怀偏好",
     "写作的人是孤独的，往往活在自己创建和想像的世界里，但回到现实是孤独的，所以我喜欢被关怀，被温柔的对待，被理解。",
     ["访谈", "认知", "情感层", "care_preference"], "访谈"),
    ("回血方式",
     "情绪恢复靠投入创作/史料：写作——如重读旧稿找回感觉、查史料、在安静环境独处。",
     ["情感层", "recovery_pattern", "写作", "安静"], "回血方式"),
    ("情感触发·对作品的珍视",
     "对写出来的作品有很强珍视感，书稿不是商品而是倾注心血的产物。",
     ["情感层", "emotional_trigger", "positive", "作品珍视"], "情感触发点"),
    ("情感触发·私密空间与安全感",
     "在小桔日记这样的私密空间里表达让他感到安心、温暖、有安全感。",
     ["情感层", "emotional_trigger", "positive", "安全感"], "情感触发点"),
    ("情感触发·写作顺畅",
     "写作顺利、章节写顺时情绪明显变好，创作进入状态是最让他愉悦的时刻。",
     ["情感层", "emotional_trigger", "positive", "写顺"], "情感触发点"),
    ("情感触发·创作理想落差",
     "创作时存在理想与现实的落差，由此感到遗憾、忧伤、无奈。",
     ["情感层", "emotional_trigger", "negative", "遗憾"], "情感触发点"),
    ("情感触发·现实不平",
     "看到社会上不平之事、奇怪现象会让他心情郁闷，且常借写纪实类历史书抒发。",
     ["情感层", "emotional_trigger", "negative", "郁闷"], "情感触发点"),
    ("情感触发·读者连接",
     "与读者的连接让他感到温暖、被看见，写作不只是自我表达，也是和人建立理解的过程。",
     ["情感层", "emotional_trigger", "positive", "读者连接"], "情感触发点"),
]


def test_real_profile_snapshot_no_fake_gap():
    """用真实 9 条情感画像 + 一批'平常日记'扫描，必须一个 gap 都不生成。

    这是上线底线：若这里挂了，说明一上线就会往库里灌伪 gap。
    """
    tmp = tempfile.mkdtemp()
    try:
        pm = _pm(tmp)
        for i, (topic, content, tags, cat) in enumerate(_REAL_SNAPSHOT):
            pm.add_stance(topic, content, _src(f"real{i}"), tags=tags, category=cat)
        pm.save()

        ordinary = [
            {"id": "n1", "content": "今天查了一天史料，写了两千字，还算顺利。"},
            {"id": "n2", "content": "读《明史》到深夜，安静的夜里思路最清楚。"},
            {"id": "n3", "content": "看到读者留言说被某段打动，有点感动。"},
            {"id": "n4", "content": "这一章卡住了，找不回感觉，有点烦。"},
            {"id": "n5", "content": "整理笔记，把散乱的考据重新归类，踏实。"},
            {"id": "n6", "content": "书稿改了又改，一字一句都是心血。"},
        ]
        res = conflict_gap.scan_and_spawn_gaps(
            pm=pm, diary_store=_FakeDiary(ordinary)
        )
        assert res["gaps_added"] == 0, (
            f"平常日记不该触发冲突，实际生成 {res['gaps_added']} 个："
            f"{res['conflict_detail']}"
        )
        assert not [g for g in pm.get().cognitive_gaps if g.status == "open"]
    finally:
        shutil.rmtree(tmp)
    print("OK test_real_profile_snapshot_no_fake_gap")


# ----------------------------------------------------------------------
# 7. 真实画像 + 明确反向信号 → 应当检出（不能矫枉过正到检测不出来）
# ----------------------------------------------------------------------
def test_real_profile_detects_genuine_flip():
    tmp = tempfile.mkdtemp()
    try:
        pm = _pm(tmp)
        for i, (topic, content, tags, cat) in enumerate(_REAL_SNAPSHOT):
            pm.add_stance(topic, content, _src(f"real{i}"), tags=tags, category=cat)
        pm.save()

        # 明确反向：他说过"喜欢被关怀"，现在说"别来烦我，想一个人待着"
        flipped = _FakeDiary([
            {"id": "f1", "content": "这段时间不想被打扰，别来烦我，想一个人待着，自己消化。"},
        ])
        res = conflict_gap.scan_and_spawn_gaps(pm=pm, diary_store=flipped)
        assert res["gaps_added"] == 1, (
            f"明确反向信号应被检出，实际 {res['gaps_added']}"
        )
        gaps = [g for g in pm.get().cognitive_gaps if g.status == "open"]
        assert gaps[0].topic == "画像冲突·关怀偏好"
    finally:
        shutil.rmtree(tmp)
    print("OK test_real_profile_detects_genuine_flip")


# ----------------------------------------------------------------------
# 8. fail-open：日记炸了不能拖垮 tick
# ----------------------------------------------------------------------
def test_fail_open_on_diary_error():
    tmp = tempfile.mkdtemp()
    try:
        pm = _seed_care_support(_pm(tmp))
        res = conflict_gap.scan_and_spawn_gaps(pm=pm, diary_store=_BoomDiary())
        assert res["gaps_added"] == 0
        assert res["note"].startswith("diary_read_failed"), res
    finally:
        shutil.rmtree(tmp)
    print("OK test_fail_open_on_diary_error")


def test_fail_open_on_tick_error():
    """tick 入口必须吞掉一切异常（绝不阻塞主流程）。"""
    tmp = tempfile.mkdtemp()
    try:
        pm = _seed_care_support(_pm(tmp))
        out = conflict_gap.maybe_scan_conflicts_on_tick(
            interval_hours=0, pm=pm, diary_store=_BoomDiary()
        )
        assert out is None or isinstance(out, dict), out
    finally:
        shutil.rmtree(tmp)
    print("OK test_fail_open_on_tick_error")


# ----------------------------------------------------------------------
# 9. 节流：间隔未到不跑
# ----------------------------------------------------------------------
def test_tick_throttle():
    tmp = tempfile.mkdtemp()
    try:
        pm = _seed_care_support(_pm(tmp))
        diary = _FakeDiary([{"id": "p1", "content": "别来烦我，一个人静静。"}])

        r1 = conflict_gap.maybe_scan_conflicts_on_tick(
            interval_hours=6, pm=pm, diary_store=diary
        )
        assert r1 is not None and r1["gaps_added"] == 1

        # 立刻再跑 → 被节流
        r2 = conflict_gap.maybe_scan_conflicts_on_tick(
            interval_hours=6, pm=pm, diary_store=diary
        )
        assert r2 is None, "6h 内不应重复扫描"
    finally:
        shutil.rmtree(tmp)
    print("OK test_tick_throttle")


# ----------------------------------------------------------------------
# 10. 闭环：冲突 gap 必须能真正触发访谈（防死代码回归）
# ----------------------------------------------------------------------
def test_conflict_gap_triggers_interview():
    """真冲突 → gap → 访谈触发。少一环，方案 C 就是躺在库里的死代码。

    历史坑：should_interview_now 原本只认 `open_gaps() >= 2`，而单维度冲突
    只会生成 1 个 gap → gap 生成了却永远触发不了访谈。故引入「高优 gap 一个即问」。
    """
    import deskmate.cognitive.interview as iv_mod
    from deskmate.cognitive.store import CognitiveStore as _RealStore
    from deskmate.cognitive.interview import (
        maybe_trigger_interview, get_interview_engine,
    )

    tmp = tempfile.mkdtemp()
    saved_store, saved_gpm = iv_mod.CognitiveStore, iv_mod.get_profile_manager
    try:
        pm = _seed_care_support(_pm(tmp))
        diary = _FakeDiary([
            {"id": "p1", "content": "别来烦我，我想一个人静静，自己消化。"},
        ])
        res = conflict_gap.scan_and_spawn_gaps(pm=pm, diary_store=diary)
        assert res["gaps_added"] == 1

        # 隔离：让访谈引擎用临时库（否则读到真实库的冷却与画像）
        class _TmpStore(_RealStore):
            def __init__(self, *a, **kw):
                super().__init__(db_path=str(Path(tmp) / "t.db"), legacy_json=None)

        iv_mod.CognitiveStore = _TmpStore
        iv_mod.get_profile_manager = lambda layer="cognitive": pm

        gaps = pm.get().open_gaps()
        assert len(gaps) == 1, "只有 1 个 gap，靠数量门槛(<2)是过不了的"
        assert get_interview_engine().should_interview_now(
            pm.get(), layer="emotional"
        ), "高优冲突 gap 应当一个即触发"

        iv = maybe_trigger_interview(limit=3, layer="emotional")
        assert iv and iv.get("session_id"), f"访谈未触发：{iv}"
        assert iv.get("questions"), "触发了但没生成问题"
    finally:
        iv_mod.CognitiveStore = saved_store
        iv_mod.get_profile_manager = saved_gpm
        shutil.rmtree(tmp)
    print("OK test_conflict_gap_triggers_interview")


if __name__ == "__main__":
    test_reverse_polarity_spawns_gap()
    test_same_polarity_no_gap()
    test_emotional_trigger_swing_ignored()
    test_idempotent_no_duplicate()
    test_only_scans_new_posts()
    test_real_profile_snapshot_no_fake_gap()
    test_real_profile_detects_genuine_flip()
    test_fail_open_on_diary_error()
    test_fail_open_on_tick_error()
    test_tick_throttle()
    test_conflict_gap_triggers_interview()
    print("\nALL conflict_gap TESTS PASSED")


# ----------------------------------------------------------------------
# 11. F3：扫描范围不止日记
# ----------------------------------------------------------------------
def test_expression_source_catches_what_diary_misses(monkeypatch):
    """冲突信号在访谈里时，只扫日记**永远**发现不了。

    真实场景：他在访谈里答"低落时看电影、出去走走"，却从没写进日记，
    光扫日记就发现不了它跟"回血靠投入创作"的张力——再生机制形同虚设。
    """
    from deskmate.cognitive import expressions

    tmp = tempfile.mkdtemp()
    try:
        # ① 日记路径：日记里没有冲突信号 → 不该生成 gap
        pm_diary = _seed_care_support(_pm(tmp, "diary.db"))
        calm = _FakeDiary([{"id": "p1", "content": "今天改了一版稿子，进度一般。"}])
        res_diary = conflict_gap.scan_and_spawn_gaps(pm=pm_diary, diary_store=calm)
        assert res_diary["gaps_added"] == 0, "日记里没有反向信号，不该生 gap"

        # ② 多源路径：同样的画像，但冲突信号出现在访谈回答里
        pm_multi = _seed_care_support(_pm(tmp, "multi.db"))
        monkeypatch.setattr(
            expressions,
            "collect",
            lambda **kw: [
                {"id": "i:9", "day": "2026-09-05", "ts": "2026-09-05T10:00:00",
                 "text": "别来烦我，我想一个人静静。", "source": "interview",
                 "category": "访谈"},
            ],
        )
        res_multi = conflict_gap.scan_and_spawn_gaps(pm=pm_multi)
        assert res_multi["gaps_added"] == 1, (
            f"访谈里的反向信号必须能生 gap，实际 {res_multi}"
        )
    finally:
        shutil.rmtree(tmp)
    print("OK test_expression_source_catches_what_diary_misses")


def test_injected_diary_store_still_uses_old_path(monkeypatch):
    """显式注入 diary_store 时只扫它（兼容既有测试与不想多源的场景）。"""
    from deskmate.cognitive import expressions

    tmp = tempfile.mkdtemp()
    try:
        pm = _seed_care_support(_pm(tmp))

        def _boom(**kw):
            raise AssertionError("注入 diary_store 时不该走 expressions")

        monkeypatch.setattr(expressions, "collect", _boom)  # 若被调用即失败
        res = conflict_gap.scan_and_spawn_gaps(
            pm=pm,
            diary_store=_FakeDiary([{"id": "p1", "content": "别来烦我，一个人静静。"}]),
        )
        assert res["gaps_added"] == 1
    finally:
        shutil.rmtree(tmp)
    print("OK test_injected_diary_store_still_uses_old_path")


def test_normalize_ids_backfills_legacy_prefix():
    """存量的裸日记 id 要补 d:，否则会被当成新内容重扫一遍。"""
    got = conflict_gap._normalize_ids(["abc123", "d:xyz", "i:1", "", None])
    assert got == {"d:abc123", "d:xyz", "i:1"}, got


def test_max_scan_caps_volume(monkeypatch):
    """首跑面对 200+ 条存量时，一次只处理 max_scan 条，不爆量。"""
    from deskmate.cognitive import expressions

    tmp = tempfile.mkdtemp()
    try:
        pm = _seed_care_support(_pm(tmp))
        rows = [
            {"id": f"l:{i}", "day": "2026-09-05", "ts": f"2026-09-05T10:{i:02d}:00",
             "text": "别来烦我，我想一个人静静。", "source": "learned",
             "category": "preference"}
            for i in range(100)
        ]
        monkeypatch.setattr(expressions, "collect", lambda **kw: rows)
        res = conflict_gap.scan_and_spawn_gaps(pm=pm, max_scan=5)
        assert res["scanned"] == 5, f"单次扫描应被 max_scan 截断，实际 {res['scanned']}"
        assert res["gaps_added"] == 1, "同维度冲突只生成一个"
    finally:
        shutil.rmtree(tmp)
    print("OK test_max_scan_caps_volume")


if __name__ == "__main__":
    import contextlib
    import io
    for fn in [
        test_expression_source_catches_what_diary_misses,
        test_injected_diary_store_still_uses_old_path,
        test_normalize_ids_backfills_legacy_prefix,
        test_max_scan_caps_volume,
    ]:
        with contextlib.redirect_stdout(io.StringIO()):
            fn()
        print("OK", fn.__name__)
