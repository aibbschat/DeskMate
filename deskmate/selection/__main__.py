"""适配器启动入口：``python -m deskmate.selection``。

固定端口（默认 18795），仅监听 127.0.0.1。App 模式下由 sidecar 通过环境变量
COWORKER_PORT / COWORKER_API_TOKEN 拉起，无需 token 文件；并随 sidecar 一起退出。
"""
from __future__ import annotations

import os

import uvicorn

from coworker.orphan import _exit_when_orphaned

from .server import build_app


def main() -> None:
    # Exit together with the sidecar that spawned us (App mode): when the app quits,
    # the sidecar dies and we follow, freeing port 18795 for the next launch.
    try:
        _exit_when_orphaned()
    except Exception:
        pass
    port = int(os.environ.get("DESKMATE_SELECTION_PORT", "18795"))
    uvicorn.run(build_app(), host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
