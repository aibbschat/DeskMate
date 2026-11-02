#!/usr/bin/env python3
"""deai_lint — 去 AI 味·确定性校验器 (与 writing_kernel 同级硬门)

写作专家产出的每一段文字, 在交付前必须通过本 linter。
它不负责"写出好文字", 只负责"逮住机器味"——和 continuity kernel
逮住"死而复生"一样, 是确定性门禁: 命中即不过, 逼生成环节自己去改,
而不是靠当次写手(人或模型)的临场手感。

检测类别:
  T1 空泛套话 / 机械总结词
  T2 手艺分析师腔 / 自我标注技法 (作家给自己贴技法标签)
  T3 正文元注释 (解说技能本身而非故事, 如 总闸/路由/内核记账)
  T4 清单式残留 (V4 病根: 写法提示/人物结局表/倒计时链/长 bullet 串)
  T4B 工程措辞残留 (V4 字眼级: 差额/只说一个字/，阵亡。)
  T5 太硬 / 句式过均 (句长方差过低, 连续句子几乎一样长)

纯标准库, 无外部依赖, 与 writing_kernel.py 同构。
"""

from __future__ import annotations

import re
import sys

# ---------------------------------------------------------------------------
# 命中规则
# ---------------------------------------------------------------------------

# T1 空泛套话 / 机械总结词
T1_PHRASES = [
    "在当今", "随着", "值得注意的是", "总而言之", "综上所述", "毋庸置疑",
    "显而易见", "首先，", "其次，", "最后，", "一方面", "另一方面",
    "让我们", "我们需要", "所谓", "又称", "是指", "在……背景下",
]

# T2 手艺分析师腔 / 自我标注技法 (作家不该在大纲正文里给自己贴这些标签)
T2_PHRASES = [
    "发动机", "错峰兑付", "渡水三法", "黑色幽默配额", "叙事功能", "戏剧功能",
    "结构骨架", "人物弧光", "期待线", "兑付章节", "这是全书最大的反转",
    "这是全书最大的", "本章的戏剧功能", "本章的叙事功能",
]

# T3 正文元注释 (解说技能/路由本身, 而非讲故事)
T3_PHRASES = [
    "总闸", "路由到", "路由表", "内核记账", "交给内核", "writing_kernel",
    "本技能", "本专家", "本原型",
]

# T4 清单式残留 (V4 病根关键词)
T4_PHRASES = [
    "写法提示", "人物结局表", "倒计时链", "新地图一览", "伏笔账表", "三幕结构",
]

# T4B 工程文档残留字眼 (V4 病根·字眼级)
# 这些不是"清单", 是 V4 工程文档/人物结局表里特有的措辞, 一出现就露馅
# (财会词 + 极简摘要腔)。萧哥明确: 这类"刻意短打"本身就是 AI/工程味, 必须逮。
T4B_PHRASES = [
    "差额",            # 财会/工程词, V4 文案口吻 ("这一天的差额，得拿命填")
    "只说一个字",       # 人物结局表式极简摘要腔 ("孔大贵只说一个字：追")
    "，阵亡。",         # 「方也行，阵亡。」式结局表条目
    "阵亡。",           # 同上, 独立成句
]

# T5 太硬 / 句式过均 参数
# "太硬"=通篇齐整无起伏, 含刻意短打串(如 V4「人物结局表」式
# 「方也行，阵亡。陈冲，阵亡。」全是同长句)也一并算。短句本身不罚,
# 只在"通篇/连续齐整"时罚。注意: 个别短打若带 V4 措辞, 由 T4B 单独抓。
T5_MIN_SENTENCES = 6
T5_MIN_CV = 0.28          # 句长变异系数低于此 => 过均(太硬)
T5_RUN_LEN = 4            # 连续 N 句长度互相差 <15% => 过均


def _sentences(text: str) -> list[str]:
    """按句号/叹号/问号/换行切句, 只留"散文句"。

    剔除: 空白、纯标点/标记(markdown ---、引号碎片)、过短(<4字)句。
    保留: 真短散文、以及 V4 式齐整阵亡串(>=4字, 如「方也行，阵亡。」),
          这样 T5 既不会误杀真文字, 也不放过点 V4 残留。
    """
    out: list[str] = []
    for p in re.split(r"[。！？\n]", text):
        s = p.strip().strip('"\'`')
        if len(s) < 4:
            continue
        if not re.search(r"[\u4e00-\u9fa5A-Za-z0-9]", s):
            continue
        out.append(s)
    return out


