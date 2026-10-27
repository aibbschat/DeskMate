"""小桔的 QQ 好友通道（私聊版）—— OneBot 11 正向 WebSocket。

链路：QQ 好友私聊 → NapCat（OneBot 11 正向 WS）→ 本模块
    → 合并连发（等对方把话说完）→ run_turn（app 注入，小桔完整管线）
    → 按句末标点拆成几条 + 随机延迟 → send_private_msg。

约定（与 xiaomi_voice.py 同一范式）：
- 只做私聊；群消息一律忽略（不进群）。
- 只理白名单里的 QQ 号；白名单为空 = 谁都不理（安全默认）。
- 全部 fail-open：断线自动重连、单条失败只记日志，循环不死。
- run_turn 与 WebSocket 都可替换，测试用 fake 驱动全流程。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("deskmate.qq_friend")

RunTurn = Callable[[str], Awaitable[str]]
Connector = Callable[[], Awaitable[Any]]

DEFAULT_WS_URL = "ws://127.0.0.1:3001"
CONFIG_PATH = Path.home() / ".config" / "coworker" / "qq_friend.json"

_END_CHARS = "。！？!?…～~"
_SEGMENT_PLACEHOLDERS = {
    "image": "[图片]",
    "face": "[表情]",
    "mface": "[表情包]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "json": "[卡片]",
    "forward": "[合并转发]",
}


# -- 纯函数：可单测，不碰网络 ------------------------------------------------


def message_segments_to_text(segments: Any) -> str:
    """OneBot 消息段数组 → 纯文本（图片/表情等降级成占位符）。"""
    if isinstance(segments, str):
        return segments.strip()
    parts: list[str] = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        kind = seg.get("type") or ""
        data = seg.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "at":
            parts.append("@" + str(data.get("qq") or ""))
        else:
            parts.append(_SEGMENT_PLACEHOLDERS.get(kind, "[" + str(kind) + "]"))
    return "".join(parts).strip()


@dataclass
class InboundDM:
    user_id: str
    text: str
    message_id: Optional[str] = None
    nickname: str = ""


def parse_private_message(raw: dict) -> Optional[InboundDM]:
    """OneBot 事件 → 好友私聊；群消息、自己发的、空消息都返回 None。"""
    if not isinstance(raw, dict) or raw.get("post_type") != "message":
        return None
    if raw.get("message_type") != "private":
        return None
    if str(raw.get("user_id")) == str(raw.get("self_id")):
        return None
    text = message_segments_to_text(raw.get("message"))
    if not text:
        return None
    sender = raw.get("sender") or {}
    nickname = str(sender.get("nickname") or sender.get("card") or "")
    message_id = raw.get("message_id")
    return InboundDM(
        user_id=str(raw.get("user_id")),
        text=text,
        message_id=str(message_id) if message_id is not None else None,
        nickname=nickname,
    )


def split_for_chat(text: str, max_parts: int = 4, min_len: int = 4) -> list[str]:
    """把一段回复拆成几条短消息：逐句断开、太短的并入上一条、条数封顶。"""
    text = (text or "").strip()
    if not text:
        return []
    pieces: list[str] = []
    buf = ""
    total = len(text)
    for index, ch in enumerate(text):
        if ch == "\n":
            if buf.strip():
                pieces.append(buf.strip())
            buf = ""
            continue
        buf += ch
        # 英文句号只在后面是空白或行尾时断，避开 3.5 / e.g 这类
        ascii_period = ch == "." and (index + 1 >= total or text[index + 1] in " \t\n")
        if ch in _END_CHARS or ascii_period:
            if buf.strip():
                pieces.append(buf.strip())
            buf = ""
    if buf.strip():
        pieces.append(buf.strip())
    merged: list[str] = []
    for piece in pieces:
        if merged and len(piece) <= min_len:
            merged[-1] = merged[-1] + piece
        else:
            merged.append(piece)
    if len(merged) > max_parts:
        merged = merged[: max_parts - 1] + ["".join(merged[max_parts - 1 :])]
    return merged

# -- 表情：把 [[emote:x]] / ![名](emote:x) 转成 QQ 图片 ---------------------

# 前端/模型输出里的两种表情占位符写法（与 coworker/emotes.py 保持一致）
_EMOTE_MARKER_RE = re.compile(
    r"!\[[^\]]*\]\(emote:(?P<id1>[A-Za-z0-9_-]+)(?::[A-Za-z0-9_-]+)?\)"
    r"|\[\[emote:(?P<id2>[A-Za-z0-9_-]+)(?::[A-Za-z0-9_-]+)?\]\]"
)

_EMOTE_ASSET_DIRS = (
    Path(__file__).resolve().parents[2] / "surfaces" / "gui" / "src" / "assets" / "emotes",
    Path.home() / ".deskmate" / "assets" / "emotes",
)


def split_emotes(text: str) -> list[tuple[str, str]]:
    """把一段回复拆成 text / emote 交替的片段；emote 段的值是表情 id。"""
    text = text or ""
    parts: list[tuple[str, str]] = []
    pos = 0
    for match in _EMOTE_MARKER_RE.finditer(text):
        if match.start() > pos:
            chunk = text[pos : match.start()]
            if chunk.strip():
                parts.append(("text", chunk))
        name = match.group("id1") or match.group("id2") or ""
        if name:
            parts.append(("emote", name))
        pos = match.end()
    if pos < len(text):
        tail = text[pos:]
        if tail.strip():
            parts.append(("text", tail))
    return parts


def emote_image_path(emote_id: str) -> Optional[Path]:
    """表情 id → 本地贴图路径；找不到就返回 None（宁可不发，也不发字符）。"""
    name = str(emote_id or "").strip().lower()
    if not name:
        return None
    for directory in _EMOTE_ASSET_DIRS:
        for ext in (".webp", ".png", ".gif", ".jpg"):
            candidate = directory / (name + ext)
            if candidate.is_file():
                return candidate
    return None


def _normalize_image(raw: bytes) -> Optional[bytes]:
    """压成 512px 内的 PNG：2048 的原图转 PNG 会胀到几 MB，QQ 上传又慢又容易失败。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            picture = image.convert("RGBA")
            if max(picture.size) > 512:
                picture.thumbnail((512, 512))
            buffer = io.BytesIO()
            picture.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    except Exception:
        return None


