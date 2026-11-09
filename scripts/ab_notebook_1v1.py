#!/usr/bin/env python3
"""同题 A/B：有 / 无笔记本（仅 1v1，手动触发）。

用法（真实模型）：
    .venv/bin/python scripts/ab_notebook_1v1.py \
        --expert brainstormer --book 夜不收 \
        --question "后半部这三个人一个都不能死，你觉得怎么走？" \
        --out reports/ab-notebook-brainstormer.md

离线自检：
    .venv/bin/python scripts/ab_notebook_1v1.py --scripted ...
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deskmate.bots.abtest import run_ab  # noqa: E402


class _ScriptedProvider:
    """离线演示：A 组答 A，B 组答 B，证明两条链路真的不同。"""

    def capabilities(self, model: str):  # noqa: ANN001
        from coworker.providers.base import ModelCapabilities

        return ModelCapabilities()

    def complete(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import AssistantTurn

        system = str(messages[0].get("content", "")) if messages else ""
        return AssistantTurn(text=("B 组：我按笔记本里的旧判断来" if "笔记本摘要" in system else "A 组：我只看正文"))

    def stream(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import StreamChunk

        yield StreamChunk(turn=self.complete(model=model, messages=messages, tools=tools, **settings))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expert", default="brainstormer")
    ap.add_argument("--book", default="")
    ap.add_argument("--question", required=True)
    ap.add_argument("--scripted", action="store_true", help="不发真实请求，用固定回复演示")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if args.scripted:
        provider, model, source = _ScriptedProvider(), "scripted", "scripted"
    else:
        from deskmate.bots.runtime import resolve_default_provider_model

        provider, model, source = resolve_default_provider_model()
        if provider is None:
            print("拿不到可用模型通道；可以用 --scripted 先看链路。", file=sys.stderr)
            return 2

    result = asyncio.run(
        run_ab(
            expert=args.expert,
            book=args.book,
            question=args.question,
            provider=provider,
            model=model,
        )
    )
    result["model"] = model
    result["model_source"] = source
    result["ran_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# 同题 A/B · {args.expert} · 《{args.book or '未挂书'}》",
            "",
            f"- 问题：{args.question}",
            f"- 模型：{model}（{source}）",
            f"- 笔记本摘要：{result['digest_chars']} 字",
            "",
            "## A. 无笔记本",
            "",
            result["without_notebook"] or "（空）",
            "",
            "## B. 有笔记本",
            "",
            result["with_notebook"] or "（空）",
            "",
        ]
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"\n报告已写入：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
