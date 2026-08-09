"""sidecar 发现：定位本地 OpenWorker sidecar 的端口与 token。

两种来源（优先级从高到低）：
1. 环境变量 ``COWORKER_PORT`` + ``COWORKER_API_TOKEN``
   —— App 模式下由 sidecar 拉起适配器时通过 env 传入（不落盘，保住内存 token 安全）。
2. ``state_dir()/sidecar-{port}.token`` 文件
   —— dev/standalone 模式下 ``coworker/server/run.py`` 写入的每端口 token 文件。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Tuple

import httpx


def resolve_sidecar() -> Tuple[int, str]:
    """返回 ``(port, token)``；找不到运行中的 sidecar 时抛 ``RuntimeError``。"""
    env_port = os.environ.get("COWORKER_PORT")
    env_token = os.environ.get("COWORKER_API_TOKEN")
    if env_port and env_token:
        return int(env_port), env_token

    try:
        from coworker.secrets import state_dir
    except Exception:
        state_dir = lambda: Path.home() / ".config" / "coworker"  # type: ignore
    d = state_dir()

    candidates: list[tuple[int, str]] = []
    for p in sorted(d.glob("sidecar-*.token")):
        name = p.name  # 形如 sidecar-18795.token
        if not (name.startswith("sidecar-") and name.endswith(".token")):
            continue
        mid = name[len("sidecar-"): -len(".token")]
        try:
            port = int(mid)
        except ValueError:
            continue
        try:
            token = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if token:
            candidates.append((port, token))

    for port, token in candidates:
        if _ping(port):
            return port, token

    raise RuntimeError(
        "找不到运行中的 OpenWorker sidecar：env 中无 COWORKER_PORT/COWORKER_API_TOKEN，"
        f"且 {d} 下无可用 sidecar-*.token。请先启动 OpenWorker。"
    )


def _ping(port: int, timeout: float = 1.0) -> bool:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/v1/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False
