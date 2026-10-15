"""办公文档「源保护 + 原子替换 + 恢复副本」交付工具。

对标 PilotDeck 的 `deliver --replace-source`：在编辑/覆盖已有 Office 文档时，
先留恢复副本（`.bak` 滚动），再用临时文件 + `os.replace` 原子替换原文件，
绝不半途截断、绝不丢失原稿。

设计要点：
- **源保护（recovery copy）**：编辑任何已存在文件前，`protect` 先把原文件滚
  动备份为 `<file>.bak` / `<file>.bak.1` / ...（保留最近 N 份）。原稿永远可回滚。
- **原子替换**：`deliver --candidate <新文件>` 先把候选写到同目录临时文件，
  再 `os.replace` 到原路径——要么整体替换成功，要么原文件不动，不存在「写了一半」。
- **in-place 友好**：若 agent 用 officecli 原地编辑并 save（无独立候选），
  `deliver --source <原文件>`（不带 --candidate）仅补做源保护（`.bak`）。
- **恢复**：`restore <file>` 从 `.bak` 还原，把当前版另存为 `<file>.broken` 以防丢数据。

用法（技能正文里让 agent 这样调）：
    python -m deskmate.skills.tools.office_safe_deliver protect <源文件>
    python -m deskmate.skills.tools.office_safe_deliver deliver --source <源> [--candidate <候选>]
    python -m deskmate.skills.tools.office_safe_deliver restore <源文件>

退出码：0=成功；非 0=失败（错误信息走 stderr + JSON）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional


DEFAULT_KEEP = 5  # 滚动保留的 .bak 数量


def _emit(result: dict, ok: bool) -> int:
    print(json.dumps(result, ensure_ascii=False))
    return 0 if ok else 1


def _roll_backups(path: Path, keep: int) -> list[str]:
    """把已有 .bak / .bak.1 ... 向上滚动一格，腾出 .bak 位置。

    Returns: 本次新生成的 .bak 路径（若有）。
    """
    # 先整体向上滚：.bak.(keep-1) 删、.bak.(k) -> .bak.(k+1) ...
    for k in range(keep - 1, 0, -1):
        older = path.with_suffix(path.suffix + f".bak.{k}")
        newer = path.with_suffix(path.suffix + f".bak.{k + 1}")
        if older.exists():
            if newer.exists():
                newer.unlink()
            shutil.move(str(older), str(newer))
    # 现有 .bak -> .bak.1
    bak = path.with_suffix(path.suffix + ".bak")
    if bak.exists():
        bak1 = path.with_suffix(path.suffix + ".bak.1")
        if bak1.exists():
            bak1.unlink()
        shutil.move(str(bak), str(bak1))
    return [str(bak)]


def protect(source: str, keep: int = DEFAULT_KEEP) -> int:
    """编辑前保护源文件：若源存在，滚动备份为 .bak。"""
    p = Path(source)
    if not p.exists():
        return _emit({"status": "noop", "reason": "源文件不存在，无需保护", "source": source}, ok=True)
    if not p.is_file():
        return _emit({"status": "error", "reason": "源不是普通文件", "source": source}, ok=False)
    generated = _roll_backups(p, keep)
    shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
    return _emit({
        "status": "protected",
        "source": source,
        "backup": str(p.with_suffix(p.suffix + ".bak")),
        "rolled": generated,
    }, ok=True)


def deliver(source: str, candidate: Optional[str] = None, keep: int = DEFAULT_KEEP) -> int:
    """原子交付。

    - candidate 给定：保护源 → 候选写到同目录临时文件 → os.replace 到源路径。
    - candidate 未给定（原地编辑）：仅补做源保护（.bak）。
    """
    p = Path(source)
    if candidate is not None:
        c = Path(candidate)
        if not c.exists() or not c.is_file():
            return _emit({"status": "error", "reason": "候选文件不存在或非文件", "candidate": candidate}, ok=False)
        if c.stat().st_size == 0:
            return _emit({"status": "error", "reason": "候选文件为空，拒绝覆盖源", "candidate": candidate}, ok=False)
        # 先保护源：滚动旧 .bak + 把当前源复制为 .bak（恢复副本），再原子替换
        protect(source, keep=keep)
        # 临时文件放同目录，保证 os.replace 同文件系统（原子且不会跨盘半截）
        tmp_fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        os.close(tmp_fd)
        try:
            shutil.copy2(c, tmp_name)
            os.replace(tmp_name, str(p))  # 原子替换
        except Exception as e:  # noqa: BLE001
            if Path(tmp_name).exists():
                Path(tmp_name).unlink()
            return _emit({"status": "error", "reason": f"原子替换失败: {e}", "source": source}, ok=False)
        return _emit({
            "status": "delivered",
            "source": source,
            "candidate": candidate,
            "backup": str(p.with_suffix(p.suffix + ".bak")),
            "size": p.stat().st_size,
        }, ok=True)
    # 无候选：原地编辑场景，仅补做源保护
    return protect(source, keep=keep)


def restore(source: str, which: int = 0) -> int:
    """从 .bak 还原。which=0 → .bak；which=k → .bak.k。

    当前版本先另存为 `<file>.broken` 再还原，避免还原即丢当前成果。
    """
    p = Path(source)
    if which == 0:
        bak = p.with_suffix(p.suffix + ".bak")
    else:
        bak = p.with_suffix(p.suffix + f".bak.{which}")
    if not bak.exists():
        return _emit({"status": "error", "reason": "恢复副本不存在", "backup": str(bak)}, ok=False)
    if p.exists():
        broken = p.with_suffix(p.suffix + ".broken")
        # 避免覆盖已有 .broken
        n = 0
        while broken.exists():
            n += 1
            broken = p.with_suffix(p.suffix + f".broken.{n}")
        shutil.copy2(p, broken)
    shutil.copy2(bak, p)
    return _emit({"status": "restored", "source": source, "from": str(bak), "prev_saved_as": str(broken) if p.exists() else None}, ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="办公文档源保护 + 原子替换 + 恢复副本")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_protect = sub.add_parser("protect", help="编辑前保护源文件（滚动 .bak）")
    p_protect.add_argument("source")
    p_protect.add_argument("--keep", type=int, default=DEFAULT_KEEP)

    p_deliver = sub.add_parser("deliver", help="原子交付（覆盖源 / 原地保护）")
    p_deliver.add_argument("--source", required=True)
    p_deliver.add_argument("--candidate", default=None)
    p_deliver.add_argument("--keep", type=int, default=DEFAULT_KEEP)

    p_restore = sub.add_parser("restore", help="从 .bak 恢复")
    p_restore.add_argument("source")
    p_restore.add_argument("--which", type=int, default=0)

    args = ap.parse_args()
    if args.cmd == "protect":
        return protect(args.source, keep=args.keep)
    if args.cmd == "deliver":
        return deliver(args.source, candidate=args.candidate, keep=args.keep)
    if args.cmd == "restore":
        return restore(args.source, which=args.which)
    return 1


if __name__ == "__main__":
    sys.exit(main())
