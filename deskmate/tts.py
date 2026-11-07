"""按需语音朗读 —— 豆包 Agent Plan / seed-tts-2.0 → Mac 本机扬声器。

设计口径（2026-11-07 萧哥拍板）：
- **平时不自动朗读**。只有模型判定用户要「语音回复」时，调用不带正文的意图工具。
- **模型只判意图，不写朗读稿**。宿主取当轮最终回复正文（`assistant_message` 里最后一个
  无 tool_calls 的），清洗 markdown 后送 TTS —— 念的就是屏幕上那条，不是模型另编的一段。
- **单轮语义**：说完即静默，不留会话级开关；「文字就行 / 不用念了」时模型不调工具即可。
- 声音从 Mac 本机扬声器出（`afplay`），**零前端改动、零重打包**。

🔴 红线：
- 端点必须是 `openspeech.bytedance.com/api/v3/plan/tts/unidirectional`。
  **绝不用 `ark.cn-beijing.volces.com/api/v3`**（萧哥：接入会产生额外费用）。
- key 复用 `secrets.json:provider:ark-agent-plan-cn.api_key`，但 **host 不复用**该条目的
  `base_url`（那是 LLM 的 `/api/plan/v3`）。
- 响应是 **NDJSON text/plain**，不是 `audio/mpeg` 二进制：逐行
  `{"code":0,"data":"<base64 分片>"}` 拼接解码才成 mp3；`data:null` 的 sentence/usage
  行必须跳过；结束码是 `20000000` 不是 0。
- **全程 fail-open**：本模块任何异常都在 `speak_reply` 里吞掉并记日志，绝不影响主聊天链路。
- 模块顶层零网络、零密钥读取、零 import 期副作用 —— 云端同步后 import 也安全。

打包：`packaging/openworker-server.spec` 用 `collect_submodules("deskmate")` 全量收集，
新增本文件自动进 bundle，无需改 spec。
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import uuid
from typing import Any, Callable, Optional

import aisuite as ai

from deskmate.coerce import as_int

VOICE_TOOL_NAME = "request_voice_reply"
MESSAGE_TOOL_NAME = "send_voice_message"
SET_VOICE_TOOL_NAME = "set_voice_reply_voice"
AGENT_PLAN_PROFILE = "provider:ark-agent-plan-cn"
TTS_URL = "https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional"
DEFAULT_RESOURCE_ID = "seed-tts-2.0"
DEFAULT_VOICE = "zh_female_vv_uranus_bigtts"
DEFAULT_MAX_CHARS = 4000
# 气泡是聊天里的一条消息，不是有声书：上限刻意比「朗读」短得多，
# 否则 4000 字能合成出 3-5MB 的 mp3，base64 后塞进 WS 单帧会明显卡顿。
DEFAULT_MESSAGE_MAX_CHARS = 1200
DEFAULT_BITRATE_KBPS = 64
DEFAULT_TIMEOUT = 30.0
END_CODE = 20000000

# 音色预设 —— **只收本机实测能出音频的**（2026-11-07 逐个打通 seed-tts-2.0）。
#
# 🔴 探测方法有个陷阱：Plan 对「音色不存在」和「音色存在但套餐没授权」回的是**同一句**
#    `resource ID is mismatched with speaker related resource`（已用编造 ID 做过对照）。
#    所以只有真正拿到音频 + 结束码 20000000 的才算通过。
# 实测结论：Plan 只认 2.0（`*_uranus_bigtts`）音色；1.0 的 mars/moon/conversation 系列
#    和 S2S 主打音色（云舟/小天）全部 mismatch —— 别往这里加。
VOICE_PRESETS: dict[str, dict[str, Any]] = {
    "vivi": {
        "id": "zh_female_vv_uranus_bigtts",
        "label": "Vivi",
        "gender": "female",
        "names": ("vivi", "薇薇", "vivi 2.0"),
    },
    "xiaohe": {
        "id": "zh_female_xiaohe_uranus_bigtts",
        "label": "小何",
        "gender": "female",
        "names": ("小何", "小何 2.0"),
    },
    "tuanzi": {
        "id": "zh_female_tuanzi_uranus_bigtts",
        "label": "团子",
        "gender": "female",
        "names": ("团子", "团子 2.0"),
    },
    "gaolengyujie": {
        "id": "zh_female_gaolengyujie_uranus_bigtts",
        "label": "高冷御姐",
        "gender": "female",
        "names": ("高冷御姐", "御姐"),
    },
    "linjianvhai": {
        "id": "zh_female_linjianvhai_uranus_bigtts",
        "label": "邻家女孩",
        "gender": "female",
        "names": ("邻家女孩", "邻家"),
    },
    "wenrouxiaoya": {
        "id": "zh_female_wenrouxiaoya_uranus_bigtts",
        "label": "温柔小雅",
        "gender": "female",
        "names": ("温柔小雅", "小雅"),
    },
    "ruyayichen": {
        "id": "zh_male_ruyayichen_uranus_bigtts",
        "label": "儒雅逸辰",
        "gender": "male",
        "names": ("儒雅逸辰", "逸辰", "儒雅"),
    },
    "yangguangqingnian": {
        "id": "zh_male_yangguangqingnian_uranus_bigtts",
        "label": "阳光青年",
        "gender": "male",
        "names": ("阳光青年", "阳光"),
    },
    "aojiaobazong": {
        "id": "zh_male_aojiaobazong_uranus_bigtts",
        "label": "傲娇霸总",
        "gender": "male",
        "names": ("傲娇霸总", "霸总"),
    },
}

log = logging.getLogger(__name__)


class VoiceReplyError(RuntimeError):
    """TTS 链路的可诊断失败。`speak_reply` 会把它降级成结构化结果。"""


# ---------------------------------------------------------------- 环境门


def voice_reply_available(*, platform: Optional[str] = None, env: Optional[Any] = None) -> bool:
    """只在 macOS 桌面端启用；云端 Linux 自动不注册工具（零费用、零风险）。

    `DESKMATE_VOICE_REPLY_ENABLED=0|false|no|off` 可显式关闭。默认在 macOS 上是开的，
    但「开」只代表工具可见 —— 出声仍必须由用户当轮的语音意图触发。
    """
    plat = platform if platform is not None else sys.platform
    if plat != "darwin":
        return False
    source = env if env is not None else os.environ
    flag = str(source.get("DESKMATE_VOICE_REPLY_ENABLED") or "").strip().lower()
    return flag not in ("0", "false", "no", "off")


# ---------------------------------------------------------------- 意图工具


_VOICE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": VOICE_TOOL_NAME,
        "description": (
            "Request that the final reply of THIS turn be read aloud through the Mac speakers. "
            "Call ONLY when the user explicitly asks to hear the reply — e.g. 「语音回复」"
            "「用语音发给我」「念给我听」「读出来」「说给我听」「发语音」, or 'read aloud' / "
            "'say it out loud' / 'speak this'. Do NOT call for ordinary messages, and do NOT "
            "call when the user wants text only （「文字就行」「不用念了」「别读了」）. "
            "This tool takes NO text: the host speaks the exact final reply shown on screen, "
            "so never write a separate script to be read and never add a preamble about the "
            "audio. Call it once, then write your normal reply."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def make_voice_reply_tool() -> Callable[[], dict[str, Any]]:
    """造一个「只判意图、不产正文、不产音频」的工具。

    打标记的方式是**事件流**：工具返回后引擎广播 `tool_finished`，WS 侧的
    `VoiceTurnTracker` 看到它才在 `turn_done` 后朗读。这样工具与会话状态完全解耦，
    engine 复用/恢复的会话也不会漏。
    """

    def request_voice_reply() -> dict[str, Any]:
        return {"voice_reply_requested": True, "scope": "this_turn_only"}

    request_voice_reply.__name__ = VOICE_TOOL_NAME
    request_voice_reply.__doc__ = _VOICE_SCHEMA["function"]["description"]
    request_voice_reply.__aisuite_tool_metadata__ = ai.ToolMetadata(
        name=VOICE_TOOL_NAME,
        category="voice",
        risk_level="low",
        capabilities=["speak"],
        requires_approval=False,
    )
    request_voice_reply.__coworker_schema__ = _VOICE_SCHEMA
    return request_voice_reply


_MESSAGE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": MESSAGE_TOOL_NAME,
        "description": (
            "Send the final reply of THIS turn as a voice MESSAGE in the chat — a WeChat-style "
            "voice bubble the user taps to play. Call when the user wants to RECEIVE a voice "
            "message rather than hear it right now — e.g. 「发条语音」「发语音给我」「发个语音包」"
            "「甩个语音过来」「语音条」, or 'send me a voice message'. "
            "Distinguish from request_voice_reply: that one plays out loud immediately "
            "（「念给我听」「读出来」）; this one leaves a bubble that stays silent until tapped. "
            "This tool takes NO text: the host records the exact final reply shown on screen. "
            "Never write a separate script and never announce that you are sending audio. "
            "Call it once, then write your normal reply."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def make_send_voice_message_tool() -> Callable[[], dict[str, Any]]:
    """造「发语音消息」工具：零参数，宿主从当轮最终正文合成语音条。"""

    def send_voice_message() -> dict[str, Any]:
        return {"voice_message_requested": True, "scope": "this_turn_only"}

    send_voice_message.__name__ = MESSAGE_TOOL_NAME
    send_voice_message.__doc__ = _MESSAGE_SCHEMA["function"]["description"]
    send_voice_message.__aisuite_tool_metadata__ = ai.ToolMetadata(
        name=MESSAGE_TOOL_NAME,
        category="voice",
        risk_level="low",
        capabilities=["speak"],
        requires_approval=False,
    )
    send_voice_message.__coworker_schema__ = _MESSAGE_SCHEMA
    return send_voice_message


# ---------------------------------------------------------------- 单轮追踪


class VoiceTurnTracker:
    """从一轮 engine 事件流里判定「要不要发声、用哪种出口」并取出要发的正文。

    每轮 `run_turn` 新建一个实例 ⇒ 天然单轮语义，说完即静默。

    两种出口互斥：
    - `speak`   —— 直接从扬声器念（`request_voice_reply`）
    - `message` —— 发一条语音气泡，点了才播（`send_voice_message`）
    同一轮都调了就**以最后一次为准**：绝不能既念一遍又留一条气泡。

    取正文的规则：**最后一个没有 tool_calls 的 `assistant_message`**。
    工具循环中那些带 tool_calls 的中间消息（「我先查一下」）不算最终回复。
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        tool_name: str = VOICE_TOOL_NAME,
        message_tool_name: str = MESSAGE_TOOL_NAME,
    ) -> None:
        self._enabled = bool(enabled)
        self._tool_name = tool_name
        self._message_tool_name = message_tool_name
        self._mode: Optional[str] = None
        self._text = ""

    def observe(self, event_type: str, data: Optional[dict[str, Any]]) -> None:
        row = data or {}
        if event_type == "tool_finished":
            if row.get("status") == "ok":
                name = row.get("name")
                if name == self._tool_name:
                    self._mode = "speak"
                elif name == self._message_tool_name:
                    self._mode = "message"
        elif event_type == "assistant_message":
            if not row.get("tool_calls"):
                self._text = row.get("text") or ""

    @property
    def text(self) -> str:
        return self._text

    @property
    def mode(self) -> Optional[str]:
        return self._mode

    @property
    def should_speak(self) -> bool:
        return self._enabled and self._mode == "speak" and bool(self._text.strip())

    @property
    def should_send_message(self) -> bool:
        return self._enabled and self._mode == "message" and bool(self._text.strip())


