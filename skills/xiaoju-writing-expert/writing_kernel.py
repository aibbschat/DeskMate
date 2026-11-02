"""小桔写作专家 · 连续性内核 (writing_kernel)

借鉴开源方法论的内核:
  - long-novel-writer 的正典账本(canon ledger) / POV 知识边界(known_by) / 六层摘要冻结
  - story-skills 的"编译器式连续性": 把生死/承诺/伏笔/知识状态写进可校验结构,
    像编译器查类型错误一样抓矛盾, 在读者之前逮住。

设计原则(见《小桔写作专家·设计草案》):
  - 借其骨、还其魂: 机制是确定性的, 但对外以"作家笔记"面貌呈现, 不是表格清单。
  - 纯标准库, 无外部依赖, 可在本机任意环境运行。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class Fact:
    """正典事实。status 一旦确立即冻结, 不再被后续章节覆盖。"""
    id: str
    text: str
    status: str = "open"            # alive | dead | open | resolved
    chapter: Optional[int] = None   # 该状态确立的章
    known_by: list[str] = field(default_factory=list)  # POV 知识边界
    source_ref: str = ""


@dataclass
class Promise:
    """角色承诺/誓言, 带截止章。"""
    id: str
    text: str
    made_by: str
    due_chapter: Optional[int] = None
    status: str = "open"            # open | paid | broken


@dataclass
class Foreshadow:
    """伏笔账(作家笔记面貌): 埋在哪一章, 应在哪一章收, 收了没有。"""
    id: str
    text: str
    planted_chapter: int
    expected_payoff_chapter: Optional[int] = None
    status: str = "open"            # open | paid | abandoned


@dataclass
class ChapterEvent:
    """章节事件, 用于死亡/POV/桥段校验。"""
    chapter: int
    character: str
    action: str
    info_refs: list[str] = field(default_factory=list)  # 引用了哪些 fact id


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------
class CanonLedger:
    def __init__(self) -> None:
        self.facts: dict[str, Fact] = {}
        self.promises: dict[str, Promise] = {}
        self.foreshadows: dict[str, Foreshadow] = {}
        self.events: list[ChapterEvent] = []
        self.layers: dict[int, str] = {}   # L0..L5 摘要冻结

    def add_fact(self, f: Fact) -> None:
        self.facts[f.id] = f

    def add_promise(self, p: Promise) -> None:
        self.promises[p.id] = p

    def add_foreshadow(self, fs: Foreshadow) -> None:
        self.foreshadows[fs.id] = fs

    def add_event(self, e: ChapterEvent) -> None:
        self.events.append(e)

    def freeze_layer(self, level: int, summary: str) -> None:
        """六层摘要冻结: 写定即冻结, 不再被后续章节覆盖。"""
        self.layers[level] = summary

    # ---- 作家笔记面貌的渲染(不用表格清单) ----
    def writer_notes(self) -> str:
        lines = ["【伏笔账·作家笔记】"]
        for fs in self.foreshadows.values():
            if fs.status == "paid":
                mark = f"第{fs.planted_chapter}章埋 → 第{fs.expected_payoff_chapter}章收 ✓"
            elif fs.status == "open":
                mark = f"第{fs.planted_chapter}章埋 → 应在第{fs.expected_payoff_chapter}章收 (悬着)"
            else:
                mark = f"第{fs.planted_chapter}章埋 → 弃用"
            lines.append(f"  · {fs.text} —— {mark}")
        lines.append("")
        lines.append("【死序·谁已不在】")
        for f in self.facts.values():
            if f.status == "dead":
                lines.append(f"  · {f.id}: 第{f.chapter}章 ({f.source_ref})")
        return "\n".join(lines)

    # ---- 持久化(可选): 落本机 jsonl, 跨会话保鲜 ----
    def persist(self, base: str) -> None:
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "facts.jsonl"), "w", encoding="utf-8") as fh:
            for f in self.facts.values():
                fh.write(json.dumps(asdict(f), ensure_ascii=False) + "\n")
        with open(os.path.join(base, "foreshadow.jsonl"), "w", encoding="utf-8") as fh:
            for fs in self.foreshadows.values():
                fh.write(json.dumps(asdict(fs), ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 连续性校验器 (确定性, 类编译器)
# ---------------------------------------------------------------------------
class ContinuityChecker:
    def __init__(self, ledger: CanonLedger) -> None:
        self.l = ledger
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def run(self) -> tuple[list[str], list[str]]:
        self.errors.clear()
        self.warnings.clear()
        self._dead_reappear()
        self._payoff_before_setup()
        self._pov_before_known()
        self._foreshadow_align()
        return self.errors, self.warnings

    def _dead_reappear(self) -> None:
        """死人在后续章节又行动 = 最常见的吃书。"""
        for f in self.l.facts.values():
            if f.status == "dead" and f.chapter is not None:
                for ev in self.l.events:
                    if ev.chapter > f.chapter and ev.character == f.id:
                        self.errors.append(
                            f"[死而复生] {f.id} 于第{f.chapter}章死亡, "
                            f"但第{ev.chapter}章仍有其行动: {ev.action}"
                        )

    def _payoff_before_setup(self) -> None:
        """钩子回收早于埋设 = 时序颠倒。"""
        for fs in self.l.foreshadows.values():
            if fs.status == "paid" and fs.expected_payoff_chapter is not None:
                if fs.expected_payoff_chapter < fs.planted_chapter:
                    self.errors.append(
                        f"[钩子早于埋设] 伏笔 {fs.id} 埋于第{fs.planted_chapter}章, "
                        f"却在第{fs.expected_payoff_chapter}章回收"
                    )

    def _pov_before_known(self) -> None:
        """非知情角色过早使用秘密 = POV 越界(OOC 高发区)。"""
        for ev in self.l.events:
            for ref in ev.info_refs:
                f = self.l.facts.get(ref)
                if f and f.known_by and ev.character not in f.known_by:
                    self.warnings.append(
                        f"[POV越界] 第{ev.chapter}章 {ev.character} 用了 {ref} 的信息, "
                        f"但其 known_by={f.known_by}"
                    )

    def _foreshadow_align(self) -> None:
        """伏笔到了该收的章还没收 = 悬空。"""
        for fs in self.l.foreshadows.values():
            if fs.status == "open" and fs.expected_payoff_chapter is not None:
                self.warnings.append(
                    f"[伏笔悬空] {fs.id} 埋于第{fs.planted_chapter}章, "
                    f"应在第{fs.expected_payoff_chapter}章回收, 至今未收"
                )


# ---------------------------------------------------------------------------
# POV 知识边界 (按角色加载"此刻已知")
# ---------------------------------------------------------------------------
def known_to(ledger: CanonLedger, character: str, chapter: int) -> list[str]:
    out = []
    for fid, f in ledger.facts.items():
        if character in f.known_by and (f.chapter is None or f.chapter <= chapter):
            out.append(fid)
    return out


def load_ledger_from_json(path: str) -> CanonLedger:
    """从 JSON 账本加载 (供 CLI / 对话门禁调用)。

    格式:
    {
      "facts":       [{"id","text","status","chapter","known_by":[],"source_ref"}],
      "promises":    [{"id","text","made_by","due_chapter","status"}],
      "foreshadows": [{"id","text","planted_chapter","expected_payoff_chapter","status"}],
      "events":      [{"chapter","character","action","info_refs":[]}],
      "layers":      {"0": "摘要...", "5": "..."}
    }
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    lg = CanonLedger()
    for d in data.get("facts", []):
        lg.add_fact(Fact(**d))
    for d in data.get("promises", []):
        lg.add_promise(Promise(**d))
    for d in data.get("foreshadows", []):
        lg.add_foreshadow(Foreshadow(**d))
    for d in data.get("events", []):
        lg.add_event(ChapterEvent(**d))
    for lvl_s, sm in data.get("layers", {}).items():
        lg.freeze_layer(int(lvl_s), sm)
    return lg


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "check":
        # 门禁模式: python writing_kernel.py check <ledger.json>
        lg = load_ledger_from_json(sys.argv[2])
        chk = ContinuityChecker(lg)
        errs, warns = chk.run()
        print("【连续性校验】")
        if errs:
            for e in errs:
                print("  ✗", e)
        else:
            print("  ✓ 0 硬冲突")
        for w in warns:
            print("  ⚠", w)
        if errs:
            sys.exit(1)
    else:
        # 自测: 构造最小账本并跑校验
        lg = CanonLedger()
        lg.add_fact(Fact("a", "测试人物甲死于第三章", status="dead", chapter=3,
                         known_by=["乙"], source_ref="test"))
        lg.add_event(ChapterEvent(5, "a", "又出现了", []))
        chk = ContinuityChecker(lg)
        errs, warns = chk.run()
        print("自测错误:", errs)
        assert errs, "自测应抓到死而复生"
        print("内核自测通过 ✓")
