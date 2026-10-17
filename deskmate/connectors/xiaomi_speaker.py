"""小爱音箱连接器 —— 小桔的「嘴」和「远场耳朵」（路线 A：MiService 云端协议，零刷机）。

设计要点（与 aicircle.py 一致的工程范式）：
- 复用 miservice 库（github.com/Yonsm/micom，MIT）：小米账号登录 + MiNA 小爱音箱协议，
  不在 deskmate 重复实现协议层。
- 凭据：小米账号一次性登录（OTP 交互），token 由 miservice 持久化 ~/.mi.token；
  设备与轮询参数存 ~/.config/coworker/mi_speaker.json。**不存密码**。
- 全部 fail-open：调用方吞异常、记日志，不影响主服务。

能力（miservice MiNAService API 已实测）：
- latest_asks()    归一化最新提问列表（request_id / timestamp_ms / question / answer）
- speak(text)      让小爱播报文本 —— 小桔的嘴
- play_url(url)    播放音频 URL（自定义 TTS 音色备用通道）
- stop_playback()  停止当前播放 —— 打断小爱原生应答（xiaogpt --mute_xiaoai 对策）

一次性登录（OTP 交互，只需跑一次；token 之后自动复用）。密码走 getpass
交互输入，不回显、不进 shell 历史——**不要把密码贴在命令行里**：
    python -m deskmate.connectors.xiaomi_speaker login
（也可 MI_PASS=xxx python -m ... login，但密码会留在 shell 历史中，不推荐）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_SID_MICOAPI = "micoapi"  # MiNA（小爱音箱）服务 sid，见 miservice README


def default_config_path() -> Path:
    return Path.home() / ".config" / "coworker" / "mi_speaker.json"


def load_speaker_config(path: Optional[Path] = None) -> dict:
    """读取小爱音箱配置。缺省 fail-closed（enabled=False）——没配好就不启动。"""
    cfg: dict[str, Any] = {
        "enabled": False,
        "did": "",  # 设备 ID 或设备名；留空 = 账号下只有一台时自动选
        "username": "",  # 小米账号（也可用 MI_USER 环境变量）
        "poll_interval": 1.5,
        "token_path": str(Path.home() / ".mi.token"),
    }
    p = Path(path) if path else default_config_path()
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key in cfg:
                    if key in data:
                        cfg[key] = data[key]
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[xiaomi-speaker] 配置解析失败，用缺省值: %s", e)
    if not cfg["username"]:
        cfg["username"] = os.environ.get("MI_USER", "")
    try:
        cfg["poll_interval"] = max(0.5, float(cfg["poll_interval"]))
    except (TypeError, ValueError):
        cfg["poll_interval"] = 1.5
    cfg["enabled"] = bool(cfg["enabled"])
    return cfg


class AskTracker:
    """提问去重 / 水位管理（纯逻辑，无 IO）。

    语义：
    - 首次 fresh() 只建基线（全部标记已处理、水位推到最高），返回 [] ——
      启动前的历史提问不重放，否则小桔会突然回答几天前的旧问题。
    - 之后放行「未处理过 且 时间戳不旧于水位」的提问；request_id 去重
      （轮询重复返回同一批时不重复应答）。
    - 空文本提问（误触发）标记已处理并推进水位，但不放行。
    - 同一批内的多条新问按时间升序全部放行（轮询间隔内的连问不丢）。
    """

    _MEMORY = 100  # 记住最近 N 个 request_id，防轮询重复放行

    def __init__(self) -> None:
        self._baseline = False
        self._last_ts = 0
        self._order: deque[str] = deque()
        self._seen: set[str] = set()

    def _mark(self, rid: str) -> None:
        if not rid or rid in self._seen:
            return
        self._order.append(rid)
        self._seen.add(rid)
        while len(self._order) > self._MEMORY:
            self._seen.discard(self._order.popleft())

    def fresh(self, asks: list[dict]) -> list[dict]:
        items = [a for a in (asks or []) if isinstance(a, dict)]
        if not items:
            return []
        first_call = not self._baseline
        self._baseline = True  # 首个非空轮次即建立基线
        items.sort(key=lambda a: int(a.get("timestamp_ms") or 0))
        out: list[dict] = []
        for a in items:
            rid = str(a.get("request_id") or "")
            ts = int(a.get("timestamp_ms") or 0)
            if ts > self._last_ts:
                self._last_ts = ts
            if rid in self._seen:
                continue
            self._mark(rid)
            if first_call:
                continue  # 基线轮：只标记，不放行
            if ts >= self._last_ts and str(a.get("question") or "").strip():
                out.append(a)
        return out


class XiaomiSpeakerConnector:
    """miservice 封装：登录 → 设备解析 → 归一化提问 → 播报。

    生产用法::

        conn = XiaomiSpeakerConnector(load_speaker_config())
        await conn.start()                 # 登录 + 解析 did（OTP 首次交互）
        asks = await conn.latest_asks()    # 归一化提问列表
        await conn.speak("萧哥你好")        # 小桔的嘴

    测试可注入 fake service：``XiaomiSpeakerConnector.__init__(service=fake)``。
    """

    def __init__(self, config: Optional[dict] = None, *, service: Any = None) -> None:
        self._cfg = config if config is not None else load_speaker_config()
        self._service = service  # MiNAService（或 fake）
        self._account = None
        self._did = str(self._cfg.get("did") or "")
        self._session = None  # aiohttp session（自建时才需要关）

    # -- 生命周期 ------------------------------------------------------------
    async def start(
        self,
        otp_callback: Optional[Callable[[str], Awaitable[str]]] = None,
    ) -> str:
        """登录小米账号并解析设备，返回 did。失败抛异常（调用方 fail-open）。"""
        if self._service is None:
            from miservice import MiAccount, MiNAService

            username = str(self._cfg.get("username") or os.environ.get("MI_USER") or "")
            password = os.environ.get("MI_PASS", "")
            if not username or not password:
                raise RuntimeError(
                    "未配置小米账号：mi_speaker.json 的 username + MI_PASS 环境变量"
                    "（一次性登录：MI_USER=.. MI_PASS=.. python -m "
                    "deskmate.connectors.xiaomi_speaker login）"
                )
            try:
                from aiohttp import ClientSession

                self._session = ClientSession()
            except ImportError:
                self._session = None  # miservice 自带 biohttp 回退
            # 注意：otp_callback 为 None 时**不传该参数**——miservice 的
            # MiAccount 默认值就是终端输入 otp_input，显式传 None 会把它
            # 覆盖掉，导致 CLI 登录需要验证码时直接抛异常（真实踩坑）。
            if otp_callback is not None:
                self._account = MiAccount(
                    self._session,
                    username,
                    password,
                    str(self._cfg.get("token_path") or (Path.home() / ".mi.token")),
                    otp_callback=otp_callback,
                )
            else:
                self._account = MiAccount(
                    self._session,
                    username,
                    password,
                    str(self._cfg.get("token_path") or (Path.home() / ".mi.token")),
                )
            self._service = MiNAService(self._account)
        await self._ensure_login()
        await self._resolve_device()
        return self._did

    async def _ensure_login(self) -> None:
        """确保有有效的 micoapi 服务 token。

        关键细节：miservice 的 ``login()`` 自身**不加载**持久化 token（懒加载只发生在
        ``mi_request`` 里）。若这里无脑显式 login，守护进程每次重启都会走密码 + OTP，
        而无终端环境下 OTP 必然 EOFError 失败。正确做法：先手动加载 token，有效则跳过登录。
        """
        if self._account is None:
            return  # 注入 service 的测试路径没有 account
        store = getattr(self._account, "token_store", None)
        if getattr(self._account, "token", None) is None and store is not None:
            try:
                self._account.token = await store.load_token()
            except Exception:
                pass
        token = getattr(self._account, "token", None) or {}
        if _SID_MICOAPI not in token:
            await self._account.login(_SID_MICOAPI)

    async def _resolve_device(self) -> None:
        devices = await self._service.device_list() or []
        if not devices:
            raise RuntimeError("小米账号下没有小爱音箱设备")
        want = self._did
        if want:
            for d in devices:
                if str(d.get("deviceID", "")) == want:
                    return
            for d in devices:
                if str(d.get("miotDID", "")) == want or str(d.get("name", "")) == want:
                    self._did = str(d.get("deviceID", ""))
                    return
            raise RuntimeError(f"找不到设备 {want!r}（账号下共 {len(devices)} 台）")
        if len(devices) == 1:
            self._did = str(devices[0].get("deviceID", ""))
            return
        names = "、".join(str(d.get("name", d.get("deviceID", "?"))) for d in devices)
        raise RuntimeError(f"账号下有多台设备（{names}），请在配置里指定 did")

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    async def __aenter__(self) -> "XiaomiSpeakerConnector":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # -- 能力 ----------------------------------------------------------------
    async def latest_asks(self) -> list[dict]:
        """归一化最新提问。字段：request_id / timestamp_ms / question / answer。"""
        raw = await self._service.get_latest_ask(self._did) or []
        out: list[dict] = []
        for m in raw:
            if not isinstance(m, dict):
                continue
            answers = (m.get("response") or {}).get("answer") or []
            question = ""
            answer = ""
            for a in answers:
                if not isinstance(a, dict):
                    continue
                question = question or str(a.get("question") or "")
                answer = answer or str(a.get("content") or "")
            out.append(
                {
                    "request_id": str(m.get("request_id") or ""),
                    "timestamp_ms": int(m.get("timestamp_ms") or 0),
                    "question": question.strip(),
                    "answer": answer.strip(),
                }
            )
        return out

    async def speak(self, text: str) -> bool:
        """让小爱播报文本（小桔的嘴）。"""
        return bool(await self._service.text_to_speech(self._did, text))

    async def play_url(self, url: str) -> bool:
        """播放音频 URL（自定义 TTS 音色备用通道）。"""
        return bool(await self._service.play_by_url(self._did, url))

    async def stop_playback(self) -> bool:
        """停止当前播放——打断小爱原生应答。"""
        return bool(await self._service.player_stop(self._did))


# ---------------------------------------------------------------------- 一次性登录 CLI
async def _interactive_login() -> int:
    """OTP 交互登录（终端输入验证码），成功后 token 落盘，之后无需再登。

    密码优先取 MI_PASS 环境变量；未提供时用 getpass 交互输入——
    不回显、不进 shell 历史，避免密码落在命令行里。
    """
    if not os.environ.get("MI_PASS", "").strip():
        import getpass

        pw = getpass.getpass("小米账号密码（输入不回显，不会留下记录）: ").strip()
        if pw:
            os.environ["MI_PASS"] = pw
    conn = XiaomiSpeakerConnector(load_speaker_config())
    try:
        did = await conn.start()
    except Exception as e:
        logger.exception("登录失败")
        print(f"登录失败: {e}")
        return 1
    finally:
        await conn.close()
    print(f"登录成功，设备 did={did}。token 已保存，语音通道可直接复用。")
    return 0


if __name__ == "__main__":  # python -m deskmate.connectors.xiaomi_speaker login
    import sys

    logging.basicConfig(level=logging.INFO)
    _cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if _cmd == "login":
        raise SystemExit(asyncio.run(_interactive_login()))
    print(__doc__)
    raise SystemExit(0)
