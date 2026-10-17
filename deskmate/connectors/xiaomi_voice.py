"""小爱音箱语音通道（路线 A）：轮询音箱提问 → 小桔大脑应答 → 音箱播报。

链路：小爱音箱（用户喊「小爱同学，<问题>」）→ MiNA 云端拦截（get_latest_ask）
→ 小桔完整聊天管线（人格 / 记忆 / 成长，由 app 注入的 run_turn 提供）
→ text_to_speech 播报（小桔的嘴）。

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


class VoiceChannel:
    """小爱音箱语音通道守护循环。

    参数：
    - connector: 提供 latest_asks() / speak() / stop_playback() 的对象
     （生产为 XiaomiSpeakerConnector，测试为 FakeConnector）。
    - run_turn: async (question: str) -> str，小桔大脑。生产由 app 提供：
      走 manager.get_engine(agent="deskmate") → engine.run() → 收集
      ASSISTANT_MESSAGE。测试可注入任意 fake。
    - poll_interval: 轮询间隔秒（下限 0.1s）。
    """

    def __init__(
        self,
        *,
        connector: Any,
        run_turn: RunTurn,
        poll_interval: float = 1.5,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self._connector = connector
        self._run_turn = run_turn
        self._poll_interval = max(0.1, float(poll_interval))
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
            await self._connector.speak(text)
            self._log.info("[xiaomi-voice] 已播报（%d 字）", len(text))
        except Exception as e:
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
    cfg = config if config is not None else load_speaker_config()
    if not cfg.get("enabled"):
        return None
    if connector is None:
        from .xiaomi_speaker import XiaomiSpeakerConnector

        connector = XiaomiSpeakerConnector(cfg)
    return VoiceChannel(
        connector=connector,
        run_turn=run_turn,
        poll_interval=float(cfg.get("poll_interval") or 1.5),
    )
