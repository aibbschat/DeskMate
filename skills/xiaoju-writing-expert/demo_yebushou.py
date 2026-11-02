"""演示: 用《夜不收》后半部(V4) 播种连续性内核, 并故意植入 V3 旧 bug,
证明内核能在读者之前逮住"死而复生"这类吃书矛盾。

运行: python3 demo_yebushou.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from writing_kernel import (
    CanonLedger,
    ContinuityChecker,
    Fact,
    Promise,
    Foreshadow,
    ChapterEvent,
)


def build_yebushou() -> CanonLedger:
    l = CanonLedger()

    # ---- 正典账本: 事实(写定即冻结) ----
    l.add_fact(Fact(
        "quanshiyu", "权世宇, 明军夜不收同伴, 第九章铜雀渡中箭死于灭口乱箭",
        status="dead", chapter=9,
        known_by=["宁靖", "陈冲", "孔大贵", "方也行"],
        source_ref="夜不收V4·第九章",
    ))
    l.add_fact(Fact(
        "liyi", "朝鲜国王李昖, 被倭军囚于龙山仓, 后救出交还明军",
        status="alive", chapter=12,
        known_by=["宁靖", "陈冲", "孔大贵", "方也行", "倭军"],
        source_ref="夜不收V4·第十章",
    ))
    l.add_fact(Fact(
        "meiji", "权世宇遗物'梅记'册, 记明军动向买卖笔笔对得上泄密夜",
        status="open", chapter=9,
        known_by=["宁靖", "陈冲"],
        source_ref="夜不收V4·第九章",
    ))

    # ---- 承诺 ----
    l.add_promise(Promise(
        "kong_refuse", "孔大贵: 火照举, 王照救, 夜不收自己收账不带李军门功",
        made_by="孔大贵", due_chapter=11, status="paid",
    ))

    # ---- 伏笔账(作家笔记面貌) ----
    l.add_foreshadow(Foreshadow(
        "meiji_thread", "梅记册勾出军中铁齿, 与明营焚卷半收(不写破)",
        planted_chapter=9, expected_payoff_chapter=16, status="paid"))
    l.add_foreshadow(Foreshadow(
        "signal3", "三枚信号弹, 第九章交弹, 第十六章最后一枚",
        planted_chapter=9, expected_payoff_chapter=16, status="paid"))
    l.add_foreshadow(Foreshadow(
        "taixue", "踏雪寻梅, 前半部跟到柳旗主, 第十三章反用识暗桩",
        planted_chapter=3, expected_payoff_chapter=13, status="paid"))
    l.add_foreshadow(Foreshadow(
        "yaopai", "权世宇腰牌, 第九章遗物, 第十六章掷出假冒南旗号令",
        planted_chapter=9, expected_payoff_chapter=16, status="paid"))

    # ---- 章节事件 ----
    l.add_event(ChapterEvent(9, "quanshiyu", "中箭死于铜雀渡江滩", []))
    l.add_event(ChapterEvent(16, "ningjing", "掷出权世宇腰牌假冒南旗号令", ["yaopai"]))
    # === 故意植入 V3 旧 bug: 权世宇在第十六章"现身" ===
    l.add_event(ChapterEvent(16, "quanshiyu", "坡上现身为他本人, 替四人挡最后半刻", []))

    # 六层摘要冻结示意
    l.freeze_layer(5, "全书: 烧仓与救王双线, 四人之死, 两令皆成")
    l.freeze_layer(4, "后半部八章: 敌营→孤城→归路")
    return l


if __name__ == "__main__":
    l = build_yebushou()
    chk = ContinuityChecker(l)
    errors, warnings = chk.run()

    print("=" * 60)
    print("小桔连续性内核 · 《夜不收》演示")
    print("=" * 60)
    print(f"正典事实 {len(l.facts)} 条 | 伏笔 {len(l.foreshadows)} 条 | 事件 {len(l.events)} 条")
    print("\n--- 作家笔记面貌的伏笔账 ---")
    print(l.writer_notes())
    print("\n--- 错误(必须在读者之前逮住) ---")
    for e in errors:
        print("  ✗", e)
    print("\n--- 警告 ---")
    for w in warnings:
        print("  !", w)
    print(f"\n结论: {len(errors)} 个硬冲突, {len(warnings)} 个待办。")
    if errors:
        print("内核生效: V3 旧 bug(权世宇活到十六章)被确定性捕获 ✓")