# ---------------------------------------------------------------- 正文清洗


_FENCE_RE = re.compile(r"```.*?```", re.S)
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HTML_RE = re.compile(r"<[^>]+>")
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_EMPHASIS_RE = re.compile(r"\*\*|__|\*|_")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.M)
_BULLET_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d+\.)\s+", re.M)
_RULE_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$", re.M)
_BLANK_RE = re.compile(r"\n{3,}")


def clean_for_speech(text: str) -> str:
    """去掉渲染层标记，保留可读正文与段落换行。

    注意「语音内容＝屏幕文字」指的是**同一份正文**，不是原始 markdown 字节：
    `#`、`**`、链接 URL、代码块围挡这些是渲染语法，念出来没有意义。
    绝不用 `re.sub(r"\\s+", " ")` 全压空白 —— 段落换气要留住。
    """
    s = text or ""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _FENCE_RE.sub("\n", s)          # 代码块整段不念
    s = _IMG_RE.sub("", s)
    s = _LINK_RE.sub(r"\1", s)           # 链接只念锚文本
    s = _HTML_RE.sub("", s)
    s = _INLINE_CODE_RE.sub(r"\1", s)
    s = _EMPHASIS_RE.sub("", s)
    s = _HEADING_RE.sub("", s)
    s = _QUOTE_RE.sub("", s)
    s = _BULLET_RE.sub("", s)
    s = _RULE_RE.sub("", s)
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    s = _BLANK_RE.sub("\n\n", s)
    return s.strip()


