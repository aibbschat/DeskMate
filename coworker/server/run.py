"""Launch the server with uvicorn. Used by the desktop GUI sidecar and `openworker-server`."""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
from pathlib import Path

logger = logging.getLogger("coworker.server.run")

from ..config import load_config
from ..orphan import _exit_when_orphaned
from ..permissions import Mode
from ..secrets import _load_dotenv, state_dir, write_private_text
from .app import _WS_MAX_FRAME_BYTES, create_app
from .manager import SessionManager
from ..mcp.builtin import _MCP_RUN_FLAG

# 空闲 HTTP keep-alive 时长（2026-10-13 13:45 面板全空事故根因，详见下方
# uvicorn.run 接线处注释）。uvicorn 默认 5s 会制造「服务端已 FIN、WebView
# 仍在复用」的半死连接竞态；300s 远长于前端最长轮询间隔（30s）。
KEEP_ALIVE_SECONDS = 300


def _load_env_file() -> None:
    """Load ~/.config/coworker/.env into os.environ (does NOT override existing vars).

    SecretStore only resolves ${VAR} refs inside JSON configs; it never populates
    os.environ. But deskmate feature flags (DESKMATE_AUTO_INJECT,
    DESKMATE_PROACTIVE_BACKGROUND) are read via os.environ.get() at runtime in the
    main process. Without this loader, those flags are always off even if .env
    sets them - the "gets smarter over time" loop never closes.
    """
    try:
        env = _load_dotenv(state_dir() / ".env")
        for k, v in env.items():
            if k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass  # never block startup on .env loading


def build_app(workspace: str | None, model: str, mode: str):
    manager = SessionManager(
        workspace=Path(workspace).expanduser().resolve() if workspace else None,
        data_dir=state_dir(),
        model=model,
        mode=Mode(mode),
    )
    return create_app(manager)


def _ensure_ca_bundle() -> None:
    """Point SSL at certifi's CA bundle if the interpreter has none configured. macOS framework
    Python ships without a usable system trust store for `aiohttp` (it builds an `ssl` context with
    no CAs), so the Slack Socket-Mode client fails with CERTIFICATE_VERIFY_FAILED. `httpx`/`requests`
    bundle certifi already; aiohttp honours the SSL_CERT_FILE env var, so set it once at startup.
    """
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi

        os.environ["SSL_CERT_FILE"] = certifi.where()
    except Exception:
        pass


def _ensure_api_token(port: int) -> Path | None:
    """Set launch auth; standalone/dev tokens use a user-only, port-specific file."""
    if os.environ.get("COWORKER_API_TOKEN"):
        return None  # Tauri supplied an in-memory token; never persist it.
    token = secrets.token_hex(32)
    os.environ["COWORKER_API_TOKEN"] = token
    return write_private_text(
        state_dir() / f"sidecar-{port}.token", token + "\n"
    )


def _spawn_selection_adapter(port: int) -> None:
    """已退役（2026-11-16 萧哥拍板：划词功能下线），保留壳供考古/复活。

    原职责：sidecar 启动时自动拉起 ``python -m deskmate.selection``，给外部
    划词客户端（OpenAI Translator 等）提供本地 OpenAI 兼容端点。该端点默认
    路由本机 OmniRoute 免费网关，而 OmniRoute 已于 2026-10-24 删除——默认
    「free」档实际是死路，功能早已无人可用，故整条拉起链路撤掉。

    服务本体 ``deskmate/selection/`` 与接口测试 ``tests/test_deskmate_selection.py``
    保留勿删；``DESKMATE_SELECTION_DISABLE`` 开关随之失效（恒不拉起）。
    复活＝恢复本函数体（见 git 历史 2026-11-16 之前版本）。
    """
    return
    return


def _seed_builtin_mcp_servers() -> None:
    """Bootstrap OpenWorker's preinstalled MCP servers into the global config.

    Idempotent and non-destructive: missing builtins are added, path drift (e.g.
    after a venv move / reinstall) is repaired, user-customized same-named
    servers are left untouched. Lets the existing 5 deskmate servers become
    "preinstalled plugins" instead of hand-edited mcp.json entries. Failures
    never block startup.
    """
    try:
        from ..mcp.builtin import ensure_builtin_servers

        touched = ensure_builtin_servers()
        if touched:
            logger.info("seeded builtin MCP servers: %s", ", ".join(touched))
    except Exception as exc:  # never block sidecar startup on bootstrap
        logger.warning("builtin MCP seed skipped: %s", exc)


