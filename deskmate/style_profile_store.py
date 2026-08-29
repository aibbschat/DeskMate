"""写作风格画像增量更新存储（P1-5 实施）。

职责：
- 读取 `~/.deskmate/style_profile.json`（或指定路径）现有画像；
- 用新文本的量化风格信号（deskmate.style_metrics）增量合并；
- 保留原有定性 profile（summary/features），新增 `metrics`（量化层）+
  `samples`（来源快照）+ `updates`（更新次数）——**向后兼容**：
  deskmate.writing 的 `format_profile_for_prompt` 只读 summary/features，
  不受新增字段影响。

设计（对应 docs/越用越懂你-打磨建议.md P1-5）：
把「一次性 LLM 定性画像」升级为「随作品演进的量化风格指纹」——
每次有新文稿/审稿样本，就增量更新一次；画像随作品进化，「越写越懂你的文风」。

用法（CLI）：
    # 用一份新文本增量更新画像
    python -m deskmate.style_profile_store 书稿.txt --source "《国子监秘闻录》第3章"

    # 仅查看当前画像
    python -m deskmate.style_profile_store --show

    # 指定画像路径 / 自定义样本来源
    python -m deskmate.style_profile_store 稿.txt --profile ~/.deskmate/style_profile.json --source "审稿样本"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from deskmate.style_metrics import compute_style_metrics, merge_metrics

DEFAULT_PROFILE_PATH = (
    Path(os.environ.get("DESKMATE_DIR", str(Path.home() / ".deskmate")))
    / "style_profile.json"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_profile(path: Path) -> dict:
    """读取现有画像；不存在或损坏返回空 dict。"""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_profile(path: Path, data: dict) -> None:
    """原子写盘（临时文件 + replace），避免并发读到半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def update_style_profile(
    text: str,
    *,
    source: str = "",
    profile_path: Optional[Path] = None,
    max_sample_chars: int = 50_000,
) -> dict:
    """用一份新文本增量更新风格画像。

    Args:
        text: 新样本文本（作者文稿/审稿产出）。
        source: 来源描述（如「《国子监秘闻录》第3章」），用于 samples 记录。
        profile_path: 画像路径，默认 ~/.deskmate/style_profile.json。
        max_sample_chars: 单次样本送入统计的最大字符数（控制开销）。

    Returns:
        更新后的完整画像 dict。
    """
    path = profile_path or DEFAULT_PROFILE_PATH
    data = load_profile(path)

    sample = (text or "").strip()
    if not sample:
        return data

    metrics_new = compute_style_metrics(sample[:max_sample_chars])

    # ---- 提取既有状态 ----
    metrics_old = data.get("metrics") if isinstance(data.get("metrics"), dict) else None
    old_n = 0
    if isinstance(metrics_old, dict):
        old_n = int(metrics_old.get("n", 0) or 0)
    samples = data.get("samples")
    if not isinstance(samples, list):
        samples = []
    chars_total = int(data.get("chars", 0) or 0)
    updates = int(data.get("updates", 0) or 0)
    first_updated = data.get("first_updated_at", _now_iso())

    # ---- 合并量化指标（加权平均，样本数 old_n -> old_n+1） ----
    metrics_merged = merge_metrics(metrics_old, metrics_new, old_n)
    metrics_merged["n"] = old_n + 1

    # ---- 记录本次样本快照 ----
    samples.append(
        {
            "source": source or f"sample-{updates + 1}",
            "chars": metrics_new.get("chars", 0),
            "updated_at": _now_iso(),
            "metrics": metrics_new,
        }
    )
    # 快照只保留最近 20 次，避免无限膨胀
    if len(samples) > 20:
        samples = samples[-20:]

    # ---- 组装新画像（保留原 profile 定性层） ----
    profile = data.get("profile") or {}
    new_data = {
        "profile": profile,
        # 顶层冗余 summary/features：deskmate.writing._auto_load_style_profile
        # 会把整个 json 直接丢给 format_profile_for_prompt（只读顶层），
        # 顶层冗余保证两种读取姿势都能正确取到定性画像。
        "summary": profile.get("summary", ""),
        "features": profile.get("features", {}) if isinstance(profile.get("features"), dict) else {},
        "metrics": metrics_merged,
        "samples": samples,
        "source": source or data.get("source", ""),
        "chars": chars_total + metrics_new.get("chars", 0),
        "updates": updates + 1,
        "first_updated_at": first_updated,
        "updated_at": _now_iso(),
    }
    save_profile(path, new_data)
    return new_data