def truncate_for_speech(text: str, *, max_chars: Optional[int] = None) -> tuple[str, bool]:
    """长回复截断，优先在句末断。**0 是合法值**（=不截断），不许被 `or default` 吞掉。"""
    if max_chars is None:
        max_chars = as_int(os.environ.get("DESKMATE_TTS_MAX_CHARS"), DEFAULT_MAX_CHARS)
    limit = as_int(max_chars, DEFAULT_MAX_CHARS)
    if limit <= 0 or len(text) <= limit:
        return text, False
    window = text[:limit]
    boundary = max(window.rfind(c) for c in "。！？!?\n；;")
    if boundary >= limit // 2:
        return window[: boundary + 1], True
    return window, True


# ---------------------------------------------------------------- 音色

VOICE_PREFS_KEY = "voice_reply_voice"


def voice_catalog() -> list[dict[str, Any]]:
    """给人和模型看的音色清单（别名 + 中文名 + 性别 + 完整 ID）。"""
    return [
        {
            "alias": alias,
            "label": preset["label"],
            "gender": preset["gender"],
            "id": preset["id"],
        }
        for alias, preset in VOICE_PRESETS.items()
    ]


def resolve_voice(value: Any) -> Optional[str]:
    """别名 / 中文名 / 完整音色 ID → 完整 ID；认不出返回 None。

    大小写与首尾空白都容忍（「VIVI」「 vivi 」等价），否则用户说「换个 VIVI」会莫名失败。
    完整 ID 不做白名单校验 —— 用户可能自己从控制台拿了新音色 ID 来试，
    能不能出声由服务端判定（失败会在 `speak_reply` 里 fail-open 成静默）。
    """
    text = str(value or "").strip()
    if not text:
        return None
    lowered = text.lower()
    for alias, preset in VOICE_PRESETS.items():
        if lowered == alias.lower() or lowered == preset["id"].lower():
            return preset["id"]
        for name in preset["names"]:
            if lowered == str(name).strip().lower():
                return preset["id"]
    if lowered.startswith("zh_") and lowered.endswith("_bigtts"):
        return text  # 原样透传，交给服务端判
    return None


