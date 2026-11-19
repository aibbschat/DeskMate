#!/usr/bin/env python3
"""audit_async_blocking —— 找出「async 端点里做同步重活」的位置（只读审计）。

背景（文档 #28）：FastAPI 的 ``async def`` 路由跑在事件循环线程里，里面的同步
I/O（SQLite、read_text、requests、sleep）会**阻塞整个 loop**，把并发退化成串行。
本脚本用 AST 静默扫描，不改代码：

    python scripts/audit_async_blocking.py [--repo .] [--json]

输出：文件:行 · 函数名 · 命中的同步调用。修复方式是把同步段挪进
``starlette.concurrency.run_in_threadpool``（或把路由改成普通 ``def``）。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

SYNC_CALLS = {
    "sqlite3.connect",
    "read_text",
    "read_bytes",
    "write_text",
    "write_bytes",
    "urlopen",
    "sleep",
    "requests.get",
    "requests.post",
    "subprocess.run",
    "subprocess.check_output",
    "os.system",
}


def _call_name(node: ast.Call) -> str:
    func = node.func
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    return ".".join(reversed(parts))


def _decorated_as_route(fn: ast.AST) -> bool:
    for dec in getattr(fn, "decorator_list", []):
        text = ast.unparse(dec) if hasattr(ast, "unparse") else ""
        if any(k in text for k in ("app.", "router.", "get(", "post(", "put(", "patch(", "delete(")):
            return True
    return False


def scan(repo: Path) -> list[dict]:
    hits: list[dict] = []
    for path in repo.rglob("*.py"):
        if any(part in {".venv", "node_modules", "target", "dist", ".git"} for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not isinstance(node, ast.AsyncFunctionDef) or not _decorated_as_route(node):
                continue
            found: list[str] = []
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    name = _call_name(sub)
                    if name in SYNC_CALLS or ("subprocess" in name and name.endswith(".run")):
                        found.append(name)
            if found:
                hits.append(
                    {
                        "file": str(path.relative_to(repo)),
                        "line": node.lineno,
                        "func": node.name,
                        "calls": sorted(set(found)),
                    }
                )
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="async 端点同步阻塞审计（只读）")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()
    hits = scan(repo)
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return 0
    print(f"命中 {len(hits)} 个 async 路由函数（同步 I/O 会阻塞事件循环）")
    for h in hits:
        print(f"  {h['file']}:{h['line']}  {h['func']}  ← {', '.join(h['calls'])}")
    if hits:
        print("\n修复建议：同步段改用 `await run_in_threadpool(fn, ...)`，")
        print("或把该路由从 `async def` 改为普通 `def`（FastAPI 会自动丢线程池）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