def _format_metrics_for_human(metrics: dict) -> str:
    """把量化指标格式化为人可读文本（供 --show / 备注使用）。"""
    if not metrics:
        return "（暂无量化指标）"
    n = metrics.get("n", 0)
    s = metrics.get("sentences", {})
    p = metrics.get("paragraphs", {})
    f = metrics.get("four_char", {})
    pn = metrics.get("punct", {})
    lines = [
        f"样本数：{n}",
        f"句长：平均 {s.get('avg_len')} / 中位 {s.get('median_len')} 字 · 短句(≤12字) {s.get('short_pct')}% · 长句(≥40字) {s.get('long_pct')}%",
        f"段落：平均 {p.get('avg_len')} 字 · 短段(≤40字) {p.get('short_pct')}% · 长段(≥200字) {p.get('long_pct')}%",
        f"四字格倾向：四字词块占 {f.get('pct_of_chunks')}%",
        f"文言虚词密度：{metrics.get('classical_per_1k')}/千字（现代语气词 {metrics.get('modern_per_1k')}/千字）",
        f"对话占比：{metrics.get('dialogue_pct')}%",
        f"标点节奏（/千字）：句号 {pn.get('period_per_1k')} · 逗号 {pn.get('comma_per_1k')} · 顿号 {pn.get('dunhao_per_1k')} · 分号 {pn.get('semicolon_per_1k')}",
    ]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="墨灵写作风格画像增量更新（P1-5）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法（CLI）：")[1].split("改为")[0] if "用法（CLI）：" in __doc__ else "",
    )
    parser.add_argument("text_file", nargs="?", help="新样本文本文件路径（txt/md/docx 会被尽力解析）")
    parser.add_argument("--source", default="", help="样本来源描述（如《国子监秘闻录》第3章）")
    parser.add_argument("--profile", default=None, help="画像路径（默认 ~/.deskmate/style_profile.json）")
    parser.add_argument("--show", action="store_true", help="仅查看当前画像（含量化指标）")
    args = parser.parse_args(argv)

    path = Path(args.profile).expanduser() if args.profile else DEFAULT_PROFILE_PATH

    if args.show or not args.text_file:
        data = load_profile(path)
        print(f"画像路径：{path}")
        print(f"更新次数：{data.get('updates', 0)} · 累计字符 {data.get('chars', 0)} · 最后更新 {data.get('updated_at', '-')}")
        prof = data.get("profile") or {}
        if prof.get("summary"):
            print(f"\n整体画像：{prof['summary']}")
        feats = prof.get("features") or {}
        for k, v in feats.items():
            print(f"  {k}: {v}")
        print("\n量化指标：")
        print(_format_metrics_for_human(data.get("metrics")))
        if data.get("samples"):
            print("\n样本记录：")
            for smp in data["samples"]:
                print(f"  - [{smp.get('updated_at','')}] {smp.get('source','')}（{smp.get('chars',0)} 字）")
        return 0

    # ---- 读文本 ----
    fp = Path(args.text_file)
    if not fp.is_file():
        print(f"文件不存在：{fp}", file=sys.stderr)
        return 1
    try:
        from deskmate.writing import read_text_from_path

        text = read_text_from_path(str(fp))
    except Exception:
        text = fp.read_text(encoding="utf-8", errors="replace")
    if not text or not text.strip():
        print("文本为空，跳过更新。", file=sys.stderr)
        return 1

    data = update_style_profile(text, source=args.source, profile_path=path)
    print(f"已更新画像：{path}")
    print(f"更新次数 → {data.get('updates')} · 累计字符 {data.get('chars')}")
    print("\n量化指标（合并后）：")
    print(_format_metrics_for_human(data.get("metrics")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