def _prefs_path(prefs_path: Optional[Any] = None):
    if prefs_path is not None:
        from pathlib import Path

        return Path(prefs_path)
    # 调用时解析，禁模块级常量（`DESKMATE_DIR` 可被测试/多实例改写）
    from pathlib import Path

    from coworker.secrets import state_dir

    return Path(state_dir()) / "prefs.json"


def load_saved_voice(prefs_path: Optional[Any] = None) -> Optional[str]:
    """读 `prefs.json:voice_reply_voice`。文件损坏/缺键一律当没配过，绝不抛。"""
    try:
        path = _prefs_path(prefs_path)
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(VOICE_PREFS_KEY)
    return value.strip() if isinstance(value, str) and value.strip() else None


def save_voice(voice: Any, *, prefs_path: Optional[Any] = None) -> str:
    """把音色写进 `prefs.json`，**保留其他键**并原子写。

    不用 `~/.deskmate/settings.json`（那是另一套、且含隐私配置），也不用独立文件：
    `prefs.json` 已是同类运行时偏好的家（`memory_llm_group` 就在这），读写风格一致。
    """
    resolved = resolve_voice(voice)
    if not resolved:
        raise VoiceReplyError(f"unknown voice: {voice!r}")
    path = _prefs_path(prefs_path)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}  # 坏了就重建，但不能因此丢掉本次设置
    data[VOICE_PREFS_KEY] = resolved
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return resolved


