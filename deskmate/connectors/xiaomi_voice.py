"""小爱音箱语音通道（路线 A）：轮询音箱提问 → 小桔大脑应答 → 音箱播报。

链路：小爱音箱（用户喊「小爱同学，<问题>」）→ MiNA 对话记录拦截
（userprofile conversations，S12 实测可用）→ 小桔完整聊天管线（人格 /
记忆 / 成长，由 app 注入的 run_turn 提供）→ text_to_speech 播报（小桔的嘴）。

工程约束：
- 全部 fail-open：轮询 / 大脑 / 播报任一环节异常只记日志，循环不死。
- 基线语义：启动那一刻之前的历史提问不重放（AskTracker）。
- 依赖注入：connector 与 run_turn 都是可替换件，测试用 fake 驱动全流程。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from .xiaomi_speaker import AskTracker, load_speaker_config

logger = logging.getLogger(__name__)

RunTurn = Callable[[str], Awaitable[str]]

#: Mina ubus「远程控制超时」特征串（code 3012）：请求其实已送达设备执行，
#: 只是 Mina 回执超时。2026-10-17 真机两轮实测：日志报失败但音箱正常播出。
#: 刻意与「真失败」区分——若按失败处理（尤其是将来有人加重试），同一条
#: 回答会被播两遍。
_REMOTE_TIMEOUT_MARKERS = ("远程控制超时", "3012")

#: ubus 回执等待上限（秒）。S12 老固件回执通道坏：服务端要 40+ 秒才回 3012
#: （真机实测 15:28:24 提问 → 15:29:13 才打日志，停滞 49s），而播放 3-5s 早就
#: 开始了。等满回执对用户体验零贡献，只拖累「发现下一句提问」的时效。
#: 到点按「命令已送达」放行——缩短窗口不增加误判面（3012 本身就不携带设备
#: 状态，等 30s 拿到 3012 同样无法区分播了/没播）。
_DEFAULT_SPEAK_TIMEOUT = 10.0


def _is_remote_timeout(exc: BaseException) -> bool:
    """Mina 回执超时（命令可能已送达）vs 真播报失败。"""
    s = str(exc)
    return any(m in s for m in _REMOTE_TIMEOUT_MARKERS)


def ensure_channel_logging() -> None:
    """确保语音通道日志可见（INFO 级 StreamHandler，幂等）。

    为什么需要：server 走 uvicorn，uvicorn 只配自己的 logger，root 没有
    handler → 本模块日志只剩 logging 的 last-resort（WARNING+），INFO 全被
    吞掉。2026-10-17 排查语音延迟时「新提问/已播报」全程不可见，只能靠猜。
    语音通道是运维关键路径，构建通道时确保有 INFO 级输出。
    """
    if logger.handlers:
        return
    h = logging.StreamHandler()
    h.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


class VoiceChannel:
    """小爱音箱语音通道守护循环。

    参数：
    - connector: 提供 latest_asks() / speak() / stop_playback() 的对象
     （生产为 XiaomiSpeakerConnector，测试为 FakeConnector）。
    - run_turn: async (question: str) -> str，小桔大脑。生产由 app 提供：
      走 manager.get_engine(agent="deskmate") → engine.run() → 收集
      ASSISTANT_MESSAGE。测试可注入任意 fake。
    - poll_interval: 轮询间隔秒（下限 0.1s）。
    - speak_timeout: ubus 回执等待上限秒（下限 1.0s）。S12 回执通道坏，
      服务端 40+ 秒才回 3012，播放却 3-5s 已开始；到点按已送达放行。
    """

    def __init__(
        self,
        *,
        connector: Any,
        run_turn: RunTurn,
        poll_interval: float = 1.5,
        speak_timeout: float = _DEFAULT_SPEAK_TIMEOUT,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self._connector = connector
        self._run_turn = run_turn
        self._poll_interval = max(0.1, float(poll_interval))
        self._speak_timeout = max(1.0, float(speak_timeout))
        self._log = log or logger
        self._tracker = AskTracker()
        self._stop = asyncio.Event()

    # -- 控制 ----------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return not self._stop.is_set()

    async def serve(self) -> None:
        """启动入口：先登录/解析设备（失败即退出，不进循环），再进入轮询。"""
        starter = getattr(self._connector, "start", None)
        if starter is not None:
            await starter()
        await self.run_forever()

    # -- 主循环 --------------------------------------------------------------
    async def run_forever(self) -> None:
        self._log.info("[xiaomi-voice] 语音通道启动（轮询 %.1fs）", self._poll_interval)
        try:
            while not self._stop.is_set():
                try:
                    asks = await self._connector.latest_asks()
                    for ask in self._tracker.fresh(asks):
                        await self._handle(ask)
                except Exception as e:  # 轮询异常：记日志，循环继续
                    self._log.warning("[xiaomi-voice] 轮询异常（继续）: %s", e)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._log.info("[xiaomi-voice] 语音通道停止")

    # -- 单问处理 ------------------------------------------------------------
    async def _handle(self, ask: dict) -> None:
        question = str(ask.get("question") or "").strip()
        if not question:
            return
        self._log.info("[xiaomi-voice] 新提问: %s", question[:80])
        await self._safe_stop()  # 打断小爱原生应答（检测时刻）
        try:
            reply = await self._run_turn(question)
        except Exception as e:  # 大脑异常：本轮静默，不杀循环
            self._log.warning("[xiaomi-voice] 大脑异常（本轮静默）: %s", e)
            return
        text = (reply or "").strip()
        if not text:
            return
        await self._safe_stop()  # 原生应答可能在思考期间刚起，播报前再停
        try:
            await asyncio.wait_for(
                self._connector.speak(text), timeout=self._speak_timeout
            )
            self._log.info("[xiaomi-voice] 已播报（%d 字）", len(text))
        except asyncio.TimeoutError:
            # 回执通道坏（S12 常态）：命令已送达、设备在播，只是 Mina 不回执。
            # 刻意不重试——重试会把同一条回答播两遍。
            self._log.info(
                "[xiaomi-voice] 已播报（%d 字；回执 %.0fs 超时，命令已送达，不重试）",
                len(text),
                self._speak_timeout,
            )
        except Exception as e:
            if _is_remote_timeout(e):
                # 同上的 3012 路径：服务端自己拖了 40+ 秒才回「远程控制超时」。
                # 按已送达记 INFO——这是常态路径，不是故障。
                self._log.info(
                    "[xiaomi-voice] 已播报（%d 字；Mina 回执超时 3012，命令已送达，不重试）",
                    len(text),
                )
            else:
                self._log.warning("[xiaomi-voice] 播报失败: %s", e)

    async def _safe_stop(self) -> None:
        try:
            await self._connector.stop_playback()
        except Exception:
            pass


def build_voice_channel(
    run_turn: RunTurn,
    *,
    config: Optional[dict] = None,
    connector: Any = None,
) -> Optional[VoiceChannel]:
    """按配置构建语音通道；未启用或未配置账号时返回 None（fail-closed）。"""
    ensure_channel_logging()
    cfg = config if config is not None else load_speaker_config()
    if not cfg.get("enabled"):
        return None
    if not str(cfg.get("username") or "").strip():
        logger.warning(
            "[xiaomi-voice] 已启用但未配置小米账号（username/MI_USER），跳过启动"
        )
        return None
    if connector is None:
        from .xiaomi_speaker import XiaomiSpeakerConnector

        connector = XiaomiSpeakerConnector(cfg)
    return VoiceChannel(
        connector=connector,
        run_turn=run_turn,
        poll_interval=float(cfg.get("poll_interval") or 1.5),
        speak_timeout=float(cfg.get("speak_timeout") or _DEFAULT_SPEAK_TIMEOUT),
    )
