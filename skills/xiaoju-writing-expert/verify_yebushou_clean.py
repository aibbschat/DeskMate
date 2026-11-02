"""验证《夜不收》正确版构思的连续性 (对应 walkthrough_yebushou_outline.md)。

与 demo_yebushou.py 的区别: 那份故意植入 V3 旧 bug(权世宇活到十六章)以证明内核能抓错;
这份用修订后的正确构思播种——权世宇只死在第九章, 第十六章只有他的遗物腰牌,
没有"他本人"再行动——证明内核对好材料给出 0 错误, 不误报。
"""

from writing_kernel import CanonLedger, Fact, Promise, Foreshadow, ChapterEvent, ContinuityChecker

l = CanonLedger()

# ---- 正典事实 ----
l.add_fact(Fact("quanshiyu_dead", "权世宇于第九章铜雀渡江滩中箭身亡",
                status="dead", chapter=9, known_by=["ningjing", "chenchong", "kongdagui", "fangyexing"],
                source_ref="夜不收V4·第九章"))
l.add_fact(Fact("wang_in_longshan", "李昖被囚于龙山仓(第十章囚车进仓院证实)",
                status="alive", chapter=10, known_by=["ningjing", "chenchong", "kongdagui", "fangyexing"],
                source_ref="夜不收V4·第十章"))
l.add_fact(Fact("meiji_leak", "梅记册勾出军中铁齿, 与明营焚卷半收(不写破)",
                status="resolved", chapter=16, known_by=["ningjing", "kongdagui"],
                source_ref="夜不收V4·第十六章"))

# ---- 伏笔账 (作家笔记面貌) ----
l.add_foreshadow(Foreshadow("meiji", "梅记册勾出军中铁齿, 与明营焚卷半收(不写破)",
                            planted_chapter=9, expected_payoff_chapter=16, status="paid"))
l.add_foreshadow(Foreshadow("signal", "三枚信号弹, 第九章交弹, 第十六章最后一枚",
                            planted_chapter=9, expected_payoff_chapter=16, status="paid"))
l.add_foreshadow(Foreshadow("taixue", "踏雪寻梅, 前半部跟到柳旗主, 第十三章反用识暗桩",
                            planted_chapter=3, expected_payoff_chapter=13, status="paid"))
l.add_foreshadow(Foreshadow("waistplate", "权世宇腰牌, 第九章遗物, 第十六章掷出假冒南旗号令",
                            planted_chapter=9, expected_payoff_chapter=16, status="paid"))

# ---- 事件 (注意: 第十六章是"腰牌"被掷出, 不是权世宇本人) ----
l.add_event(ChapterEvent(9, "quanshiyu", "铜雀渡江滩中箭身亡", []))
l.add_event(ChapterEvent(16, "ningjing", "掷出权世宇的捕盗厅腰牌, 假冒南旗号令, 中箭", ["quanshiyu_dead"]))
l.add_event(ChapterEvent(14, "fangyexing", "南瓮城坐桥心砍桥桩, 与桥同塌冰水阵亡", []))

# ---- 跑校验 ----
chk = ContinuityChecker(l)
errs, warns = chk.run()

print("=" * 60)
print("小桔连续性内核 · 《夜不收》正确版构思校验")
print("=" * 60)
print(f"正典事实 {len(l.facts)} 条 | 伏笔 {len(l.foreshadows)} 条 | 事件 {len(l.events)} 条")
print("--- 作家笔记面貌的伏笔账 ---")
print(l.writer_notes())
print("--- 错误(必须在读者之前逮住) ---")
for e in errs:
    print(f"  ✗ {e}")
print("--- 警告 ---")
for w in warns:
    print(f"  ! {w}")
print(f"\n结论: {len(errs)} 个硬冲突, {len(warns)} 个待办。")

assert errs == [], f"正确构思不应有硬冲突, 但抓到: {errs}"
print("内核确认: 正确版构思 0 硬冲突, V4 的旧 bug 已被根除 ✓")