def current_voice(
    *,
    explicit: Optional[str] = None,
    env: Optional[Any] = None,
    prefs_path: Optional[Any] = None,
) -> str:
    """优先级：显式传参 > `DESKMATE_TTS_VOICE` > `prefs.json` > 默认。

    env 排第二是刻意的：临时试听不改持久配置，`DESKMATE_TTS_VOICE=xxx` 起一次就够。
    """
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    source = env if env is not None else os.environ
    from_env = str(source.get("DESKMATE_TTS_VOICE") or "").strip()
    if from_env:
        return from_env
    saved = load_saved_voice(prefs_path)
    if saved:
        return saved
    return DEFAULT_VOICE


def _voice_choices_text() -> str:
    return "、".join(
        f"{p['label']}({'/'.join(p['names'][:1]) or alias})"
        for alias, p in VOICE_PRESETS.items()
    )


def _set_voice_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": SET_VOICE_TOOL_NAME,
            "description": (
                "Change the voice used for voice replies (朗读音色) and remember it for later turns. "
                "Call when the user asks to switch/set the reading voice — e.g. 「换个声音」"
                "「换个女声」「以后用御姐音念」「换成男声」「朗读声音换成 XX」, or "
                "'change your voice' / 'use a male voice'. "
                "Available voices: " + _voice_choices_text() + ". "
                "Pass the alias or Chinese name the user said. "
                "Call with NO argument when the user asks what voices are available "
                "（「都有什么声音」「能换哪些音色」）— the result lists them. "
                "This only affects text-to-speech; it does not change the model or persona."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "voice": {
                        "type": "string",
                        "description": (
                            "Voice alias / Chinese name / full speaker ID, e.g. "
                            "'vivi'、'御姐'、'儒雅逸辰'、'zh_male_ruyayichen_uranus_bigtts'。"
                            "Omit to list available voices."
                        ),
                    }
                },
                "required": [],
            },
        },
    }


def make_set_voice_tool(prefs_path: Optional[Any] = None) -> Callable[..., dict[str, Any]]:
    """造「换音色」工具：带 `voice` 参数；不带参调用＝列出可选音色。"""

    def set_voice_reply_voice(voice: Optional[str] = None) -> dict[str, Any]:
        if not str(voice or "").strip():
            return {
                "ok": True,
                "action": "list",
                "current": load_saved_voice(prefs_path) or DEFAULT_VOICE,
                "voices": voice_catalog(),
            }
        try:
            resolved = save_voice(voice, prefs_path=prefs_path)
        except Exception as exc:  # fail-open：换音色失败不能拖垮主聊天
            return {
                "ok": False,
                "reason": str(exc),
                "voices": voice_catalog(),
            }
        label = next(
            (p["label"] for p in VOICE_PRESETS.values() if p["id"] == resolved), resolved
        )
        return {"ok": True, "action": "set", "voice": resolved, "label": label}

    set_voice_reply_voice.__name__ = SET_VOICE_TOOL_NAME
    set_voice_reply_voice.__doc__ = _set_voice_schema()["function"]["description"]
    set_voice_reply_voice.__aisuite_tool_metadata__ = ai.ToolMetadata(
        name=SET_VOICE_TOOL_NAME,
        category="voice",
        risk_level="low",
        capabilities=["configure"],
        requires_approval=False,
    )
    set_voice_reply_voice.__coworker_schema__ = _set_voice_schema()
    return set_voice_reply_voice


# ---------------------------------------------------------------- 请求形状


def build_tts_headers(
    api_key: str, *, resource_id: Optional[str] = None, request_id: Optional[str] = None
) -> dict[str, str]:
    """豆包 Plan 用 `X-Api-*`，**不是** `Authorization: Bearer`。"""
    return {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
        # 字符串场景的空值兜底是安全的（coerce 红线只约束数值）
        "X-Api-Resource-Id": resource_id or DEFAULT_RESOURCE_ID,
        "X-Api-Request-Id": request_id or str(uuid.uuid4()),
        "X-Control-Require-Usage-Tokens-Return": "*",
    }


