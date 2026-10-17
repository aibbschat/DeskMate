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

#: 提问聚合窗口（秒）。小爱端点检测激进：用户思考停顿/换气都被判「说完了」，
#: 半句话被截断；后续语音可能拆成独立记录晚几秒到来、也可能被抢答播放期间
#: 的设备 AEC 吞掉（2026-10-17 萧哥真机观察「往往没说完就接过去」）。
#: 窗口内汇合延续记录再拼接；延迟被思考提示 filler 播放覆盖。0=关闭。
_DEFAULT_MERGE_WINDOW = 2.5


def _is_remote_timeout(exc: BaseException) -> bool:
    """Mina 回执超时（命令可能已送达）vs 真播报失败。"""
    s = str(exc)
    return any(m in s for m in _REMOTE_TIMEOUT_MARKERS)


def _merge_questions(group: list[dict]) -> str:
    """把一组（被端点截断拆开的）提问按时间序拼成完整句。

    相邻重复只留一份（小爱对同一句识别出两条记录的罕见情形）。
    """
    out: list[str] = []
    for a in sorted(group, key=lambda x: int(x.get("timestamp_ms") or 0)):
        q = str(a.get("question") or "").strip()
        if q and (not out or out[-1] != q):
            out.append(q)
    return "".join(out)  # 中文无分隔拼接


