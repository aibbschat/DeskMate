"""Launch the server with uvicorn. Used by the desktop GUI sidecar and `openworker-server`."""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

from ..config import load_config
from ..orphan import _exit_when_orphaned
from ..permissions import Mode
from ..secrets import _load_dotenv, state_dir, write_private_text
from .app import _WS_MAX_FRAME_BYTES, create_app
from .manager import SessionManager


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
    """Auto-launch the DeskMate selection adapter as a child of this sidecar.

    This is what makes the select-text popup (OpenAI Translator etc.) work the moment the
    app opens — no manual ``python -m deskmate.selection`` needed. We hand the adapter our
    live port + in-memory token via env (the discovery layer prefers env over the on-disk
    token file), and tie its lifetime to ours so it exits together with the app.

    Adapter startup failures are never fatal to the sidecar.
    """
    if os.environ.get("DESKMATE_SELECTION_DISABLE") == "1":
        return
    try:
        import subprocess

        env = dict(os.environ)
        env["COWORKER_PORT"] = str(port)
        env["COWORKER_API_TOKEN"] = os.environ.get("COWORKER_API_TOKEN", "")
        # Tie the adapter's life to THIS sidecar: it watches our PID and exits when we do.
        env["COWORKER_EXIT_WITH_PARENT"] = "1"
        env["COWORKER_PARENT_PID"] = str(os.getpid())
        subprocess.Popen(
            [sys.executable, "-m", "deskmate.selection"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=False,
        )
    except Exception:
        pass  # never block sidecar startup on adapter failure


def main(argv=None) -> None:
    _load_env_file()
    _ensure_ca_bundle()
    cfg = load_config()  # global config supplies defaults
    parser = argparse.ArgumentParser(prog="openworker-server")
    parser.add_argument("--cwd", default=None, help="optional seed/default workspace")
    parser.add_argument("--model", default=cfg.model)
    parser.add_argument(
        "--mode",
        default=cfg.mode,
        choices=["discuss", "plan", "interactive", "auto"],
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
        uvicorn.run(
            app, host=args.host, port=args.port, ws_max_size=_WS_MAX_FRAME_BYTES
        )
    finally:
        if generated_token_path is not None:
            generated_token_path.unlink(missing_ok=True)
            os.environ.pop("COWORKER_API_TOKEN", None)


if __name__ == "__main__":
    main()
