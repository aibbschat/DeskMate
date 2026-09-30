#!/usr/bin/env python3
"""清理「内部·萧盛」溯源标里的冒牌条目（2026-09-30 修复配套）。

背景
----
写入侧（`coworker/engine.py::_xiaosheng_note`、`deskmate/memory/manager.py::post_session_summary`）
曾把自动化跑的 web_search 检索转储、自动化会话提示词也顶着「（内部·萧盛）」标写进理解池，
导致约 63% 的「内部·萧盛」条目是噪音（含成人内容），污染世界观回望与对话上下文
（对话「她对世界的理解」段不按溯源标过滤，会直接把转储当小桔的理解喂给模型）。

本脚本对**已存在**的脏数据做一次性清理，措辞保守、可逆：
- 理解池 `agency/observation_understanding.jsonl`：命中噪音判定的条目**直接移出**活跃池
  （它们不是有效「理解」——是原始检索转储 / 自动化提示词），原样进待审区备份。
  移除而非摘标：摘标后它们仍以「外部观察」被对话「理解」段注入，污染照旧。
- 原始素材 `agency/xiaosheng_observations.jsonl`：**只保守移除 unambiguously 自动化的会话
  提示词**（首句「⏰ Scheduled run」/ 含「This automation is due now」）。绝不按
  「检索到以下资料」删源文件——因为萧盛本人让你查资料时 web_search 摘要也是这格式，
  按内容无法区分，误删会丢真·萧盛的研究。源文件本就是 inert（已记过 ingested，不会重流），
  清理它只为防 ingestion 状态重置后重流，故只动 unambiguously 自动化的那部分。

安全措施
------
- 改动前先备份原文件为 `.bak`（已存在则不覆盖）。
- 所有改写走临时文件 + os.replace，原子落盘。
- 冒牌原样保留在 `*_quarantine.jsonl`，可随时人工复核 / 还原。
- `--dry-run` 只统计不写盘。

用法
----
    python3 scripts/clean_xiaosheng_provenance.py [--base-dir PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _is_automation_noise(body: str) -> bool:
    """理解池噪音判定（与 worldview_reflection._is_automation_noise 同义，单一事实源）。"""
    try:
        from deskmate.agency.behaviors.worldview_reflection import _is_automation_noise as fn
        return fn(body)
    except Exception:
        b = (body or "").strip()
        if b.startswith("检索到以下资料"):
            return True
        if "⏰ Scheduled run" in b:
            return True
        if "This automation is due now" in b:
            return True
        return False


def _is_unambiguous_automation_observation(body: str) -> bool:
    """源文件保守判定：unambiguously 自动化会话提示词。

    不用「检索到以下资料」——萧盛本人查资料也是这格式，不可据此删源。
    """
    b = (body or "").strip()
    if b.startswith("⏰ Scheduled run"):
        return True
    if "This automation is due now" in b:
        return True
    return False


_PROV = "（内部·萧盛）"


def _atomic_write(path: str, lines: list[str]) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln)
                if not ln.endswith("\n"):
                    f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_lines(path: str) -> list[str]:
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [ln for ln in f.read().splitlines() if ln.strip()]


def clean_xiaosheng_provenance(base_dir: str, dry_run: bool = False) -> dict:
    agency = os.path.join(base_dir, "agency")
    upath = os.path.join(agency, "observation_understanding.jsonl")
    opath = os.path.join(agency, "xiaosheng_observations.jsonl")
    u_quar = os.path.join(agency, "observation_understanding_quarantine.jsonl")
    o_quar = os.path.join(agency, "xiaosheng_observations_quarantine.jsonl")

    summary: dict = {
        "base_dir": base_dir,
        "dry_run": dry_run,
        "understanding": {"read": 0, "tagged": 0, "removed_noise": 0, "kept": 0},
        "observations": {"read": 0, "removed_automation": 0, "kept": 0},
    }

    # ---- 理解池：移除冒牌条目（保留为待审，不留在活跃池） ----
    up_lines = _read_lines(upath)
    summary["understanding"]["read"] = len(up_lines)
    u_out: list[str] = []
    u_quar_lines: list[str] = []
    for ln in up_lines:
        try:
            rec = json.loads(ln)
        except Exception:
            u_out.append(ln)
            continue
        body = str(rec.get("body", ""))
        if _PROV in body:
            summary["understanding"]["tagged"] += 1
            _clean = body.replace(_PROV, "").strip()
            if _is_automation_noise(_clean):
                summary["understanding"]["removed_noise"] += 1
                u_quar_lines.append(ln)  # 原样待审
                continue
        u_out.append(ln)
    summary["understanding"]["kept"] = len(u_out)

    # ---- 原始素材：只保守移除 unambiguously 自动化会话提示词 ----
    op_lines = _read_lines(opath)
    summary["observations"]["read"] = len(op_lines)
    o_out: list[str] = []
    o_quar_lines: list[str] = []
    for ln in op_lines:
        try:
            rec = json.loads(ln)
        except Exception:
            o_out.append(ln)
            continue
        body = str(rec.get("body", ""))
        if _is_unambiguous_automation_observation(body):
            summary["observations"]["removed_automation"] += 1
            o_quar_lines.append(ln)
            continue
        o_out.append(ln)
    summary["observations"]["kept"] = len(o_out)

    if dry_run:
        return summary

    # ---- 备份 + 原子落盘 ----
    for src in (upath, opath):
        if os.path.isfile(src):
            bak = src + ".bak"
            if not os.path.exists(bak):
                shutil.copy2(src, bak)
    _atomic_write(upath, u_out)
    _atomic_write(opath, o_out)
    if u_quar_lines:
        _atomic_write(u_quar, u_quar_lines)
    if o_quar_lines:
        _atomic_write(o_quar, o_quar_lines)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="清理冒牌「内部·萧盛」溯源标")
    ap.add_argument(
        "--base-dir",
        default=os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate")),
        help="记忆库根目录（默认 ~/.deskmate）",
    )
    ap.add_argument("--dry-run", action="store_true", help="只统计不写盘")
    args = ap.parse_args()

    s = clean_xiaosheng_provenance(args.base_dir, dry_run=args.dry_run)
    print(json.dumps(s, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
