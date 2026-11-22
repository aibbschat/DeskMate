#!/usr/bin/env python3
"""上下文总预算 A/B 测量脚本（离线，不调用 LLM，不花 token）。

用途：给一组出站消息（从 trace / 会话导出），对比开启总预算前后能砍多少、
核心（人格/原文/两端 user）是否完好。帮助判断"砍上下文"这条路值不值得走。

用法：
  python scripts/context_budget_ab.py <messages.json>

  messages.json 是 JSON 数组，形如 [{"role":"user","content":"..."}, ...]。
  不提供文件时用一个内置的"读四章原文理路线"样例演示。

输出：各路字符构成 + 3 档预算（OFF/宽松/激进）下的裁剪结果与核心校验。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coworker import context_window_budget as cwb  # noqa: E402

_SAMPLE = None


def _sample_messages():
    global _SAMPLE
    if _SAMPLE is not None:
        return _SAMPLE
    msgs = [{"role": "system", "content": "你是小桔。" + "人格与纪律底座。" * 300}]
    msgs.append(
        {
            "role": "user",
            "content": "帮我理归营路线：铜雀-渡口村-翻过山后-高村，怕跟夜不收来路冲突",
        }
    )
    for name, size in [
        ("第一章铜雀渡口", 3000),
        ("第二章翻山", 3500),
        ("第三章高村扎营", 2800),
        ("第四章夜不收来路", 3200),
    ]:
        msgs.append({"role": "assistant", "content": f"我先读{name}这一章确认路线……"})
        msgs.append({"role": "tool", "content": f"【{name}原文】" + "正文内容" * (size // 4)})
        msgs.append({"role": "assistant", "content": f"读完{name}，记录下这个点，继续下一章比对。"})
    msgs.append({"role": "user", "content": "综合各章，归营路线到底对不对？"})
    msgs.append({"role": "assistant", "content": "让我把四章的路线点都摆出来逐一核对……"})
    msgs.append({"role": "user", "content": "现在给最终结论"})
    _SAMPLE = msgs
    return msgs


def _core_intact(original, trimmed) -> bool:
    def texts(msgs, role):
        return [m.get("content") for m in msgs if isinstance(m, dict) and m.get("role") == role]

    return (
        trimmed[0].get("content") == original[0].get("content")
        and texts(original, "tool") == texts(trimmed, "tool")
        and trimmed[-1].get("content") == original[-1].get("content")
    )


def main():
    if len(sys.argv) > 1:
        data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            print("错误：messages.json 顶层必须是数组", file=sys.stderr)
            return 2
        msgs = data
        source = sys.argv[1]
    else:
        msgs = _sample_messages()
        source = "内置样例（读四章原文理路线）"

    print(f"消息源：{source}")
    print("构成：" + json.dumps(cwb.measure(msgs), ensure_ascii=False))
    print()
    total = cwb.measure(msgs)["total"]
    budgets = [(0, "关闭"), (int(total * 0.85), "宽松 -15%"), (int(total * 0.7), "激进 -30%")]
    for budget, label in budgets:
        os.environ["COWORKER_CONTEXT_TOTAL_BUDGET"] = str(budget)
        os.environ.pop("COWORKER_CONTEXT_TRIM_TOOL_RESULTS", None)
        out, report = cwb.apply(msgs)
        intact = _core_intact(msgs, out)
        saved = report["before"] - report["after"]
        print(
            f"[{label:10}] budget={budget or 'OFF':>6}  "
            f"{report['before']:>7} -> {report['after']:>7}  "
            f"省 {saved:>6} 字 ({saved * 100 // max(1, report['before']):>2}%)  "
            f"裁轮次={report['removed_turns']:>2}  "
            f"触底线={str(report['at_exempt_floor']):>5}  "
            f"核心完好={'是' if intact else '否'}"
        )
    print()
    print("提示：若『激进』仍几乎省不下、或核心不完好，说明砍上下文这条路空间极小，")
    print("      瓶颈在往返次数/缓存时效——见 docs/上下文总预算实验报告_2026-11-20.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