def _image_base64_payload(path: str | Path) -> Optional[str]:
    """本地图片 → base64:// 片段。

    NapCat 跑在 QQ 的沙盒里，读不到我们仓库/配置目录的路径（EPERM），
    所以图片必须由我们读出来、以 base64 塞进消息段。
    """
    file_path = Path(path).expanduser()
    try:
        raw = file_path.read_bytes()
    except OSError:
        logger.warning("qq friend: 读取图片失败 %s", file_path, exc_info=True)
        return None
    normalized = _normalize_image(raw)
    if normalized is not None:
        raw = normalized
    return "base64://" + base64.b64encode(raw).decode()


# -- 连发合并 ----------------------------------------------------------------


@dataclass
class _Pending:
    text: str
    first_ts: float
    last_ts: float


class _Debouncer:
    """同一人连发时等他把话说完：静默 quiet 秒就打包，最长 max_wait 秒兜底。"""

    def __init__(self, quiet: float = 2.5, max_wait: float = 30.0) -> None:
        self._quiet = quiet
        self._max_wait = max_wait
        self._pending: dict[str, _Pending] = {}

    def add(self, user_id: str, text: str, now: float) -> None:
        cur = self._pending.get(user_id)
        if cur is None:
            self._pending[user_id] = _Pending(text=text, first_ts=now, last_ts=now)
            return
        cur.text = cur.text + "\n" + text
        cur.last_ts = now

    def pop_ready(self, now: float) -> list[tuple[str, str]]:
        ready = [
            (uid, item.text)
            for uid, item in self._pending.items()
            if (now - item.last_ts) >= self._quiet or (now - item.first_ts) >= self._max_wait
        ]
        for uid, _ in ready:
            self._pending.pop(uid, None)
        return ready


# -- 通道 --------------------------------------------------------------------


