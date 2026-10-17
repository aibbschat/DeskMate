"""小爱音箱连接器 —— 小桔的「嘴」和「远场耳朵」（路线 A：MiService 云端协议，零刷机）。

设计要点（与 aicircle.py 一致的工程范式）：
- 复用 miservice 库（github.com/Yonsm/micom，MIT）：小米账号登录 + MiNA 小爱音箱协议，
  不在 deskmate 重复实现协议层。
- 凭据：小米账号一次性登录（OTP 交互），token 由 miservice 持久化 ~/.mi.token；
  设备与轮询参数存 ~/.config/coworker/mi_speaker.json。**不存密码**。
- 全部 fail-open：调用方吞异常、记日志，不影响主服务。

能力（miservice MiNAService API 已实测）：
- conversations()   MiNA 对话记录（userprofile.mina.mi.com，mi-gpt 同款路线）
- latest_asks()    归一化最新提问列表（request_id / timestamp_ms / question / answer）
- speak(text)      让小爱播报文本 —— 小桔的嘴
- play_url(url)    播放音频 URL（自定义 TTS 音色备用通道）
- stop_playback()  停止当前播放 —— 彻底停止（player_stop）
- mute_xiaoai()    静音小爱原生应答 —— xiaogpt --mute_xiaoai 同款（先查
                   状态 status==1 再 player_pause；pause 不用 stop）

提问数据源说明（S12 真机实测的坑）：对话记录走 userprofile.mina.mi.com
用户画像服务（conversations），**不是** miservice 的 get_latest_ask
（api2 ubus nlp_result_get）——后者在 S12 老固件上设备端返 1001 空数据。
hardware 参数（如 S12A）从 device_list 解析时缓存，该接口必填。

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
import uuid
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
        self._hardware = ""  # 设备硬件型号（device_list 解析时缓存，conversations 接口必填）
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
            token_path = str(self._cfg.get("token_path") or (Path.home() / ".mi.token"))
            # 守护进程常驻路径没有 MI_PASS；只要 token 文件里已有 micoapi
            # 服务 token（登录成功后 miservice 会落盘），就允许无密码启动，
            # _ensure_login 会加载复用、跳过 login()，password 不会被使用。
            has_token = False
            try:
                if Path(token_path).is_file():
                    _tok = json.loads(Path(token_path).read_text(encoding="utf-8"))
                    has_token = isinstance(_tok, dict) and _SID_MICOAPI in _tok
            except (OSError, json.JSONDecodeError):
                has_token = False
            if not username or (not password and not has_token):
                raise RuntimeError(
                    "未配置小米账号：mi_speaker.json 的 username + MI_PASS 环境变量"
                    "（一次性登录：python -m "
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

        def _pick(d: dict) -> None:
            self._did = str(d.get("deviceID", ""))
            self._hardware = str(d.get("hardware") or "")

        want = self._did
        if want:
            for d in devices:
                if str(d.get("deviceID", "")) == want:
                    self._hardware = str(d.get("hardware") or "")
                    return
            for d in devices:
                if str(d.get("miotDID", "")) == want or str(d.get("name", "")) == want:
                    _pick(d)
                    return
            raise RuntimeError(f"找不到设备 {want!r}（账号下共 {len(devices)} 台）")
        if len(devices) == 1:
            _pick(devices[0])
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
    async def conversations(self, limit: int = 10) -> list[dict]:
        """MiNA 对话记录（mi-gpt / mi-service-lite 同款路线，S12 实测 code=0）。

        打 userprofile.mina.mi.com 用户画像服务，**不是** api2 的 ubus
        nlp_result_get——后者在 S12 老固件上设备端返 1001 空数据（真机
        实测），对话记录服务才是实时写入的可靠数据源。records 从新到旧。
        """
        # 注入 service 且带 get_conversations（测试路径）：直接用 fake 数据
        fetcher = getattr(self._service, "get_conversations", None)
        if fetcher is not None:
            raw = await fetcher(self._did, self._hardware, limit) or []
            return [r for r in raw if isinstance(r, dict)]
        # 生产路径：account 直连 userprofile 端点（miservice 无此封装）
        if self._account is None or not self._did:
            return []
        url = (
            "https://userprofile.mina.mi.com/device_profile/v2/conversation"
            f"?limit={int(limit)}&source=dialogu&hardware={self._hardware}"
            f"&requestId=app_ios_{uuid.uuid4().hex[:24]}"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 10; 000; wv) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Version/4.0 Chrome/119.0.6045.193 Mobile "
                "Safari/537.36 /XiaoMi/HybridView/ micoSoundboxApp/i "
                "appVersion/A_2.4.40"
            ),
            "Referer": "https://userprofile.mina.mi.com/dialogue-note/index.html",
        }
        cookies = {
            "userId": self._account.token["userId"],
            "serviceToken": self._account.token["micoapi"][1],
            "deviceId": self._did,
        }
        async with self._account.request(url, headers=headers, cookies=cookies) as r:
            resp = await r.json(content_type=None)
        if resp.get("code") != 0:
            return []
        data = resp.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                return []
        if not isinstance(data, dict):
            return []
        return [r for r in (data.get("records") or []) if isinstance(r, dict)]

    async def latest_asks(self) -> list[dict]:
        """归一化最新提问。字段：request_id / timestamp_ms / question / answer。

        过滤规则同 mi-gpt：只保留单 answer 且类型为 TTS/LLM 的记录——
        滤掉「退出」等命令（无 answer）和音乐播放（TTS+Audio 双 answer）。
        """
        out: list[dict] = []
        for rec in await self.conversations():
            answers = rec.get("answers") or []
            if len(answers) != 1:
                continue
            a = answers[0]
            if not isinstance(a, dict) or a.get("type") not in ("TTS", "LLM"):
                continue
            answer = ((a.get("tts") or {}).get("text") or (a.get("llm") or {}).get("text") or "").strip()
            out.append(
                {
                    "request_id": str(rec.get("requestId") or ""),
                    "timestamp_ms": int(rec.get("time") or 0),
                    "question": str(rec.get("query") or "").strip(),
                    "answer": answer,
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

    async def mute_xiaoai(self) -> bool:
        """静音小爱原生应答（xiaogpt --mute_xiaoai 同款对策）。

        2026-10-17 对照 xiaogpt 源码（yihong0618/xiaogpt stop_if_xiaoai_is_playing）
        + S12 真机实测的三处关键设计：
        1. **pause 不用 stop**：player_pause（ubus action=pause）只暂停当前
           播放流、播放器会话保持；player_stop（action=stop）更暴力，社区
           反馈部分固件上会中断播放器会话。
        2. **先查状态再 pause**：player_get_status 只在 status==1（在播）时
           才 pause——少发无效命令；S12 实测空闲 status=3（pause/stop 空闲
           可达均返 True，但不该乱发）。
        3. 状态查询异常**上抛不吞**：由调用方（通道 _safe_mute）兜底，
           保持 connector 层的错误透明。

        返回 True 表示执行了 pause；False 表示当前没在播（无需动作）。
        """
        status = await self._service.player_get_status(self._did)
        if (status or {}).get("status") != 1:
            return False
        return bool(await self._service.player_pause(self._did))


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
