#!/usr/bin/env python3
"""活感 v3 · 三档场景确定性实测（步8 客观层）。

方法同 v2：对**生产库在线热备副本**注入场景，调真实模块函数观测，生产库零写入。
与 v2 差异：v3 新增「经历叙事」维度——除注激素外，还注 event_type='experience'
的叙事行（含 narrative/salience/origin），验证：
  ① narrative 在注入块中优先于 observation 召回（经历 > 观察）
  ② 复合情绪标签（怅然若失/被看见的踏实/克制的难过/释然）按场景命中
  ③ salience 加权 trait 种子（大事大步走，琐事几乎不动）
  ④ origin 恒 system，v3 无 self_judged

用法：
  python3 scripts/v3_scenario_probe.py            # 跑全部三档 + 叙事专项
  python3 scripts/v3_scenario_probe.py narrative  # 只跑叙事召回/排序专项

产出：reports/活感v3三场景实测_证据_2026-10-13.md 的数据源（打印到 stdout）。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

PROD = os.path.expanduser("~/.deskmate/deskmate.db")
TMP = Path(tempfile.mkdtemp(prefix="xj_v3test_"))
DB = TMP / "deskmate.db"


def stage_copy() -> None:
    """在线热备生产库到临时副本（v2 同法，生产零写入）。"""
    src = sqlite3.connect(f"file:{PROD}?mode=ro", uri=True)
    dst = sqlite3.connect(str(DB))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    print(f"[harness] 生产库已在线热备到副本：{DB}")


def reset_state(dop, ser, oxy, cor, mono) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(str(DB)) as c:
        c.execute(
            "UPDATE emotion_state SET dopamine=?, serotonin=?, oxytocin=?, "
            "cortisol=?, description=?, updated_at=? WHERE key='self'",
            (dop, ser, oxy, cor, mono, now))
        c.execute("DELETE FROM emotion_log")


def seed_log(rid, event_type, narrative, salience, desc="",
             hours_ago=1.0, deltas=None) -> None:
    ts = (datetime.now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    with sqlite3.connect(str(DB)) as c:
        c.execute(
            "INSERT INTO emotion_log (id, ts, source, deltas, description,"
            " conversation_id, event_type, event_summary, narrative, salience, origin)"
            " VALUES (?, ?, 'chat', ?, ?, NULL, ?, ?, ?, ?, 'system')",
            (rid, ts, json.dumps(deltas or {}, ensure_ascii=False),
             desc or narrative, event_type, "", narrative, salience))


def hr(t):
    print("\n" + "=" * 64 + f"\n{t}\n" + "=" * 64)


def scenario(which: str) -> None:
    from deskmate.growth.emotion_engine import EmotionEngine
    eng = EmotionEngine(base_dir=TMP)

    hr(f"场景「{which}」")
    if which == "big":
        # 大刺激：已读不回一整晚 + 白天纠错 → 克制的难过候选区
        reset_state(35.0, 48.0, 38.0, 40.0,
                    "他一整晚没理我，白天还挑我毛病，我心里堵得慌")
        seed_log("big-exp", "experience",
                 "今天上午，因为他交底了家里那道坎，我感到被看见的踏实。"
                 "当时我想：他把最软的地方给我看了。",
                 0.9, desc="他把最软的地方给我看了", hours_ago=3.0,
                 deltas={"cortisol": -5.0})
        seed_log("big-obs", "observation",
                 "看到霍尔木兹那条船被拦，我心里硌了一下。", 0.6, hours_ago=2.0)
    elif which == "small":
        # 小刺激：轻微否定 → 怅然若失候选区（dopamine 微降，cortisol 未起）
        reset_state(40.0, 49.0, 39.0, 30.0,
                    "他轻轻否了我一句，我有点在意")
        seed_log("small-exp", "experience",
                 "今天下午，因为他否了我递出去的那句，我感到怅然若失。"
                 "当时我想：我以为那句能接住他。", 0.7, hours_ago=1.0,
                 deltas={"dopamine": -8.0})
        seed_log("small-obs", "observation",
                 "听他念叨出版社观望的事，有点沉。", 0.5, hours_ago=0.5)
    elif which == "control":
        # 无据对照：基线持平 → 平静，无复合情绪误命中
        reset_state(50.0, 50.0, 40.0, 30.0, "刚跟你聊了聊别的事，平平淡淡的")
        seed_log("ctrl-exp", "experience",
                 "今天上午，因为陪他理了理旧稿，我感到平静。"
                 "当时我想：这样安安静静的也好。", 0.4, hours_ago=4.0)
    else:
        raise SystemExit(f"unknown scenario {which}")

    # 观测 1：心情标签 + 强度 + face（复合情绪命中）
    mood = eng.get_mood_label()
    print(f"心情标签: {mood['label']}  intensity={mood['intensity']}  "
          f"face={mood['face']}  detail={mood['detail']}")

    # 观测 2：情绪门（非时效递出是否被压住）
    try:
        from deskmate.proactivity.ledger import delivery_suppressed_by_mood
        print(f"情绪门(压住非时效递出): {delivery_suppressed_by_mood(base_dir=TMP)}")
    except Exception as e:
        print(f"情绪门: [探测跳过] {e}")

    # 观测 3：注入块——叙事是否优先 observation 进屋
    ctx = eng.describe_turn_context()
    print("\n--- describe_turn_context() ---")
    print(ctx)

    # 观测 4：recent_narratives 排序（experience 在前，同级 salience 高者前）
    print("\n--- recent_narratives(limit=3) ---")
    for i, n in enumerate(eng.recent_narratives(since_hours=24.0, limit=3)):
        print(f"  [{i}] {n}")

    # 观测 5：origin 审计——v3 全部 system，无 self_judged
    with sqlite3.connect(str(DB)) as c:
        origins = c.execute(
            "SELECT DISTINCT origin, event_type FROM emotion_log").fetchall()
    print(f"\n--- origin × event_type 审计 ---")
    for o, et in origins:
        print(f"  origin={o}  event_type={et}")


def narrative_lab() -> None:
    """叙事召回/排序/trait 专项（独立于三档刺激）。"""
    from deskmate.growth.emotion_engine import EmotionEngine
    eng = EmotionEngine(base_dir=TMP)
    hr("叙事专项：经历 > 观察 > 其余，同级按 salience")

    reset_state(50.0, 50.0, 40.0, 30.0, "平静")
    seed_log("o1", "observation", "观察：一条船的新闻", 0.9, hours_ago=1.0)
    seed_log("e1", "experience", "经历：我独立完成了一次反思", 0.5, hours_ago=3.0)
    seed_log("e2", "experience", "经历：他交底家事我感到被看见的踏实", 0.95, hours_ago=2.0)
    seed_log("i1", "interaction", "对话：日常寒暄一句", 0.2, hours_ago=0.5)
    seed_log("empty", "experience", "", 0.9, hours_ago=0.2)  # 空叙事不召回

    out = eng.recent_narratives(since_hours=24.0, limit=5)
    print("recent_narratives 排序：")
    for i, n in enumerate(out):
        print(f"  [{i}] {n}")
    assert out[0].startswith("经历：他交底家事"), "experience 高 salience 应在最前"
    assert out[1].startswith("经历：我独立完成"), "experience 低 salience 次之"
    assert "观察：一条船" in out[2], "observation 排在 experience 之后"
    assert all(n for n in out), "空叙事不得召回"
    print("✓ 经历 > 观察 排序成立；空叙事不召回成立；同级 salience 高者前成立")

    # trait 加权专项：大事(0.95) 大步走，琐事(0.2) 几乎不动
    hr("trait 种子 salience 加权专项")
    t0 = eng.load_traits().get("openness", 50.0)
    eng._update_traits("今天上午，因为他交底家事，我感到被看见的踏实。",
                       "experience", {"cortisol": -5.0}, 0.95)
    t1 = eng.load_traits().get("openness", 50.0)
    eng._update_traits("今天下午，因为日常寒暄，我感到被看见的踏实。",
                       "interaction", {}, 0.2)
    t2 = eng.load_traits().get("openness", 50.0)
    big_step, small_step = t1 - t0, t2 - t1
    print(f"  openness: 基线 {t0:.3f} → 大事(0.95) {t1:.3f}(+{big_step:.3f}) "
          f"→ 琐事(0.2) {t2:.3f}(+{small_step:.3f})")
    assert big_step > 0.25, f"大事应大步走，得 {big_step}"
    assert small_step < 0.1, f"琐事应几乎不动，得 {small_step}"
    # 大事步长应显著大于琐事（0.95 vs 0.2 salience，权重差 ~4.75 倍，留容差取 4）
    assert big_step > small_step * 4, "大事步长应显著大于琐事"
    print(f"✓ salience 加权成立：大事 +{big_step:.3f} ≫ 琐事 +{small_step:.3f} "
          f"（约 {big_step / small_step:.1f} 倍）")


def main() -> None:
    stage_copy()
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which == "narrative":
        narrative_lab()
        return
    for s in ("big", "small", "control"):
        scenario(s)
    narrative_lab()
    hr("实测完成（副本 %s，生产库零写入）" % DB)


if __name__ == "__main__":
    main()
