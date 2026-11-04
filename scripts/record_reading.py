#!/usr/bin/env python3
"""读书任务 -> 「越用越懂你」框架的桥接脚本。

由 Coworker 定时任务（每日读书 / 进度跟踪）在每轮执行末尾通过 run_shell 调用，
把本次读书沉淀登记进 deskmate.auto_tasks（写 ~/.deskmate/auto_tasks/），
从而出现在个人分身档案的「作品与创作」栏目，真正汇入「越用越懂你」框架。

用法（任务指令里给 LLM 的模板）：
  cd /Users/xiaocheng/workspace/openworker && .venv/bin/python scripts/record_reading.py \
    --target "书名" \
    --summary "本次读书的中文摘要，2-4句" \
    --insight "一条最值得记的风格/人物/叙事手法洞察" \
    --tags "风格,叙事" \
    --text-file "本次读到的原文正文存成的 txt 路径（可选）"

说明：
  - 即使没有 --insight/--tags，也会记录「读完动作」本身（含 summary）。
  - insight 默认 push_to_vault=True（能提炼出来的就是值得沉淀的）。
  - --text-file：本次读到的**书的原文**（非读后感），可重复传多个文件，逐文件喂进
    文风画像的「书稿桶」（bucket=manuscript，按内容 sha1 去重）。只传正文
    （.txt/.md），合同/目录/人物资料卡不传；没有就省略，不影响登记。
  - 异常会打印并 exit(1)，但不影响读书主流程（它是附加登记步骤）。
"""
import argparse
import os
import sys

# 让脚本在 cwd 不在仓库时也能 import deskmate.auto_tasks
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def main() -> int:
    p = argparse.ArgumentParser(description="登记读书沉淀到 auto_tasks 框架")
    p.add_argument("--target", required=True, help="作品/书名")
    p.add_argument("--summary", default="", help="本次读书摘要")
    p.add_argument("--insight", default="", help="一条值得记的洞察（可选）")
    p.add_argument("--tags", default="", help="洞察标签，逗号分隔，如 风格,叙事")
    p.add_argument("--task-type", default="daily_reading", help="auto_tasks 任务类型")
    p.add_argument("--task-name", default="每日读书", help="展示用任务名")
    p.add_argument("--action", default="finish", help="动作类型，默认 finish")
    p.add_argument(
        "--text-file",
        action="append",
        default=None,
        help="本次读到的原文正文文件（txt/md，可重复传多个），逐文件喂进文风画像书稿桶",
    )
    args = p.parse_args()

    from deskmate.auto_tasks import record_task_result

    tags = [t.strip() for t in args.tags.split(",") if t.strip()] or None
    insights = []
    if args.insight.strip():
        insights.append(
            {
                "text": args.insight.strip(),
                "tags": tags or ["风格"],
                "push_to_vault": True,
            }
        )

    rec = record_task_result(
        task_type=args.task_type,
        target=args.target,
        action=args.action,
        summary=args.summary.strip(),
        insights=insights,
        tags=tags,
        task_name=args.task_name,
    )

    # 本次读到的原文喂进文风画像「书稿桶」（fail-open：失败不影响上面的登记，但要报出来）
    style_failures = 0
    from deskmate.persistence import BlobWriteError
    from deskmate.style_profile_store import (
        DEFAULT_PROFILE_PATH,
        content_digest,
        load_profile,
        update_style_profile,
    )

    for tf in args.text_file or []:
        tf = tf.strip()
        if not tf:
            continue
        try:
            raw = open(os.path.expanduser(tf), encoding="utf-8", errors="replace").read()
            sample = raw.strip()
            if len(sample) < 500:
                print(f"SKIP style: {tf} 过短（{len(sample)} 字 < 500），不入画像")
                continue

            # 幂等自查：内容已在桶里 → 明确报 SKIP，别把「跳过」算成失败
            digest = content_digest(raw)
            before = load_profile(DEFAULT_PROFILE_PATH)
            if digest in set(before.get("ingested_hashes") or []):
                print(f"SKIP style: {tf} 内容已在书稿桶（幂等跳过，未重复计入）")
                continue

            before_n = _manuscript_n(before)
            update_style_profile(
                raw,
                source=f"书稿-{args.target}·{os.path.basename(tf)}",
                bucket="manuscript",
                raise_on_error=True,
            )
            after_n = _manuscript_n(load_profile(DEFAULT_PROFILE_PATH))
            if after_n <= before_n:
                # 关键校验：不再无条件打 OK——没涨就是没落库（沙箱/锁/未知原因）
                print(
                    f"WARN style: {tf} 调用未报错但书稿桶样本数未增长（{before_n}→{after_n}），判为未入桶",
                    file=sys.stderr,
                )
                style_failures += 1
            else:
                print(f"OK style: {tf}（{len(raw)} 字）已入书稿桶（样本 {before_n}→{after_n}）")
        except BlobWriteError as exc:
            print(f"WARN style: {tf} 写统一库失败（沙箱/权限/锁？）: {exc}", file=sys.stderr)
            style_failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"WARN style: {tf} 入画像失败（不影响登记）: {exc}", file=sys.stderr)
            style_failures += 1

    print(f"OK recorded -> task_type={rec.task_type} target={rec.target} insights={len(rec.insights)}")
    if style_failures:
        print(f"喂桶失败 {style_failures} 份，需补跑（登记已完成）", file=sys.stderr)
        return 2
    return 0


def _manuscript_n(profile: dict) -> int:
    """取画像里书稿桶的样本数（缺失按 0）。"""
    try:
        entry = (profile.get("buckets") or {}).get("manuscript") or {}
        return int((entry.get("metrics") or {}).get("n", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"record_reading FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
