#!/usr/bin/env python3
"""deskmate-migrate —— 小桔大脑搬家 CLI（整库快照 + 底座无关可移植包）。

用法：
    python scripts/deskmate_migrate.py export --out /Volumes/xs/xiaoju-backup \
        --base ~/.deskmate --book /path/to/小说目录 --book /path/to/另一本
    python scripts/deskmate_migrate.py verify /Volumes/xs/xiaoju-backup
    python scripts/deskmate_migrate.py import /Volumes/xs/xiaoju-backup --target ~/.deskmate
    python scripts/deskmate_migrate.py import /Volumes/xs/xiaoju-backup \
        --target /new/base/imported --portable-only   # 只落明文包，不碰数据库

安全：
- 导出绝不打包密钥（secrets.json/.env/mcp.json/token）、史料库（history/）、历史备份（backups/）。
- 导入前强制校验 manifest + sha256；非空目标须 --force；向量随整库走，不重刷。
- 设计论证见 docs/brainstorm/rounds/2026-11-20-小桔大脑可移植性.md（决策 D4）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskmate.migration import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
