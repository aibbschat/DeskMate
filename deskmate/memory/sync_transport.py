"""小桔记忆 · B 类同步的传输层（阶段 1 · 第②项）。

对应架构文档 §7.3 / §八：B 类走「自建 oplog over SSH 反隧道」，语义精确、
复用现有隧道、增量小；底层传输与 oplog 协议解耦，本模块只负责「把对端 oplog
拉/推过去」。

拓扑（两端只拨出、不公网监听，服务器当会合点）：
- Mac 常驻 daemon（local-first 主场），在本机 ``127.0.0.1:<local_port>`` 起一个
  oplog HTTP 端点（仅回环，不外泄）。
- Mac 拨出一条 SSH 会话：
  * 反隧道 ``-R 127.0.0.1:<remote_port>:127.0.0.1:<local_port>``：云端
    ``127.0.0.1:<remote_port>`` 转发到 Mac 的端点 → 云端 daemon 连自己回环即达 Mac；
  * 本地转发 ``-L 127.0.0.1:<fwd_port>:127.0.0.1:<remote_port>``：Mac
    ``127.0.0.1:<fwd_port>`` 转发到云端端点 → Mac daemon 连自己回环即达云端。
- 两端 daemon 的同步都只打**各自回环地址**，SSH 会话是唯一出网通道且需鉴权；
  同步端口从不在任何一端公网监听。

安全红线（单测强制）：
- 反隧道远端绑定必须是 ``127.0.0.1``（绝不 ``0.0.0.0``），否则云端端口会被公网听到；
- HTTP oplog 服务只绑 ``127.0.0.1``；
- 隧道进程用 ``-N -o ExitOnForwardFailure=yes -o ServerAliveInterval/CountMax``，
  断线即退出，由守护层 fail-open 重拨。

本模块不碰任何生产库；``HttpOplogServer`` 接的是内存/SQLite 的 ``OplogStore``，
由调用方注入。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .sync_daemon import OplogStore, Op, Transport


# ---------------------------------------------------------------------------
# 1. 对端 oplog 的 HTTP 端点（仅回环，供隧道两端互访）
# ---------------------------------------------------------------------------


class _OplogHandler(BaseHTTPRequestHandler):
    """GET /oplog?after=<seq> 拉增量；GET /snapshot?table=X 拉某表存量快照；
    POST /oplog 推一批 Op（JSON 数组）。"""

    server_version = "XiaoJuOplog/1.0"

    def _ops_from_body(self) -> list[Op]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return []
        raw = self.rfile.read(length)
        arr = json.loads(raw)
        return [Op.from_json(s) for s in arr]

    def do_GET(self) -> None:  # noqa: N802  (stdlib 命名约定)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/oplog":
            self._get_oplog(parsed)
        elif parsed.path == "/snapshot":
            self._get_snapshot(parsed)
        else:
            self.send_error(404)

    def _get_oplog(self, parsed) -> None:
        try:
            after = int(urllib.parse.parse_qs(parsed.query).get("after", ["0"])[0])
        except (TypeError, ValueError):
            after = 0
        ops = self.server.oplog.since(after)  # type: ignore[attr-defined]
        body = ("[" + ",".join(o.to_json() for o in ops) + "]").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_snapshot(self, parsed) -> None:
        """首连基线对账用：返回某同步表全部**活**记录的快照（过滤已删/已过期）。

        与 SyncEngine.reconcile_baseline_oplog 的消费端（live_items 语义）保持一致：
        不把已过期（valid_until 早于现在）或已删记录当活记录推给对端，避免首连基线
        把过期记录当活记录传播、对账口径对不上。
        """
        table = urllib.parse.parse_qs(parsed.query).get("table", [""])[0]
        stores = getattr(self.server, "stores", None)  # type: ignore[attr-defined]
        if not stores or table not in stores:
            self.send_error(404)
            return
        recs = []
        for r in stores[table].live_items():
            recs.append(
                {
                    "record_id": r.record_id,
                    "data": r.data,
                    "hlc": r.hlc.to_str(),
                    "tombstone": r.tombstone,
                    "valid_until": r.valid_until,
                }
            )
        body = json.dumps(recs, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/oplog":
            self.send_error(404)
            return
        try:
            ops = self._ops_from_body()
        except (json.JSONDecodeError, ValueError):
            self.send_error(400)
            return
        oplog: OplogStore = self.server.oplog  # type: ignore[attr-defined]
        for op in ops:
            oplog.append(op)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args) -> None:  # 静默：同步流量很大，不刷 stderr
        return


class HttpOplogServer:
    """把本端 ``OplogStore`` 暴露成回环 HTTP 端点。

    ``stores``（可选）：``{表名: store}``，供 ``/snapshot?table=X`` 首连基线对账拉
    本端某表存量快照。不传则 ``/snapshot`` 返回 404。
    """

    def __init__(
        self,
        oplog: OplogStore,
        host: str = "127.0.0.1",
        port: int = 8765,
        stores: Optional[dict] = None,
    ) -> None:
        self.oplog = oplog
        self.host = host
        self.port = port
        self.stores = stores
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> int:
        """启动；返回实际监听端口（port=0 时由系统分配）。"""
        self._httpd = ThreadingHTTPServer((self.host, self.port), _OplogHandler)
        self._httpd.oplog = self.oplog  # type: ignore[attr-defined]
        self._httpd.stores = self.stores  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self.port = self._httpd.server_address[1]
        return self.port

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


# ---------------------------------------------------------------------------
# 2. HTTP 传输（Transport 实现；替代骨架的 InMemoryTransport）
# ---------------------------------------------------------------------------


class HttpOplogTransport(Transport):
    """经 HTTP 拉/推对端 oplog。配合 SSH 反隧道时 ``base_url`` 即隧道另一端的回环地址。"""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def pull_since(self, after_seq: int) -> list[Op]:
        url = f"{self.base_url}/oplog?after={after_seq}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 (回环/隧道内受控)
            data = resp.read()
        return [Op.from_json(s) for s in json.loads(data)]

    def push(self, ops: list[Op]) -> None:
        if not ops:
            return
        body = ("[" + ",".join(o.to_json() for o in ops) + "]").encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/oplog",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            resp.read()

    def pull_snapshot(self, table: str) -> list:
        """首连基线对账：拉对端某同步表的存量快照（记录 dict 列表）。"""
        url = f"{self.base_url}/snapshot?table={urllib.parse.quote(table)}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            return json.loads(resp.read())


# ---------------------------------------------------------------------------
# 3. SSH 反隧道管理（构建命令行 + 拉起进程；fail-open 由守护层接管）
# ---------------------------------------------------------------------------


class SshTunnel:
    """构建并管理一条「Mac 拨出、两端只回环」的 SSH 反隧道。

    反隧道让云端 ``<remote_bind>:<remote_port>`` 转发到 Mac ``127.0.0.1:<local_port>``；
    本地转发让 Mac ``127.0.0.1:<local_forward_port>`` 转发到云端 ``127.0.0.1:<remote_port>``。
    两者同属一条 SSH 会话（Mac 发起），云端不同步端口公网监听。
    """

    def __init__(
        self,
        host: str,
        user: str,
        ssh_port: int = 22,
        local_port: int = 8765,
        remote_port: int = 8765,
        local_forward_port: int = 8766,
        remote_bind: str = "127.0.0.1",  # 安全红线：远端绑定地址只绑回环，绝不 0.0.0.0
        remote_bind_port: int = 8765,  # 反隧道在云端绑定的回环端口；runner 显式设 18765 以避开云端 server 已占的 8765
        identity_file: Optional[str] = None,
        # 保活间隔必须短于中间网络/NAT 的空闲超时（实测萧哥侧约 15s 即踢空闲连接），
        # 故默认 10s 发保活、3 次无响应(30s)才判定断链，避免隧道反复秒断重拨。
        server_alive_interval: int = 10,
        server_alive_count_max: int = 3,
    ) -> None:
        self.host = host
        self.user = user
        self.ssh_port = ssh_port
        self.local_port = local_port
        self.remote_port = remote_port
        self.local_forward_port = local_forward_port
        self.remote_bind = remote_bind
        self.remote_bind_port = remote_bind_port
        self.identity_file = identity_file
        self.server_alive_interval = server_alive_interval
        self.server_alive_count_max = server_alive_count_max
        self._proc: Optional[subprocess.Popen] = None

    def build_args(self) -> list[str]:
        """返回待执行的 ssh 命令行（纯函数，便于单测核验安全属性）。"""
        args = [
            "ssh",
            "-N",
            "-o",
            f"ExitOnForwardFailure=yes",
            "-o",
            f"ServerAliveInterval={self.server_alive_interval}",
            "-o",
            f"ServerAliveCountMax={self.server_alive_count_max}",
        ]
        if self.ssh_port != 22:
            args += ["-p", str(self.ssh_port)]
        if self.identity_file:
            args += ["-i", self.identity_file]
        # 反隧道：远端 remote_bind:remote_bind_port -> 本端 127.0.0.1:local_port
        args += ["-R", f"{self.remote_bind}:{self.remote_bind_port}:127.0.0.1:{self.local_port}"]
        # 本地转发：本端 127.0.0.1:local_forward_port -> 远端 127.0.0.1:remote_port
        args += ["-L", f"127.0.0.1:{self.local_forward_port}:127.0.0.1:{self.remote_port}"]
        args += [f"{self.user}@{self.host}"]
        return args

    def start(self) -> subprocess.Popen:
        """拉起隧道进程（fail-open：调用方需自行捕获异常，本方法不吞错）。"""
        self._proc = subprocess.Popen(
            self.build_args(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return self._proc

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None


# ---------------------------------------------------------------------------
# 4. 游标持久化（整数 seq；重拨/重启不丢、不重拉；原子写）
# ---------------------------------------------------------------------------


class CursorStore:
    """把 ``pull_after_seq`` / ``push_after_seq`` 持久化到 JSON，原子写、损坏 fail-open 归零。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.pull_after_seq = 0
        self.push_after_seq = 0
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.pull_after_seq = int(d.get("pull_after_seq", 0))
            self.push_after_seq = int(d.get("push_after_seq", 0))
        except Exception:
            # 损坏游标：宁可从 0 重拉（安全），也不拿错误游标推进
            self.pull_after_seq = 0
            self.push_after_seq = 0

    def save(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "pull_after_seq": self.pull_after_seq,
                    "push_after_seq": self.push_after_seq,
                    "updated_at": int(time.time_ns() // 1_000_000),
                },
                f,
                ensure_ascii=False,
            )
        os.replace(tmp, self.path)  # 原子替换