def build_tts_payload(
    text: str, *, voice: Optional[str] = None, prefs_path: Optional[Any] = None
) -> dict[str, Any]:
    """`req_params` 形状，不是 OpenAI speech 的 `{model,input,voice,response_format}`。"""
    return {
        "user": {"uid": "xiaoju-voice-reply"},
        "req_params": {
            "text": text,
            "speaker": current_voice(explicit=voice, prefs_path=prefs_path),
            "audio_params": {"format": "mp3", "sample_rate": 24000},
        },
    }


def parse_ndjson_audio(raw: bytes | str) -> bytes:
    """把流式 NDJSON 拼成完整 mp3。

    实测响应夹着 `{"code":0,"data":null,"sentence":...}` 与结束行的 `usage`，
    这些行 `data` 是 null，跳过；`code` 非 0 非 20000000 是服务端错误；
    没有结束码说明流被截断，绝不当成一段短音频播出去。
    """
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
    chunks: list[bytes] = []
    saw_end = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # 代理噪声行，忽略
        if not isinstance(row, dict):
            continue
        code = row.get("code")
        if code == END_CODE:
            saw_end = True
            continue
        if code not in (0, None):
            raise VoiceReplyError(
                f"TTS service error code={code}: {row.get('message') or ''}"
            )
        data = row.get("data")
        if isinstance(data, str) and data:
            try:
                chunks.append(base64.b64decode(data, validate=True))
            except (ValueError, binascii.Error) as exc:
                raise VoiceReplyError(f"invalid base64 audio chunk: {exc}") from exc
    if not saw_end:
        raise VoiceReplyError("TTS stream ended without end marker")
    audio = b"".join(chunks)
    if not audio:
        raise VoiceReplyError("TTS stream returned no audio")
    return audio


# ---------------------------------------------------------------- 传输 / 播放