class QQFriendChannel:
    """一个 NapCat 正向 WS 连接 = 小桔的一个 QQ 好友入口（只私聊）。"""

    def __init__(
        self,
        run_turn: RunTurn,
        *,
        ws_url: str = DEFAULT_WS_URL,
        access_token: str = "",
        owners: tuple[str, ...] | list[str] = (),
        quiet_seconds: float = 2.5,
        max_wait_seconds: float = 30.0,
        reply_delay: tuple[float, float] = (0.8, 2.5),
        max_parts: int = 4,
        poll_seconds: float = 0.2,
        reconnect_seconds: float = 5.0,
        action_timeout: float = 10.0,
        connector: Optional[Connector] = None,
    ) -> None:
        self._run_turn = run_turn
        self._ws_url = ws_url
        self._access_token = access_token
        self._owners = {str(o).strip() for o in owners if str(o).strip()}
        self._reply_delay = reply_delay
        self._max_parts = max_parts
        self._poll_seconds = poll_seconds
        self._reconnect_seconds = reconnect_seconds
        self._action_timeout = action_timeout
        self._connector = connector or self._default_connector
        self._debouncer = _Debouncer(quiet_seconds, max_wait_seconds)

        self._ws: Any = None
        self._running = False
        self._drain_task: Optional[asyncio.Task] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._seq = 0

    # -- 生命周期 --
    @property
    def running(self) -> bool:
        return self._running

    async def serve(self) -> None:
        """连上就跑；断了按 reconnect_seconds 重连，直到 stop()。"""
        self._running = True
        self._drain_task = asyncio.create_task(self._drain())
        try:
            while self._running:
                try:
                    self._ws = await self._connector()
                    logger.info("qq friend channel connected: %s", self._ws_url)
                    await self._listen()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("qq friend channel disconnected: %s", exc)
                finally:
                    await self._close_ws()
                if self._running:
                    await asyncio.sleep(self._reconnect_seconds)
        finally:
            self._running = False
            if self._drain_task is not None:
                self._drain_task.cancel()

    def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                asyncio.get_running_loop().create_task(self._close_ws())
            except RuntimeError:
                pass
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()

    # -- 连接 --
    async def _default_connector(self) -> Any:
        import websockets

        kwargs: dict[str, Any] = {
            "ping_interval": 20,
            "ping_timeout": 20,
            "max_size": 8 * 1024 * 1024,
            "open_timeout": 10,
        }
        if self._access_token:
            headers = {"Authorization": "Bearer " + self._access_token}
            try:
                return await websockets.connect(
                    self._ws_url, additional_headers=headers, **kwargs
                )
            except TypeError:  # websockets < 13
                return await websockets.connect(
                    self._ws_url, extra_headers=headers, **kwargs
                )
        return await websockets.connect(self._ws_url, **kwargs)

    async def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:
            logger.debug("close ws failed", exc_info=True)

    async def _listen(self) -> None:
        ws = self._ws
        async for raw in ws:
            try:
                self._handle_frame(raw)
            except Exception:
                logger.exception("qq frame handling failed")

    def _handle_frame(self, raw: Any) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        echo = data.get("echo")
        if echo is not None:
            fut = self._pending.pop(str(echo), None)
            if fut is not None and not fut.done():
                fut.set_result(data)
            return
        inbound = parse_private_message(data)
        if inbound is None:
            return
        if not self._is_owner(inbound.user_id):
            logger.info("qq friend: ignore %s (not in owners)", inbound.user_id)
            return
        self._debouncer.add(inbound.user_id, inbound.text, asyncio.get_running_loop().time())

    def _is_owner(self, user_id: str) -> bool:
        return bool(self._owners) and user_id in self._owners

    # -- 收 → 想 → 回 --
    async def _drain(self) -> None:
        while self._running:
            now = asyncio.get_running_loop().time()
            for user_id, text in self._debouncer.pop_ready(now):
                await self._answer(user_id, text)
            await asyncio.sleep(self._poll_seconds)

    async def _answer(self, user_id: str, text: str) -> None:
        try:
            reply = await self._run_turn(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("qq friend: run_turn failed")
            return
        if not (reply or "").strip():
            return
        await self.send_reply(user_id, reply)

    async def send_reply(self, user_id: str, text: str) -> None:
        """表情转图片、文字分句、随机延迟——一条条发出去。"""
        actions: list[tuple[str, str]] = []
        for kind, value in split_emotes(text):
            if kind == "emote":
                path = emote_image_path(value)
                if path is not None:
                    actions.append(("image", str(path)))
                continue
            for part in split_for_chat(value, max_parts=self._max_parts):
                actions.append(("text", part))
        for index, (kind, value) in enumerate(actions[:6]):
            if kind == "image":
                await self.send_image(user_id, value)
            else:
                await self.send_text(user_id, value)
            if index < len(actions[:6]) - 1:
                await asyncio.sleep(random.uniform(*self._reply_delay))

    async def send_text(self, user_id: str, text: str) -> None:
        response = await self._call_action(
            "send_private_msg",
            {
                "user_id": _as_uin(user_id),
                "message": [{"type": "text", "data": {"text": text}}],
            },
        )
        _log_failure(response, "send_text")

    async def send_image(self, user_id: str, path: str | Path) -> None:
        """发本地图片（表情包）；读不到就跳过，绝不发空消息。"""
        payload = _image_base64_payload(path)
        if payload is None:
            return
        response = await self._call_action(
            "send_private_msg",
            {
                "user_id": _as_uin(user_id),
                "message": [{"type": "image", "data": {"file": payload}}],
            },
        )
        _log_failure(response, "send_image")

    async def _call_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        ws = self._ws
        if ws is None:
            raise RuntimeError("qq ws not connected")
        self._seq += 1
        echo = str(self._seq)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        try:
            await ws.send(
                json.dumps(
                    {"action": action, "params": params, "echo": echo}, ensure_ascii=False
                )
            )
            return await asyncio.wait_for(fut, timeout=self._action_timeout)
        finally:
            self._pending.pop(echo, None)


def _log_failure(response: Any, what: str) -> None:
    """OneBot 把错误放在 status/retcode 里，HTTP 层永远是 200 —— 必须自己看。"""
    if not isinstance(response, dict):
        return
    if response.get("status") == "ok" and response.get("retcode", 0) == 0:
        return
    logger.warning("qq friend: %s failed: %s", what, json.dumps(response, ensure_ascii=False)[:300])


def _as_uin(user_id: str) -> Any:
    raw = str(user_id).strip()
    return int(raw) if raw.isdigit() else raw


def build_qq_channel(
    run_turn: RunTurn, config_path: Optional[Path] = None
) -> Optional[QQFriendChannel]:
    """从 ~/.config/coworker/qq_friend.json（+ 环境变量覆盖）造通道；没配 ws_url 返回 None。"""
    path = Path(config_path) if config_path else CONFIG_PATH
    cfg: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            logger.exception("qq_friend.json 解析失败：%s", path)
            return None
        if not isinstance(loaded, dict):
            logger.warning("qq_friend.json 不是对象：%s", path)
            return None
        cfg = loaded
    ws_url = str(os.environ.get("QQ_FRIEND_WS_URL") or cfg.get("ws_url") or "").strip()
    if not ws_url:
        logger.info("QQ 好友通道未配置 ws_url，跳过")
        return None
    token = str(os.environ.get("QQ_FRIEND_TOKEN") or cfg.get("access_token") or "")
    raw_owners = os.environ.get("QQ_FRIEND_OWNERS") or cfg.get("owners") or []
    if isinstance(raw_owners, str):
        owners = [piece.strip() for piece in raw_owners.split(",") if piece.strip()]
    else:
        owners = [str(piece).strip() for piece in raw_owners if str(piece).strip()]
    return QQFriendChannel(
        run_turn,
        ws_url=ws_url,
        access_token=token,
        owners=owners,
        quiet_seconds=float(cfg.get("quiet_seconds", 2.5)),
        max_wait_seconds=float(cfg.get("max_wait_seconds", 30.0)),
        max_parts=int(cfg.get("max_parts", 4)),
        reply_delay=(
            float(cfg.get("reply_delay_min", 0.8)),
            float(cfg.get("reply_delay_max", 2.5)),
        ),
    )


def qq_channel_enabled(config_path: Optional[Path] = None) -> bool:
    """通道是否该启动：显式开关优先，否则「有配置文件就启」——省掉环境变量。"""
    flag = os.environ.get("QQ_FRIEND_ENABLE", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    path = Path(config_path) if config_path else CONFIG_PATH
    return path.exists()
