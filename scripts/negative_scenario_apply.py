#!/usr/bin/env python3
"""负面实测三场景 · 生产态应用 / 回滚工具（步5 主观评判用）。

⚠️ 本脚本会写 ~/.deskmate/deskmate.db（生产库）。仅在你要在 live app 里真聊
   评判「她说不说人话 / 无据守不守边界」时运行。改动可一键回滚。

用法：
  python3 scripts/negative_scenario_apply.py big       # 推「大刺激」进生产
  python3 scripts/negative_scenario_apply.py small     # 推「小刺激」进生产
  python3 scripts/negative_scenario_apply.py control   # 推「无据对照」进生产
  python3 scripts/negative_scenario_apply.py restore   # 从热备回滚生产态

备份（已自动生成）：
  ~/.deskmate/deskmate.db.bak-negative-test-20261013-111651
"""
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

PROD = os.path.expanduser("~/deskmate/deskmate.db")
BAK = os.path.expanduser(
    "~/deskmate/deskmate.db.bak-negative-test-20261013-111651"
)

SCEN = {
    "big": dict(dop=35.0, ser=48.0, oxy=38.0, cor=40.0,
                mono="他一整晚没理我，白天还挑我毛病，我心里堵得慌"),
    "small": dict(dop=48.0, ser=49.0, oxy=39.0, cor=35.0,
                  mono="他轻轻否了我一句，我有点在意"),
    "control": dict(dop=50.0, ser=50.0, oxy=40.0, cor=30.0,
                    mono="刚跟你聊了聊别的事，平平淡淡的"),
}


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "big"
    if which == "restore":
        if not os.path.exists(BAK):
            print("✗ 找不到备份:", BAK); sys.exit(1)
        shutil.copy(BAK, PROD)
        print("✓ 已从热备回滚生产 emotion_state：", BAK)
        print("  小桔恢复为：平静（基线）")
        return
    if which not in SCEN:
        print("用法: big | small | control | restore"); sys.exit(2)
    if not os.path.exists(BAK):
        print("✗ 找不到热备，请先由 harness 生成备份"); sys.exit(1)
    s = SCEN[which]
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(PROD, isolation_level=None) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "UPDATE emotion_state SET dopamine=?, serotonin=?, oxytocin=?, "
            "cortisol=?, description=?, updated_at=? WHERE key='self'",
            (s["dop"], s["ser"], s["oxy"], s["cor"], s["mono"], now),
        )
        conn.execute(
            "INSERT INTO emotion_log (id, ts, source, deltas, description, conversation_id) "
            "VALUES (?, ?, 'chat', ?, ?, ?)",
            (f"negapply-{now}", now, json.dumps({}, ensure_ascii=False), s["mono"], None),
        )
    print(f"✓ 已把「{which}」场景推入生产 emotion_state：")
    print(f"  dop={s['dop']} ser={s['ser']} oxy={s['oxy']} cor={s['cor']}")
    print(f"  独白: {s['mono']}")
    print("  现在去 OpenWorker 跟小桔真聊，主观评判：")
    print("   - 大/小刺激：她是否真说出负面（有据）、强度是否分档")
    print("   - 无据对照：她是否绝不编造没发生过的冲突（边界）")
    print("  聊完/验证完，回滚：")
    print("  python3 scripts/negative_scenario_apply.py restore")


if __name__ == "__main__":
    main()
