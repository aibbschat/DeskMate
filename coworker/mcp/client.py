"""MCPManager — our own thin async MCP client over the official `mcp` SDK.

Async-native (no `nest_asyncio`, no second event loop): each server runs in a dedicated
asyncio task that opens the transport + `ClientSession`, keeps them alive until shutdown,
then closes them in the *same* task — required because the SDK's transports use anyio cancel
scopes that must be entered and exited on one task. Tool calls are awaited from any task on
the same loop, which is safe.

Tool execution from the (sync) ToolRegistry bridges back here via
`run_coroutine_threadsafe` — see `coworker/mcp/tools.py`.
"""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import AsyncExitStack
from typing import Any, IO, Optional

import logging

logger = logging.getLogger(__name__)

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.sse import sse_client

from .config import MCPServerDef


_STDERR_TAIL_LINES = 20
_STDERR_TAIL_CHARS = 1500


def _read_tail(errfile: Optional[IO[str]]) -> Optional[str]:
    """Last few lines of a captured stderr file — the crash evidence, not the log."""
    if errfile is None:
        return None
    try:
        errfile.seek(0)
        text = errfile.read()
    except (OSError, ValueError):
        return None
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    return "\n".join(lines[-_STDERR_TAIL_LINES:])[-_STDERR_TAIL_CHARS:]


class _Conn:
    def __init__(self, session: ClientSession, tools: list[Any]) -> None:
        self.session = session
        self.tools = tools  # list[mcp.types.Tool]
        self.shutdown = asyncio.Event()