def _curl_post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> bytes:
    """默认传输走 curl（绕开 python urllib 在部分网络下的证书链问题）。

    header 写进 curl config 文件而不是命令行，避免 api key 出现在进程参数里。
    """
    with tempfile.TemporaryDirectory(prefix="xj_tts_") as td:
        body_path = os.path.join(td, "body.json")
        cfg_path = os.path.join(td, "curl.cfg")
        out_path = os.path.join(td, "resp.txt")
        with open(body_path, "wb") as fh:
            fh.write(body)
        with open(cfg_path, "w", encoding="utf-8") as fh:
            for key, value in headers.items():
                fh.write(f'header = "{key}: {value}"\n')
            fh.write(f'data-binary = "@{body_path}"\n')
        cmd = [
            "curl", "-sS", "--max-time", str(int(timeout)),
            "-X", "POST", "-K", cfg_path, "-o", out_path,
            "-w", "%{http_code}", url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
        status = (proc.stdout or "").strip()
        if proc.returncode != 0 or not status.startswith("2"):
            raise VoiceReplyError(
                f"TTS HTTP {status or 'no response'}: {(proc.stderr or '')[:200]}"
            )
        with open(out_path, "rb") as fh:
            return fh.read()


def _afplay(data: bytes, suffix: str) -> bool:
    """macOS 本机扬声器。`afplay` 是系统自带命令，无需打进 bundle。"""
    fd, path = tempfile.mkstemp(prefix="xj_voice_", suffix=suffix or ".mp3")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        proc = subprocess.run(["afplay", path], capture_output=True, timeout=180)
        return proc.returncode == 0
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _resolve_api_key(secrets: Any) -> Optional[str]:
    """只认 SecretStore 的 `provider:ark-agent-plan-cn`；不碰 settings.json / .env。"""
    store = secrets
    if store is None:
        # 延迟 import：模块顶层零密钥读取、零 import 期副作用，云端 import 绝对安全
        from coworker.secrets import SecretStore

        store = SecretStore()
    try:
        profile = store.get(AGENT_PLAN_PROFILE) or {}
    except Exception:
        return None
    key = profile.get("api_key")
    return key.strip() if isinstance(key, str) and key.strip() else None


def synthesize(
    text: str,
    *,
    secrets: Any = None,
    poster: Optional[Callable[..., bytes]] = None,
    voice: Optional[str] = None,
    resource_id: Optional[str] = None,
    request_id: Optional[str] = None,
    timeout: Optional[float] = None,
) -> bytes:
    """合成一段 mp3。抛 `VoiceReplyError`；由 `speak_reply` 统一降级。"""
    api_key = _resolve_api_key(secrets)
    if not api_key:
        raise VoiceReplyError("no Agent Plan TTS key configured")
    headers = build_tts_headers(api_key, resource_id=resource_id, request_id=request_id)
    body = json.dumps(build_tts_payload(text, voice=voice), ensure_ascii=False).encode("utf-8")
    post = poster or _curl_post
    raw = post(TTS_URL, headers, body, DEFAULT_TIMEOUT if timeout is None else timeout)
    return parse_ndjson_audio(raw)


def _prepare_speech_text(text: str, max_chars: Optional[int]) -> tuple[str, bool]:
    cleaned = clean_for_speech(text or "")
    if not cleaned:
        return "", False
    return truncate_for_speech(cleaned, max_chars=max_chars)


def estimate_audio_seconds(
    data: bytes, *, bitrate_kbps: int = DEFAULT_BITRATE_KBPS
) -> float:
    """按码率估算时长，给前端的语音条先定宽度。

    精确值由前端 `Audio.duration` 拿到后校正，这里只要别差一个量级。
    实测 64kbps、24kHz：34221B ↔ 4.27s，公式吻合。
    """
    if not data:
        return 0.0
    kbps = as_int(bitrate_kbps, DEFAULT_BITRATE_KBPS)
    if kbps <= 0:
        kbps = DEFAULT_BITRATE_KBPS
    return round(len(data) * 8 / (kbps * 1000), 1)


def build_voice_message(
    text: str,
    *,
    secrets: Any = None,
    poster: Optional[Callable[..., bytes]] = None,
    voice: Optional[str] = None,
    max_chars: Optional[int] = None,
) -> dict[str, Any]:
    """合成一条语音消息（**不播放**）。同样绝不抛异常。

    与 `speak_reply` 的唯一区别是最后一步：这里把音频交回去，由 WS 层发成
    聊天里的语音气泡；`speak_reply` 则直接 `afplay`。
    """
    limit = DEFAULT_MESSAGE_MAX_CHARS if max_chars is None else max_chars
    spoken, truncated = _prepare_speech_text(text, limit)
    if not spoken:
        return {
            "ok": False, "reason": "empty reply text", "text": "", "audio": b"",
            "seconds": 0.0, "truncated": False,
        }
    try:
        audio = synthesize(spoken, secrets=secrets, poster=poster, voice=voice)
        return {
            "ok": True,
            "reason": "",
            "text": spoken,
            "truncated": truncated,
            "audio": audio,
            "seconds": estimate_audio_seconds(audio),
        }
    except Exception as exc:  # fail-open：气泡发不出去也不能拖垮主聊天
        log.warning("voice message failed (fail-open): %s", exc)
        return {
            "ok": False, "reason": str(exc), "text": spoken,
            "audio": b"", "seconds": 0.0, "truncated": truncated,
        }


def speak_reply(
    text: str,
    *,
    secrets: Any = None,
    poster: Optional[Callable[..., bytes]] = None,
    player: Optional[Callable[[bytes, str], bool]] = None,
    voice: Optional[str] = None,
    max_chars: Optional[int] = None,
) -> dict[str, Any]:
    """清洗 → 合成 → 播放。**绝不抛异常**，返回结构化结果供调用方记日志。"""
    spoken, truncated = _prepare_speech_text(text, max_chars)
    if not spoken:
        return {"ok": False, "reason": "empty reply text", "text": "", "audio_bytes": 0}
    if truncated:
        log.warning("voice reply truncated: %d chars kept", len(spoken))
    try:
        audio = synthesize(spoken, secrets=secrets, poster=poster, voice=voice)
        play = player or _afplay
        ok = bool(play(audio, ".mp3"))
        return {
            "ok": ok,
            "reason": "" if ok else "player returned false",
            "text": spoken,
            "chars": len(spoken),
            "truncated": truncated,
            "audio_bytes": len(audio),
        }
    except Exception as exc:  # fail-open：朗读永远不能拖垮主聊天
        log.warning("voice reply failed (fail-open): %s", exc)
        return {
            "ok": False,
            "reason": str(exc),
            "text": spoken,
            "chars": len(spoken),
            "truncated": truncated,
            "audio_bytes": 0,
        }