class _AskAggregator:
    """提问聚合器：小爱端点检测激进（思考停顿/换气都被判「说完了」），
    用户的半句话会被截断；后续语音可能作为独立记录晚几秒到来，也可能
    在抢答播放期间被设备 AEC 吞掉不进记录（2026-10-17 萧哥真机观察
    「往往没说完就接过去，小桔只听到半句话」）。

    策略：窗口期内汇合延续记录，flush 时按时间序拼接，一次性交给大脑。
    代价（多等一个窗口）被思考提示 filler 的播放覆盖；窗口内没有后续
    也不亏。窗口从首条到达起算、不顺延（防用户断续续等永远不答）。
    """

    def __init__(self, window: float) -> None:
        self._window = max(0.0, float(window))
        self._pending: list[dict] = []
        self._deadline: Optional[float] = None

    def add(self, asks: list[dict], now: float) -> None:
        for a in asks:
            if str(a.get("question") or "").strip():
                self._pending.append(a)
        if self._pending and self._deadline is None:
            self._deadline = now + self._window

    def pop_ready(self, now: float, force: bool = False) -> list[list[dict]]:
        """取出到期的组；force=True 时忽略期限（退出前 flush 用）。"""
        if not self._pending:
            return []
        if not force and (self._deadline is None or now < self._deadline):
            return []
        group, self._pending = self._pending, []
        self._deadline = None
        return [group]


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
    - connector: 提供 latest_asks() / speak() / mute_xiaoai() 的对象
     （生产为 XiaomiSpeakerConnector，测试为 FakeConnector）。
    - run_turn: async (question: str) -> str，小桔大脑。生产由 app 提供：
      走 manager.get_engine(agent="deskmate") → engine.run() → 收集
      ASSISTANT_MESSAGE。测试可注入任意 fake。
    - poll_interval: 轮询间隔秒（下限 0.1s）。
    - speak_timeout: ubus 回执等待上限秒（下限 1.0s）。S12 回执通道坏，
      服务端 40+ 秒才回 3012，播放却 3-5s 已开始；到点按已送达放行。
    - merge_window: 提问聚合窗口秒。小爱端点检测激进会把半句话截断，
      窗口内汇合延续记录再拼接交给大脑；延迟被 filler 覆盖。0=关闭。
    - thinking_filler: 打断小爱后、大脑思考前发送的短 TTS 提示文本，
      填补思考期间的静默。speak() 只发命令不等播放完成，filler 与
      run_turn 并行。空串则不发。缺省「让我想想」。
    - mute_on_playback: 播放状态探测开关（默认开）。不等对话记录——
      记录出现延迟（1-3s）是小爱抢答漏字大半句的元凶；每轮查
      player_get_status，小爱在播（status=1）且非我方播出即 pause，
      链路压到 轮询间隔+mute ~0.9s。误伤面：小爱放歌/闹钟/报时也会
      被 pause；false 关探测，回归记录驱动（xiaogpt 原版行为）。
    """

    def __init__(
        self,
        *,
        connector: Any,
        run_turn: RunTurn,
        poll_interval: float = 1.5,
        speak_timeout: float = _DEFAULT_SPEAK_TIMEOUT,
        merge_window: float = _DEFAULT_MERGE_WINDOW,
        thinking_filler: str = "让我想想",
        mute_on_playback: bool = True,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self._connector = connector
        self._run_turn = run_turn
        self._poll_interval = max(0.1, float(poll_interval))
        self._speak_timeout = max(1.0, float(speak_timeout))
        self._merge_window = max(0.0, float(merge_window))
        self._aggregator = _AskAggregator(self._merge_window)
        self._thinking_filler = thinking_filler
        self._mute_on_playback = bool(mute_on_playback)
        self._filler_tasks: list[asyncio.Task] = []  # 并行发送中的思考提示
        #: 我方播放守卫（loop.time() 基准，秒）：此前不做播放探测 pause。
        #: 覆盖生成期 + filler/正式播报的命令期与设备侧云端播放延迟
        #: （3-5s）——这期间 status==1 是小桔自己在播，pause=掐自己。
        self._own_playback_until: float = 0.0
        self._log = log or logger
        self._tracker = AskTracker()
        self._stop = asyncio.Event()

    # -- 控制 ----------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()
        for t in self._filler_tasks:  # 取消还没发完的思考提示
            t.cancel()

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
        loop = asyncio.get_running_loop()
        try:
            while not self._stop.is_set():
                try:
                    asks = await self._connector.latest_asks()
                    # 播放状态探测（提前打断）：不等对话记录——记录出现延迟
                    # 1-3s，等它小爱已播大半句（2026-10-17 真机实测 pause
                    # 机制有效、链路各段快，唯独记录迟）。
                    if self._mute_on_playback:
                        await self._probe_mute(loop.time())
                    fresh = self._tracker.fresh(asks)
                    for ask in fresh:
                        # 发现即静音（xiaogpt 轮询循环级打断）：不等聚合
                        # 窗口/大脑流程，小爱抢答压得越早漏得越少。
                        await self._safe_mute()
                    self._aggregator.add(fresh, loop.time())
                    for group in self._aggregator.pop_ready(loop.time()):
                        await self._handle_group(group)
                except Exception as e:  # 轮询异常：记日志，循环继续
                    self._log.warning("[xiaomi-voice] 轮询异常（继续）: %s", e)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._log.info("[xiaomi-voice] 语音通道停止")
            # 退出前 flush 未到期组：App 关闭不吞用户最后一句话（若关闭流程
            # 有取消时限，被取消也只是丢这半句，不影响主链路）。
            try:
                for group in self._aggregator.pop_ready(loop.time(), force=True):
                    await self._handle_group(group)
            except Exception as e:
                self._log.debug("[xiaomi-voice] 退出前 flush 失败（忽略）: %s", e)
            for t in self._filler_tasks:  # 等取消落地，避免 "Task was destroyed"
                t.cancel()
            if self._filler_tasks:
                await asyncio.gather(*self._filler_tasks, return_exceptions=True)

    # -- 单组处理 ------------------------------------------------------------
    async def _handle_group(self, group: list[dict]) -> None:
        """处理一组（可能被端点截断拆开的）提问：拼接 → 提示 → 大脑 → 播报。"""
        question = _merge_questions(group)
        if not question:
            return
        loop = asyncio.get_running_loop()
        # 播放守卫起：生成期 + filler + 正式播报全程（含设备云端播放延迟）
        # 不许探测 pause——那会掐小桔自己的声音。
        self._own_playback_until = loop.time() + 120.0
        if len(group) > 1:
            self._log.info(
                "[xiaomi-voice] 新提问（合并 %d 条截断记录）: %s", len(group), question[:80]
            )
        else:
            self._log.info("[xiaomi-voice] 新提问: %s", question[:80])
        await self._safe_mute()  # 双保险：循环级已静音过一次（发现即静音）
        # 思考提示：打断后立刻发一句短 TTS，填补大脑思考期间的静默。
        # 必须**并行**发送（create_task，不 await 回执）：S12 的 ubus 回执要
        # 40+ 秒，若 await 它 run_turn 会被推迟 speak_timeout 秒——端到端
        # 延迟凭空 +10s（2026-10-17 修：原实现是 await wait_for 串行等）。
        # sleep(0) 让 task 立即启动，保证 filler 命令先于正式 speak 入队
        # （设备侧按到达顺序播放：先「让我想想」再回答）。
        # 注意：run_turn 之后不能再静音——filler 可能正在播，pause 会把它
        # 掐断；正式 speak 到设备播出有 3-5s 云端延迟，设备自会排队接播。
        if self._thinking_filler:
            task = asyncio.create_task(self._speak_filler(self._thinking_filler))
            self._filler_tasks.append(task)
            task.add_done_callback(self._filler_tasks.remove)
            # filler 守卫：命令期（回执最多 speak_timeout）+ 设备云端播放
            # 延迟（3-5s）——播出期间探测 pause 会掐掉「让我想想」。
            self._own_playback_until = max(
                self._own_playback_until,
                loop.time() + self._speak_timeout + 15.0,
            )
            await asyncio.sleep(0)  # 让 filler task 启动到「命令已提交」
        try:
            reply = await self._run_turn(question)
        except Exception as e:  # 大脑异常：本轮静默，不杀循环
            self._log.warning("[xiaomi-voice] 大脑异常（本轮静默）: %s", e)
            return
        text = (reply or "").strip()
        if not text:
            return
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
                return
        # 播报守卫续到设备侧播放预计结束（约 4 字/秒 + 云端延迟余量）：
        # 回答播出中 status==1，探测 pause 会停掉小桔自己的话。
        self._own_playback_until = max(
            self._own_playback_until,
            loop.time() + 15.0 + len(text) / 4.0,
        )

    async def _speak_filler(self, text: str) -> None:
        """并行发送思考提示（fire-and-forget，失败只记 debug）。"""
        try:
            await asyncio.wait_for(
                self._connector.speak(text), timeout=self._speak_timeout
            )
            self._log.debug("[xiaomi-voice] 思考提示已发送（%d 字）", len(text))
        except Exception as e:
            self._log.debug("[xiaomi-voice] 思考提示发送失败（不影响主流程）: %s", e)

    async def _safe_mute(self) -> None:
        """静音小爱原生应答（xiaogpt --mute_xiaoai 同款）；失败只记 debug 不杀循环。"""
        try:
            await self._connector.mute_xiaoai()
        except Exception as e:
            self._log.debug("[xiaomi-voice] 静音小爱失败（继续）: %s", e)

    async def _probe_mute(self, now: float) -> None:
        """播放状态探测：小爱在播（status=1）且非我方播出 → pause。

        为什么不等对话记录：记录出现延迟 1-3s，等它小爱已播大半句
        （2026-10-17 萧哥真机反馈「小爱的字还是挺多」）。S12 真机实测
        pause 有效（播放中 status=1 可识别，pause 后 status=0 真停）。
        探测把打断链路压到 轮询间隔 + mute ~0.9s。

        守卫 now < _own_playback_until：我方生成/播出期（含 filler 与
        正式播报的设备侧云端延迟）——这期间在播的是小桔自己，pause=掐
        自己（测试：test_probe_respects_own_playback_guard）。
        """
        if now < self._own_playback_until:
            return  # 我方在播，让路
        try:
            status = await self._connector.get_player_status()
        except Exception as e:
            self._log.debug("[xiaomi-voice] 播放状态查询失败（跳过本轮探测）: %s", e)
            return
        if not isinstance(status, dict) or status.get("status") != 1:
            return
        await self._safe_mute()
        self._log.info(
            "[xiaomi-voice] 播放探测：小爱在播 → 已 pause（未等对话记录，提前打断）"
        )


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
        merge_window=float(cfg.get("merge_window") or 0.0)
        if "merge_window" in cfg
        else _DEFAULT_MERGE_WINDOW,
        # 显式空串 = 关闭思考提示（尊重配置，不用 or 回退默认）
        thinking_filler=str(cfg["thinking_filler"])
        if "thinking_filler" in cfg
        else "让我想想",
        # 播放状态探测：默认开；显式 false 关（回归 xiaogpt 记录驱动）
        mute_on_playback=bool(cfg.get("mute_on_playback", True)),
    )