def lint(text: str) -> tuple[list[str], list[str]]:
    """返回 (errors, warnings)。errors 为硬失败, 必须清零方可交付。"""
    errors: list[str] = []

    # ---- T1 ~ T4B: 短语命中 ----
    for tag, table in (
        ("T1套话", T1_PHRASES),
        ("T2分析师腔", T2_PHRASES),
        ("T3元注释", T3_PHRASES),
        ("T4清单残留", T4_PHRASES),
        ("T4B工程措辞残留", T4B_PHRASES),
    ):
        for ph in table:
            if ph in text:
                errors.append(f"[{tag}] 命中机器味短语: 「{ph}」")

    # ---- T4 补充: 长 bullet 串 (清单式) ----
    bullet_lines = [ln for ln in text.splitlines() if re.match(r"^\s*([-\u2022*·]|\d+[.、])\s", ln)]
    if len(bullet_lines) >= 4:
        errors.append(f"[T4清单残留] 检测到 {len(bullet_lines)} 行条列式内容, 疑似清单骨架")

    # ---- T5: 句式过均 (太硬) ----
    sents = _sentences(text)
    if len(sents) >= T5_MIN_SENTENCES:
        lens = [len(re.sub(r"\s", "", s)) for s in sents]
        mean = sum(lens) / len(lens)
        if mean > 0:
            var = sum((x - mean) ** 2 for x in lens) / len(lens)
            cv = (var ** 0.5) / mean
            if cv < T5_MIN_CV:
                errors.append(
                    f"[T5太硬] 句长变异系数 {cv:.2f} < {T5_MIN_CV}, "
                    f"句式过于均匀(平均 {mean:.0f} 字/句), 读感发硬"
                )
            # 连续近等长句
            run = 1
            for i in range(1, len(lens)):
                if mean > 0 and abs(lens[i] - lens[i - 1]) / mean < 0.15:
                    run += 1
                    if run >= T5_RUN_LEN:
                        errors.append(
                            f"[T5太硬] 连续 {run} 句长度几乎一致(±15%), 缺长短错落"
                        )
                        break
                else:
                    run = 1

    return errors, []


if __name__ == "__main__":
    # ---- 自测1: 污染样本必须被逮住 ----
    bad = (
        "在当今网络文学蓬勃发展的背景下, 值得注意的是, 本章的戏剧功能是铺垫。\n"
        "首先，要明确目标；其次，要拆解任务；最后，完成交付。\n"
        "这是全书最大的反转, 也是后半部的发动机。\n"
        "结构骨架要保持稳定。\n"
        "伏笔账表如下: 一、二、三。\n"
        "这个人死了。那个人也死了。第三个也死了。第四个也没了。第五个倒下。第六个没了。\n"
        "这一天的差额，得拿命填。\n"
        "孔大贵只说一个字：追。\n"
        "方也行，阵亡。陈冲，阵亡。\n"
    )
    e1, _ = lint(bad)
    assert e1, "自测1应逮住机器味"
    print(f"自测1(污染样本): 逮住 {len(e1)} 处机器味 ✓")
    for x in e1:
        print("   ✗", x)

    # ---- 自测2: 干净样本应 0 错误 ----
    good = (
        "第九章我想写得干脆些。那一刀真劈下去了，刀锋偏了，权世宇没死在刀下，\n"
        "却死在随后赶来的乱箭里。他临死才明白，自己从昨夜起就被人写好了结局。\n"
        "所以这一章，我想让四个人第一次看清这个骗局。\n"
        "腰牌后头有大用，先埋着。\n"
        "四人夜渡汉江，江心大雾，对岸火把连营。孔大贵把信号弹按进宁靖手里。\n"
        "三枚弹，四个人，此后再没有第四枚了。\n"
    )
    e2, _ = lint(good)
    assert e2 == [], f"自测2不应有错误, 但抓到: {e2}"
    print(f"自测2(干净样本): 0 错误 ✓")

    # ---- 若给了文件路径, 顺带 lint 实际大纲 ----
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as fh:
            content = fh.read()
        errs, _ = lint(content)
        print(f"\n文件校验 {sys.argv[1]}: {len(errs)} 处机器味")
        for x in errs:
            print("   ✗", x)
        if not errs:
            print("   → 通过 de-AI 门 ✓")