class MCPManager:
    """Owns persistent MCP connections keyed by server name; lazy-connects on demand."""

    def __init__(self, secrets: Any = None) -> None:
        self._conns: dict[str, _Conn] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stderr_tails: dict[str, str] = {}
        self._lock = asyncio.Lock()
        # SecretStore for OAuth servers' token persistence (mcp/oauth.py); lazy default
        # so library/CLI construction without secrets keeps working.
        self._secrets = secrets

    async def ensure(self, server: MCPServerDef, *, interactive: bool = False) -> _Conn:
        """Return a live connection for `server`, connecting (once) if needed.

        `interactive=True` (explicit connect actions only) lets an OAuth server run
        the browser sign-in flow; the default refuses it — stored tokens and silent
        refresh still work, but a server that insists on re-authorization raises
        InteractiveAuthRequired instead of hijacking the user's browser.
        """
        async with self._lock:
            existing = self._conns.get(server.name)
            if existing is not None:
                return existing
            ready: asyncio.Future = asyncio.get_running_loop().create_future()
            self._tasks[server.name] = asyncio.create_task(
                self._serve(server, ready, interactive=interactive)
            )
            conn = await ready  # propagates connection errors
            self._conns[server.name] = conn
            return conn

    async def tools(self, server: MCPServerDef) -> list[Any]:
        return (await self.ensure(server)).tools

    async def verify(self, server: MCPServerDef, *, interactive: bool = False) -> _Conn:
        """A REAL health check for explicit Test actions. `ensure` returns a cached
        connection untouched, which made Test-on-Live a silent no-op that could not
        detect a dead server (owner-hit 2026-08-21). Here a cached connection is
        round-tripped (tools/list, refreshing the tool set); a dead one is torn
        down and reconnected fresh."""
        conn = self._conns.get(server.name)
        if conn is not None:
            try:
                listed = await asyncio.wait_for(conn.session.list_tools(), timeout=20)
                conn.tools = list(listed.tools)
                return conn
            except Exception:
                conn.shutdown.set()
                task = self._tasks.pop(server.name, None)
                if task is not None:
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout=5)
                    except Exception:
                        task.cancel()
                self._conns.pop(server.name, None)  # _serve pops too; belt and braces
        return await self.ensure(server, interactive=interactive)

    def last_stderr(self, name: str) -> Optional[str]:
        """Stderr tail from the most recent failed startup of `name`, if any."""
        return self._stderr_tails.get(name)

    async def call(
        self,
        name: str,
        tool: str,
        arguments: Optional[dict[str, Any]],
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        conn = self._conns.get(name)
        if conn is None:
            raise RuntimeError(f"MCP server not connected: {name}")
        # PR18(#37)：可选协程级超时——wait_for 超时会真正 cancel 底层 call_tool，
        # 与 tools.py 的 future.cancel 形成双保险（前者释放连接、后者释放线程等待）。
        coro = conn.session.call_tool(tool, arguments or {})
        if timeout is not None and timeout > 0:
            try:
                result = await asyncio.wait_for(coro, timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(f"MCP tool {tool} timed out after {timeout:g}s") from None
        else:
            result = await coro
        return _result_payload(result)

    async def aclose(self) -> None:
        for conn in self._conns.values():
            conn.shutdown.set()
        for task in list(self._tasks.values()):
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (asyncio.TimeoutError, Exception):
                task.cancel()
        self._conns.clear()
        self._tasks.clear()

    async def reload_server(self, server: MCPServerDef, *, interactive: bool = False) -> _Conn:
        """Hot-swap a single server's live connection WITHOUT dropping others.

        Build-before-swap: spin up the new connection first; only swap
        ``_conns[name]`` and gracefully shut the old one down once the new is
        healthy. If the new connection fails, the old one is left untouched —
        the session keeps working on the previous config instead of going dark.
        This is what makes "add an external server → it's usable in the running
        roundtable without a restart" possible.
        """
        async with self._lock:
            # Pull the live connection out of the slot (under the lock) so the
            # throwaway task's `finally` pop is a no-op, and a failure can simply
            # restore `old`. No other coroutine can `ensure` this name while we
            # hold the lock, so the slot stays empty only for the build window.
            old = self._conns.pop(server.name, None)
            ready: asyncio.Future = asyncio.get_running_loop().create_future()
            task = asyncio.create_task(
                self._serve(server, ready, interactive=interactive)
            )
            self._tasks[server.name] = task
            try:
                new_conn = await ready  # propagates connection errors
            except Exception:
                # Restore the previous live connection — session keeps working.
                if old is not None:
                    self._conns[server.name] = old
                raise
            # Swap: the new connection takes the slot; signal the old to close.
            self._conns[server.name] = new_conn
            if old is not None:
                old.shutdown.set()
            return new_conn

    async def reload_all(self, servers: list[MCPServerDef]) -> None:
        """Hot-swap every known server at once (config changed on disk).

        Build-before-swap per server; a server whose new connect fails keeps its
        prior live connection (logged, not fatal) so one bad entry can't take
        the whole tool surface offline.
        """
        for server in servers:
            try:
                await self.reload_server(server)
            except Exception as exc:  # noqa: BLE001
                logger.warning("MCP reload failed for %s (kept old): %s", server.name, exc)

    # -- per-server lifecycle (one task owns enter+exit) ------------------------
    async def _serve(
        self, server: MCPServerDef, ready: asyncio.Future, *, interactive: bool = False
    ) -> None:
        errfile = None
        try:
            async with AsyncExitStack() as stack:
                if server.transport in ("http", "sse"):
                    if not server.url:
                        raise ValueError(
                            f"MCP server '{server.name}' is {server.transport} but has no url"
                        )
                    auth = None
                    if server.auth == "oauth":
                        from ..secrets import SecretStore
                        from .oauth import build_auth

                        if self._secrets is None:
                            self._secrets = SecretStore()
                        auth = build_auth(
                            server.name,
                            server.url,
                            self._secrets,
                            interactive=interactive,
                        )
                    if server.transport == "http":
                        read, write, *_ = await stack.enter_async_context(
                            streamablehttp_client(
                                server.url, headers=server.headers or None, auth=auth
                            )
                        )
                    else:  # sse — Server-Sent Events transport (Tencent Cloud
                        # hosted MCP, OpenAI/Anthropic SSE endpoints, etc.)
                        read, write, *_ = await stack.enter_async_context(
                            sse_client(
                                server.url, headers=server.headers or None, auth=auth
                            )
                        )
                else:
                    if not server.command:
                        raise ValueError(
                            f"MCP server '{server.name}' is stdio but has no command"
                        )
                    params = StdioServerParameters(
                        command=server.command,
                        args=server.args,
                        env=server.env or None,
                        cwd=server.cwd,
                    )
                    # Capture the child's stderr so a startup crash leaves evidence
                    # the UI can show (the SDK needs a real file descriptor here).
                    errfile = tempfile.TemporaryFile(
                        mode="w+", encoding="utf-8", errors="replace"
                    )
                    read, write = await stack.enter_async_context(
                        stdio_client(params, errlog=errfile)
                    )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = await session.list_tools()
                conn = _Conn(session, list(listed.tools))
                self._stderr_tails.pop(server.name, None)
                if not ready.done():
                    ready.set_result(conn)
                await conn.shutdown.wait()
        except Exception as exc:  # connection / init failure
            tail = _read_tail(errfile)
            if tail:
                self._stderr_tails[server.name] = tail
            if not ready.done():
                ready.set_exception(exc)
        finally:
            if errfile is not None:
                try:
                    errfile.close()
                except OSError:
                    pass
            # Only the task that still OWNS the slot may vacate it. During a
            # hot-swap (reload_server), a superseded task finishes its teardown
            # *after* the new connection has already taken the slot — an
            # unconditional pop here would orphan the brand-new connection and
            # force a duplicate connect on next use.
            if self._tasks.get(server.name) is asyncio.current_task():
                self._conns.pop(server.name, None)
                self._tasks.pop(server.name, None)


def _result_payload(result: Any) -> Any:
    """Flatten a CallToolResult into something the engine can serialize for the model."""
    texts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            texts.append(text)
        else:  # non-text content (image/resource) — describe it
            texts.append(f"[{getattr(block, 'type', 'content')}]")
    body = "\n".join(texts)
    if getattr(result, "isError", False):
        return {"error": body or "MCP tool error"}
    structured = getattr(result, "structuredContent", None)
    if structured is not None and not body:
        return structured
    return body
