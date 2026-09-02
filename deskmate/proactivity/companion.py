"""P2-4 桌面常驻「看见你在做什么」主动陪伴。

把感知层的窗口/活动数据转成人类可读的「你在做什么」描述，
并在长时间专注时生成温暖陪伴语（非建议、非任务提醒，纯陪伴存在感）。

设计原则：
- **零 LLM**：活动描述用应用映射 + 窗口标题提取，陪伴语用模板化生成
- **只感知不录全量**：只读当前活动窗口标题 + 应用名，不存历史、不录屏
- **温暖不打扰**：陪伴语有冷却时间（同类型 2 小时内不重复），深度工作中不打断
- **fail-open**：感知失败时返回通用描述，绝不阻断 tick
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..perception.window_monitor import ActiveWindow
from .user_active import ActivityState, UserActivity


# ---------------------------------------------------------------------------
# 应用 → 活动描述映射
# ---------------------------------------------------------------------------

# 关键词 → 活动描述（按优先级匹配，先匹配 bundle_id 再匹配 app 名）
_APP_RULES: list[tuple[str, str]] = [
    # 代码/开发
    ("com.microsoft.VSCode", "写代码"),
    ("com.apple.dt.Xcode", "写代码"),
    ("com.google.android.studio", "写代码"),
    ("com.jetbrains.pycharm", "写代码"),
    ("com.jetbrains.intellij", "写代码"),
    ("code", "写代码"),
    ("xcode", "写代码"),
    ("terminal", "敲命令"),
    ("iterm", "敲命令"),
    ("hyper", "敲命令"),
    # 写作/文档
    ("com.microsoft.Word", "写文档"),
    ("com.apple.TextEdit", "写文档"),
    ("com.apple.iWork.Pages", "写文档"),
    ("notion", "写笔记"),
    ("obsidian", "写笔记"),
    ("typora", "写文档"),
    ("bear", "写笔记"),
    # 浏览器
    ("com.apple.Safari", "浏览网页"),
    ("com.google.Chrome", "浏览网页"),
    ("com.mozilla.firefox", "浏览网页"),
    ("com.microsoft.edgemac", "浏览网页"),
    ("arc", "浏览网页"),
    # 聊天/通讯
    ("com.tencent.xinWeChat", "聊天"),
    ("com.tencent.WeChat", "聊天"),
    ("com.bytedance.lark", "聊天"),
    ("com.dingtalk.mac", "聊天"),
    ("com.tencent.qq", "聊天"),
    ("slack", "聊天"),
    ("discord", "聊天"),
    ("telegram", "聊天"),
    # 设计/创意
    ("com.adobe.Photoshop", "做设计"),
    ("com.adobe.illustrator", "做设计"),
    ("com.adobe.PremierePro", "剪视频"),
    ("com.apple.iMovie", "剪视频"),
    ("figma", "做设计"),
    ("sketch", "做设计"),
    # 文件/系统
    ("com.apple.finder", "整理文件"),
    ("com.apple.systempreferences", "调设置"),
    # 娱乐
    ("com.apple.music", "听音乐"),
    ("com.apple.TV", "看视频"),
    ("netflix", "看视频"),
    ("bilibili", "看视频"),
    ("youtube", "看视频"),
    ("spotify", "听音乐"),
]


def _match_activity(app: str, bundle_id: str) -> str:
    """根据应用名 + bundle_id 匹配活动描述。"""
    app_lower = (app or "").lower()
    bundle_lower = (bundle_id or "").lower()
    for keyword, desc in _APP_RULES:
        kw = keyword.lower()
        if kw in bundle_lower or kw in app_lower:
            return desc
    return "用电脑"


# 切到这些活动描述，视为「离开了工作流」，打断专注计时
_DISTRACTION_DESCS = {"聊天", "听音乐", "看视频"}


def _is_distraction(app: str, bundle_id: str) -> bool:
    """判断当前应用是否属于娱乐/社交类（切过去即视为离开工作）。

    用于专注计时：只有切到分心类应用才重置，工作类应用之间互切
    （写稿 / 查资料 / 写代码）继续累计，符合「切换应用不代表没工作」。
    """
    return _match_activity(app, bundle_id) in _DISTRACTION_DESCS


def _extract_title_hint(title: str) -> str:
    """从窗口标题中提取关键信息（文件名/项目名/网页标题）。"""
    if not title:
        return ""
    # 去掉常见的应用后缀（如 " — Visual Studio Code"、" - Google Chrome"）
    title = title.split(" — ")[0].split(" - ")[0].strip()
    # 太长就截断
    if len(title) > 30:
        title = title[:28] + "…"
    return title


def describe_activity(window: ActiveWindow | None, activity: ActivityState | None) -> str:
    """把感知数据转成人类可读的「你在做什么」描述。

    示例：
    - "你在 VS Code 写代码（openworker 项目）"
    - "你在 Safari 浏览网页"
    - "你暂时离开了电脑"
    - "你在会议中"
    """
    if activity and activity.level == UserActivity.IDLE:
        return "你暂时离开了电脑，我在这里等你回来"
    if activity and activity.level == UserActivity.BUSY:
        return "你在忙碌中（会议 / 通话），我不打扰"

    if window is None:
        return "你在用电脑"

    activity_desc = _match_activity(window.app, window.bundle_id)
    title_hint = _extract_title_hint(window.title)

    if title_hint and title_hint.lower() != window.app.lower():
        return f"你在 {window.app} {activity_desc}（{title_hint}）"
    return f"你在 {window.app} {activity_desc}"


# ---------------------------------------------------------------------------
# 专注时长跟踪 + 陪伴语生成
# ---------------------------------------------------------------------------

@dataclass
class FocusSession:
    """一次连续专注会话。"""
    start_ts: float
    last_ts: float
    app: str
    notified_minutes: set[int]  # 已触发陪伴语的分钟数（去重）
    bundle_id: str = ""  # 起始应用 bundle（用于判断切到分心类时重置）


class CompanionTracker:
    """跟踪用户专注时长，在适当时机生成温暖陪伴语。

    陪伴语触发点（零 LLM，模板化）：
    - 专注 30 分钟：轻量鼓励
    - 专注 60 分钟：提醒休息
    - 专注 90 分钟：关心身体
    - 专注 120 分钟：强烈建议休息

    冷却：同一分钟数只触发一次；用户离开（idle）后重置会话。
    """

    # 触发陪伴语的分钟数阈值
    _THRESHOLDS = [30, 60, 90, 120]

    # 陪伴语模板（按分钟数区间）
    _MESSAGES = {
        30: [
            "你已经专注了半小时，状态不错，继续保持",
            "半小时了，你投入的样子很认真",
        ],
        60: [
            "你已经投入了 1 小时，记得偶尔抬头看看远处，让眼睛休息一下",
            "1 小时了，进度不错，要不要喝口水继续？",
        ],
        90: [
            "你已经连续工作 1.5 小时了，肩膀和脖子还好吗？记得活动一下",
            "90 分钟了，你很投入，但身体也需要照顾，站起来伸个懒腰吧",
        ],
        120: [
            "你已经连续工作 2 小时了，强烈建议休息 10 分钟，看看窗外或者走动一下",
            "2 小时了，效率会开始下降，休息一下反而更好，我陪你",
        ],
    }

    def __init__(self) -> None:
        self._session: FocusSession | None = None

    def update(
        self,
        activity: ActivityState | None,
        window: ActiveWindow | None,
    ) -> str | None:
        """更新专注状态，返回需要展示的陪伴语（无则 None）。

        Args:
            activity: 用户活动状态
            window: 当前活动窗口

        Returns:
            陪伴语文本，或 None（不需要触发）
        """
        now = time.time()

        # 用户离开 / 忙碌 → 结束当前专注会话
        if activity and activity.level != UserActivity.ACTIVE:
            self._session = None
            return None

        app = window.app if window else "unknown"
        bundle = window.bundle_id if window else ""
        distract = _is_distraction(app, bundle)

        # 切到娱乐/社交类应用（聊天/音乐/视频）→ 离开工作流，重置专注计时
        if distract:
            self._session = None
            return None

        # 没有会话，或从分心状态切回工作 → 开始新会话（重新锚定起点）
        if self._session is None or _is_distraction(self._session.app, self._session.bundle_id):
            self._session = FocusSession(
                start_ts=now, last_ts=now, app=app, bundle_id=bundle, notified_minutes=set()
            )
            return None

        # 不同非分心应用之间切换（写稿 / 查资料 / 写代码）→ 继续累计，不重置
        self._session.app = app
        self._session.bundle_id = bundle
        self._session.last_ts = now
        duration_min = int((now - self._session.start_ts) / 60)

        # 检查是否达到阈值
        for threshold in self._THRESHOLDS:
            if duration_min >= threshold and threshold not in self._session.notified_minutes:
                self._session.notified_minutes.add(threshold)
                # 选一个模板（用分钟数做种子，保证同一阈值同一用户体验一致）
                templates = self._MESSAGES[threshold]
                idx = (duration_min // threshold) % len(templates)
                return templates[idx]

        return None

    def reset(self) -> None:
        """重置专注跟踪（如用户手动触发）。"""
        self._session = None


# 全局单例（tick 间共享状态）
_companion_tracker = CompanionTracker()


def get_companion_tracker() -> CompanionTracker:
    """获取全局陪伴跟踪器。"""
    return _companion_tracker