def main(argv=None) -> None:
    _load_env_file()
    _ensure_ca_bundle()

    # Frozen-build MCP subprocess dispatch. PyInstaller bundles `openworker-server`
    # as a single binary, so the builtin MCP servers (declared via `python -m
    # <module>`) cannot spawn a fresh interpreter. Instead they relaunch THIS
    # binary with `--mcp-run <module>`; we route that to runpy — exactly what
    # `python -m <module>` does — and never reach the uvicorn server path below.
    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)
    if argv and argv[0] == _MCP_RUN_FLAG:
        module = argv[1] if len(argv) > 1 else ""
        if module:
            import runpy

            # runpy.run_module does NOT reset sys.argv: without this the child's argv
            # still carries ["--mcp-run", <module>] and every builtin MCP server's
            # `parser.parse_args()` dies with "unrecognized arguments". Stripping the
            # dispatch token makes it behave exactly like `python -m <module>`.
            sys.argv = [sys.argv[0], *argv[2:]]
            runpy.run_module(module, run_name="__main__")
        return

    _seed_builtin_mcp_servers()
    cfg = load_config()  # global config supplies defaults
    parser = argparse.ArgumentParser(prog="openworker-server")
    parser.add_argument("--cwd", default=None, help="optional seed/default workspace")
    parser.add_argument("--model", default=cfg.model)
    parser.add_argument(
        "--mode",
        default=cfg.mode,
        choices=["discuss", "plan", "interactive", "auto", "bypass-approvals", "auto-approve"],
    )
    parser.add_argument("--host", default=cfg.host)
    parser.add_argument("--port", type=int, default=cfg.port)
    args = parser.parse_args(argv)

    # Publish the ACTUAL bound port so loopback URLs (the managed-OAuth callback)
    # target this process, not config.port. The desktop shell runs the sidecar on
    # a random free port (to coexist with a hand-run server on 8765), so the
    # managed-connect redirect must follow the real port, not the 8765 default.
    os.environ["COWORKER_PORT"] = str(args.port)
    generated_token_path = _ensure_api_token(args.port)
    try:
        import uvicorn

        _exit_when_orphaned()
        # Bring up the select-text popup adapter so it's ready the moment the app opens.
        _spawn_selection_adapter(args.port)
        app = build_app(args.cwd, args.model, args.mode)
        # access log 带时间戳（2026-10-13 吞吐排查时发现默认格式无时间戳，
        # 「12:37-12:40 堵塞窗口哪些请求挂起」只能靠行序倒推）。复制 uvicorn
        # 默认 LOGGING_CONFIG 只改 formatter，其余行为不变。
        try:
            from uvicorn.config import LOGGING_CONFIG as _UV_LOG

            _log_cfg = dict(_UV_LOG)
            _fmts = dict(_UV_LOG.get("formatters") or {})
            _acc = dict(_fmts.get("access") or {})
            _acc["fmt"] = "%(asctime)s - %(client_addr)s - \"%(request_line)s\" %(status_code)s"
            _acc.pop("use_colors", None)
            _def = dict(_fmts.get("default") or {})
            _def["fmt"] = "%(asctime)s %(levelprefix)s %(message)s"
            _def.pop("use_colors", None)
            _log_cfg["formatters"] = {**_fmts, "access": _acc, "default": _def}
        except Exception:  # uvicorn 版本差异时退回默认配置
            _log_cfg = None
        uvicorn.run(
            app, host=args.host, port=args.port,
            ws_max_size=_WS_MAX_FRAME_BYTES,
            log_config=_log_cfg,
            # 2026-10-13 13:45 面板全空事故根因：uvicorn 默认 keep-alive 5s，
            # 服务端先关空闲连接，WebView 在半死连接上发请求 → 卡到 TCP 超时
            # （实测 173s）→ 占满 WebView 6 连接槽 → 面板 GET 全排队 → 全空，
            # TCP 超时后排队请求一次性涌出 →「自动恢复」。必须远长于前端最长
            # 轮询间隔（30s），让服务端在会话期内永不先关。
            timeout_keep_alive=KEEP_ALIVE_SECONDS,
        )
    finally:
        if generated_token_path is not None:
            generated_token_path.unlink(missing_ok=True)
            os.environ.pop("COWORKER_API_TOKEN", None)


if __name__ == "__main__":
    main()
