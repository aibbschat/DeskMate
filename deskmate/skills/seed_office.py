"""办公技能 seed —— 把仓库内置的 office 技能装入用户技能库。

单一真源：`deskmate/skills/seeds/office/*.md`（版本受控）。
目标：`~/.deskmate/skills/office/`（受 DESKMATE_SKILLS_DIR 影响）。

特性：
- **幂等、非破坏**：目标已存在则跳过，绝不覆盖用户在活目录里的手动打磨。
- 与 `SkillStore` 同一套路径解析（DESKMATE_SKILLS_DIR 优先）。
- 可作为模块函数 `ensure_office_skills(base_dir)` 调用，或 `python -m deskmate.skills.seed_office` 直接跑。

用法：
    python -m deskmate.skills.seed_office
    # 强制覆盖（用于把仓库更新后的骨架同步到活目录）：
    python -m deskmate.skills.seed_office --force
"""
from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

# 单一真源：本文件所在目录下的 seeds/office
SEED_DIR = Path(__file__).resolve().parent / "seeds" / "office"
OFFICE_CATEGORY = "office"


def _live_skills_dir() -> Path:
    from deskmate.skills.skill_store import DEFAULT_SKILLS_DIR

    return Path(os.environ.get("DESKMATE_SKILLS_DIR", str(DEFAULT_SKILLS_DIR)))


def ensure_office_skills(*, base_dir=None, force: bool = False) -> dict:
    """把 seeds/office 下的技能装入活目录 office/。

    Args:
        base_dir: 备用（兼容 SkillStore 的 base_dir 约定），实际目录以
            DESKMATE_SKILLS_DIR / DEFAULT 为准。
        force: True 时覆盖已存在的同名文件（同步仓库更新）；默认 False（不破坏手动改动）。

    Returns:
        {"installed": [...], "skipped": [...], "seed_dir": str, "office_dir": str}
    """
    if not SEED_DIR.is_dir():
        return {"error": f"seed 目录不存在: {SEED_DIR}", "installed": [], "skipped": []}

    office_dir = _live_skills_dir() / OFFICE_CATEGORY
    office_dir.mkdir(parents=True, exist_ok=True)

    installed: list[str] = []
    skipped: list[str] = []
    for seed in sorted(SEED_DIR.glob("*.md")):
        dest = office_dir / seed.name
        if dest.exists() and not force:
            skipped.append(seed.name)
            continue
        shutil.copy2(seed, dest)
        installed.append(seed.name)

    return {
        "installed": installed,
        "skipped": skipped,
        "seed_dir": str(SEED_DIR),
        "office_dir": str(office_dir),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="seed 办公技能到用户技能库")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的同名技能")
    args = ap.parse_args()
    result = ensure_office_skills(force=args.force)
    if "error" in result:
        print(result["error"])
        raise SystemExit(1)
    print(f"seed 源: {result['seed_dir']}")
    print(f"装入到: {result['office_dir']}")
    if result["installed"]:
        print(f"已安装({len(result['installed'])}): {', '.join(result['installed'])}")
    if result["skipped"]:
        print(f"已存在跳过({len(result['skipped'])}): {', '.join(result['skipped'])}（用 --force 覆盖）")


if __name__ == "__main__":
    main()
