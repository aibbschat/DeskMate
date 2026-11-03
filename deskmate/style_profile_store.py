"""写作风格画像增量更新存储（P1-5 实施）。

职责：
- 读取 `~/.deskmate/style_profile.json`（或指定路径）现有画像；
- 用新文本的量化风格信号（deskmate.style_metrics）增量合并；
- **按来源分桶**（``manuscript`` 书稿 / ``chat`` 对话）：写作侧文风指纹只取书稿桶，
  聊天继续单独学聊天语感，互不稀释；
- 保留原有定性 profile（summary/features），新增 `metrics`（量化层）+
  `buckets`（分桶量化层）+ `samples`（来源快照）+ `updates`（更新次数）——
  **向后兼容**：deskmate.writing 的 `format_profile_for_prompt` 只读 summary/features，
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
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from deskmate.persistence import BlobStore
from deskmate.style_metrics import compute_style_metrics, merge_metrics

DEFAULT_PROFILE_PATH = (
    Path(os.environ.get("DESKMATE_DIR", str(Path.home() / ".deskmate")))
    / "style_profile.json"
)

# 样本分桶：书稿样本（作者本人的作品）与对话样本（日常聊天语感）分开存。
# 写作侧的文风指纹**只认书稿桶**——聊天语感不能冒充文风（2026-11-01 萧哥拍板）。
BUCKET_MANUSCRIPT = "manuscript"
BUCKET_CHAT = "chat"
_BUCKET_LABELS = {BUCKET_MANUSCRIPT: "书稿样本", BUCKET_CHAT: "对话样本"}


def _resolve_bucket(source: str, bucket: Optional[str]) -> str:
    """确定本次样本进哪个桶：显式指定优先，否则按来源推断（对话-* → 聊天桶）。"""
    if bucket in (BUCKET_MANUSCRIPT, BUCKET_CHAT):
        return bucket
    return BUCKET_CHAT if (source or "").startswith("对话") else BUCKET_MANUSCRIPT


def _is_default_path(path: Path) -> bool:
    """是否默认全局画像路径——是则走统一库，否则走显式文件（CLI/自定义路径）。"""
    return Path(path) == DEFAULT_PROFILE_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_profile(path: Path) -> dict:
    """读取现有画像；不存在或损坏返回空 dict。

    默认全局路径（~/.deskmate/style_profile.json）从统一库 deskmate.db 读
    （首次自动从 legacy JSON 迁移）；显式路径仍走文件（CLI/自定义路径兼容）。
    """
    if _is_default_path(path):
        data = BlobStore().get(BlobStore.KEY_STYLE, legacy_path=DEFAULT_PROFILE_PATH)
        return data if isinstance(data, dict) else {}
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_profile(path: Path, data: dict) -> None:
    """保存画像。默认全局路径写入统一库 deskmate.db；显式路径走文件（原子写）。"""
    if _is_default_path(path):
        BlobStore().set(BlobStore.KEY_STYLE, data)
        return
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
    bucket: Optional[str] = None,
) -> dict:
    """用一份新文本增量更新风格画像（按来源分桶：书稿 / 对话）。

    - ``bucket="manuscript"``：书稿样本（他本人写的稿子）。写作/审稿的文风指纹**只认这桶**。
    - ``bucket="chat"``：对话样本（日常聊天语感），只用于聊天时学他的口气，不冒充文风。
    - ``bucket`` 缺省按 ``source`` 推断：以「对话」开头的来源进聊天桶，其余进书稿桶。
    - 顶层 ``metrics``/``samples``/``chars`` 仍按旧语义维护（混合口径，只留给旧读者兼容）；
      写作侧不再消费顶层，避免聊天语感污染文风指纹。

    **并发安全**：默认全局路径（走统一库）通过 ``BlobStore.atomic_update`` 把
    load→merge→save 包在 SQLite ``BEGIN IMMEDIATE`` 事务里，彻底消除多进程/线程
    read-modify-write 竞态。显式路径（文件）仍走原有逻辑（CLI/测试场景，无并发）。

    Args:
        text: 新样本文本（书稿 / 聊天消息）。
        source: 来源描述（如「《国子监秘闻录》第3章」「对话-2026-11-01」），用于 samples 记录。
        profile_path: 画像路径，默认 ~/.deskmate/style_profile.json。
        max_sample_chars: 单次样本送入统计的最大字符数（控制开销）。
        bucket: 样本桶；缺省按 source 推断。

    Returns:
        更新后的完整画像 dict。
    """
    path = profile_path or DEFAULT_PROFILE_PATH

    sample = (text or "").strip()
    if not sample:
        return load_profile(path)
    sample = sample[:max_sample_chars]
    bkey = _resolve_bucket(source, bucket)

    metrics_new = compute_style_metrics(sample)
    if not int(metrics_new.get("chars", 0) or 0):
        return load_profile(path)  # 没有中文正文（空稿 / 纯代码）不入画像

    digest = hashlib.sha1(sample.encode("utf-8")).hexdigest()

    # ---- 构建合并逻辑（作为 fn 传给 atomic_update，或直接执行）----
    def _merge(old_data: dict) -> Optional[dict]:
        """纯合并逻辑：输入旧画像，返回新画像（或 None 表示放弃写入）。"""
        data = old_data if isinstance(old_data, dict) else {}

        # 幂等去重（书稿桶）
        hashes = data.get("ingested_hashes")
        if not isinstance(hashes, list):
            hashes = []
        if bkey == BUCKET_MANUSCRIPT and digest in hashes:
            return None  # 放弃写入，返回 None 不触发 update

        # ---- 提取既有状态（顶层混合口径，保持旧字段语义不变） ----
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
        if len(samples) > 20:
            samples = samples[-20:]

        # ---- 分桶更新（书稿 / 对话各算各的，互不稀释） ----
        buckets = data.get("buckets")
        if not isinstance(buckets, dict):
            buckets = {}
        entry = buckets.get(bkey)
        if not isinstance(entry, dict):
            entry = {}
        b_metrics_old = entry.get("metrics") if isinstance(entry.get("metrics"), dict) else None
        b_n = int(b_metrics_old.get("n", 0) or 0) if b_metrics_old else 0
        b_metrics = merge_metrics(b_metrics_old, metrics_new, b_n)
        b_metrics["n"] = b_n + 1
        b_samples = entry.get("samples") if isinstance(entry.get("samples"), list) else []
        b_samples.append(
            {
                "source": source or f"sample-{int(entry.get('updates', 0) or 0) + 1}",
                "chars": metrics_new.get("chars", 0),
                "updated_at": _now_iso(),
                "metrics": metrics_new,
            }
        )
        if len(b_samples) > 20:
            b_samples = b_samples[-20:]
        buckets[bkey] = {
            "metrics": b_metrics,
            "samples": b_samples,
            "chars": int(entry.get("chars", 0) or 0) + int(metrics_new.get("chars", 0) or 0),
            "updates": int(entry.get("updates", 0) or 0) + 1,
            "updated_at": _now_iso(),
        }

        # ---- 组装新画像 ----
        profile = data.get("profile") or {}
        return {
            "profile": profile,
            "summary": profile.get("summary", ""),
            "features": profile.get("features", {}) if isinstance(profile.get("features"), dict) else {},
            "metrics": metrics_merged,
            "samples": samples,
            "buckets": buckets,
            "ingested_hashes": (hashes + [digest])[-100:],
            "source": source or data.get("source", ""),
            "chars": chars_total + metrics_new.get("chars", 0),
            "updates": updates + 1,
            "first_updated_at": first_updated,
            "updated_at": _now_iso(),
        }

    # ---- 执行：默认全局路径走原子事务；显式路径走原有 load-modify-save ----
    if _is_default_path(path):
        result = BlobStore().atomic_update(
            BlobStore.KEY_STYLE,
            _merge,
            legacy_path=DEFAULT_PROFILE_PATH,
        )
        if result is None:
            # atomic_update 返回 None 可能是 fn 返回 None（去重）或异常
            return load_profile(path)
        clear_style_fingerprint_cache()
        return result
    else:
        # 显式路径（CLI/自定义）：无并发，原有逻辑安全
        old_data = load_profile(path)
        new_data = _merge(old_data)
        if new_data is None:
            return old_data if isinstance(old_data, dict) else {}
        save_profile(path, new_data)
        clear_style_fingerprint_cache()
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


# 文风指纹缓存（按路径）：画像不会在单次进程内变化，避免每次生成都读盘。
_STYLE_FP_CACHE: dict[str, str] = {}


def clear_style_fingerprint_cache() -> None:
    """清空文风指纹缓存（测试 / 画像刷新后重载用）。"""
    _STYLE_FP_CACHE.clear()


def _bucket_entry(data: dict, bucket: str) -> Optional[dict]:
    """取某个桶的条目（无则 None）。"""
    buckets = data.get("buckets")
    if not isinstance(buckets, dict):
        return None
    entry = buckets.get(bucket)
    return entry if isinstance(entry, dict) and isinstance(entry.get("metrics"), dict) else None


def _bucket_metrics(data: dict, bucket: str) -> Optional[dict]:
    """取某个桶的量化指标；无 buckets 的旧画像按「素材来源」兼容归属。"""
    entry = _bucket_entry(data, bucket)
    if entry:
        return entry["metrics"]
    # 兼容旧画像（还没有 buckets 字段）：历史自动抽取全部来自对话消息，
    # 因此顶层混合指标归聊天桶；书稿桶保持空 → 写作侧会明确降级告知，不拿聊天语感冒充文风。
    if bucket == BUCKET_CHAT and isinstance(data.get("metrics"), dict):
        return data["metrics"]
    return None


def manuscript_fingerprint_available(profile_path: Optional[Path] = None) -> bool:
    """是否已有书稿样本（写作侧据此区分「须贴合」与「仅粗参考」）。"""
    data = load_profile(profile_path or DEFAULT_PROFILE_PATH)
    metrics = _bucket_metrics(data, BUCKET_MANUSCRIPT)
    return bool(metrics) and int(metrics.get("n", 0) or 0) > 0


def format_style_fingerprint(
    profile_path: Optional[Path] = None,
    *,
    bucket: str = BUCKET_MANUSCRIPT,
    use_cache: bool = True,
) -> str:
    """把已量化的风格画像格式化为紧凑「文风指纹」字符串，供写作/审稿 prompt 注入。

    - 默认只取**书稿桶**：写作侧绝不能拿聊天语感当文风（2026-11-01 修正）。
    - 返回 "" 表示无画像或尚无量化指标（零配置场景不注入、不污染 prompt）。
    - 书稿桶为空但存在旧/对话指标时，**明确降级告知**（标注口径，提醒不要据此收敛文风）。
    - 结果按「路径 + 桶」缓存（画像在单次进程内不变），避免每次 LLM 生成都读盘。
    """
    path = profile_path or DEFAULT_PROFILE_PATH
    key = f"{path}|{bucket}"
    if use_cache and key in _STYLE_FP_CACHE:
        return _STYLE_FP_CACHE[key]
    data = load_profile(path)
    metrics = _bucket_metrics(data, bucket)
    out = ""
    if metrics:
        out = _format_metrics_for_human(metrics)
    elif bucket == BUCKET_MANUSCRIPT and isinstance(data.get("metrics"), dict):
        out = (
            "【暂无书稿样本：以下指标来自对话记录，仅供粗参考，勿据此收敛文风；"
            "多读几本他的书稿后会自动切到书稿口径】\n"
            + _format_metrics_for_human(data["metrics"])
        )
    if use_cache:
        _STYLE_FP_CACHE[key] = out
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="小桔写作风格画像增量更新（P1-5）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法（CLI）：")[1].split("改为")[0] if "用法（CLI）：" in __doc__ else "",
    )
    parser.add_argument("text_file", nargs="?", help="新样本文本文件路径（txt/md/docx 会被尽力解析）")
    parser.add_argument("--source", default="", help="样本来源描述（如《国子监秘闻录》第3章）")
    parser.add_argument("--profile", default=None, help="画像路径（默认 ~/.deskmate/style_profile.json）")
    parser.add_argument(
        "--bucket",
        default=None,
        choices=[BUCKET_MANUSCRIPT, BUCKET_CHAT],
        help="样本桶：manuscript=书稿（默认，写作指纹只认这个）/ chat=对话样本",
    )
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
        print("\n各桶量化指标：")
        buckets = data.get("buckets") if isinstance(data.get("buckets"), dict) else {}
        if not buckets:
            print("（旧画像还没有分桶：现有指标为混合口径，按聊天桶兼容；"
                  "用 --bucket manuscript 导入书稿后会逐步切开）")
        for bkey, entry in buckets.items():
            metrics = (entry or {}).get("metrics") if isinstance(entry, dict) else None
            label = _BUCKET_LABELS.get(bkey, bkey)
            print(f"\n[{label}] 样本数 {(metrics or {}).get('n', 0)} · "
                  f"累计 {(entry or {}).get('chars', 0)} 字")
            print(_format_metrics_for_human(metrics or {}))
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

    data = update_style_profile(
        text, source=args.source, profile_path=path, bucket=args.bucket
    )
    print(f"已更新画像：{path}（{_BUCKET_LABELS.get(_resolve_bucket(args.source, args.bucket), '')}）")
    bkey = _resolve_bucket(args.source, args.bucket)
    entry = (data.get("buckets") or {}).get(bkey) or {}
    print(f"更新次数 → {data.get('updates')} · 累计字符 {data.get('chars')}")
    print(f"\n{_BUCKET_LABELS.get(bkey, bkey)}量化指标（合并后）：")
    print(_format_metrics_for_human(entry.get("metrics") or {}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
