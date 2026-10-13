"""Kilo 免费网关 provider — Our Free Model 插件的第二条匿名车道（2026-10-13 移植）。

Kilo AI（https://kilo.ai）发布了一个 OpenAI 兼容网关，其**免费池免密钥、免登录、
无指纹门**（实测：不带任何 Authorization 头即可推理；带任意 Bearer 反而
``INVALID_TOKEN``）。协议来源：~/.dsh/our-free-model/rollback/src/kilo.js
（插件 v2.x 的 Kilo 车道），2026-10-13 实测验证后移植。

与 :mod:`coworker.providers.opencode_provider` 的对比：

| | opencode 车道 | Kilo 车道 |
|---|---|---|
| 凭证 | 匿名 ``Bearer public`` 字面量 | **完全不发 Authorization** |
| 请求头 | CLI 伪装头 + UA | 仅 UA（``dsh-our-free-model``） |
| 请求体 | bash/read 门面工具 + 仅流式 | 普通 OpenAI 形状，流式/非流式均可 |
| 复杂度 | 高（会话 id 派生 + 门面） | 低（固定 base + 普通 POST） |

**隐私 trade-off（插件 README 原话，萧哥 10-13 知情拍板）**：Kilo 免费池的 prompt
**可能被上游提供方记录**。因此本车道只用于后台加工（发帖草稿 / 理解加工 / 自主层），
**记忆抽取已切回付费组**（萧哥 10-13「乙」方案：`memory_group` 默认 paid）。

⚠️ **Authorization 头必须剥掉**：OpenAI SDK 只要 api_key 非空就会发
``Authorization: Bearer <key>``，而 Kilo 网关对任何非空 token 一律
``INVALID_TOKEN``。做法是给 httpx client 挂 request event hook，在请求发出前
删掉 authorization 头（实测有效，见 tests/test_kilo_provider.py）。
"""

from __future__ import annotations

from typing import Any, Optional

from .base import AssistantTurn
from .openai_provider import OpenAIProvider

KILO_BASE_URL = "https://api.kilo.ai/api/gateway"
#: SDK 要求非空 api_key；值本身无所谓——发出前会被 hook 剥掉（见 _strip_authorization）。
KILO_PLACEHOLDER_KEY = "kilo"
#: 探针/默认模型：免费池的自动路由入口（ listing 里排第一，永不因单模型下架而 404）。
KILO_AUTO_MODEL = "kilo-auto/free"
#: 与插件一致的 UA（归属卫生；网关不校验，实测无 UA 也通）。
KILO_USER_AGENT = "dsh-our-free-model"


def _strip_authorization(request: Any):
    """httpx request event hook：删掉 SDK 自动加的 Authorization 头。

    Kilo 免费池把任何非空 token 当无效凭证（``INVALID_TOKEN``），只有**完全没有
    Authorization 头**（或空 token）才放行。event hook 在请求构建后、发出前运行，
    改 header 即生效，无需碰 SDK 内部。
    """
    request.headers.pop("authorization", None)
    return request


def build_kilo_client(
    base_url: Optional[str] = None, *, trust_env: bool = True, transport: Any = None
) -> Any:
    """构造指向 Kilo 网关的 OpenAI client（免密钥 + 剥 Authorization + 可选代理尊重）。

    ``trust_env=False`` 给本机回环代理场景用（侧车继承的开发代理挂掉时会劫持
    外网调用，见 ``deskmate.memory.llm._proxy_is_loopback`` 的同款判断）。
    ``transport`` 仅测试注入（``httpx.MockTransport``），生产不传。
    """
    import httpx
    from openai import OpenAI

    http = httpx.Client(
        trust_env=trust_env,
        transport=transport,
        event_hooks={"request": [_strip_authorization]},
    )
    return OpenAI(
        api_key=KILO_PLACEHOLDER_KEY,
        base_url=((base_url or "").strip().rstrip("/") or KILO_BASE_URL),
        http_client=http,
    )


class KiloProvider(OpenAIProvider):
    """Kilo 免费池的 OpenAI Chat Completions provider。

    与基类的差别只有两处：默认 base_url 指向 Kilo 网关、client 自带剥
    Authorization 的 hook。其余（tools / 流式 / 参数修复重试）全部沿用基类——
    Kilo 是普通 OpenAI 兼容端点，没有 opencode 那种门面协议。
    """

    def __init__(
        self,
        client: Any = None,
        *,
        default_model: str = KILO_AUTO_MODEL,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        secrets: Any = None,
    ):
        super().__init__(
            client if client is not None else build_kilo_client(base_url),
            default_model=default_model,
            api_key=(api_key or "").strip() or KILO_PLACEHOLDER_KEY,
            base_url=(base_url or "").strip() or KILO_BASE_URL,
            secrets=secrets,
        )

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **settings: Any,
    ) -> AssistantTurn:
        settings.setdefault("extra_headers", {"user-agent": KILO_USER_AGENT})
        return super().complete(
            model=model, messages=messages, tools=tools, **settings
        )

    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **settings: Any,
    ):
        settings.setdefault("extra_headers", {"user-agent": KILO_USER_AGENT})
        yield from super().stream(
            model=model, messages=messages, tools=tools, **settings
        )


def probe_kilo_lane(base_url: Optional[str] = None, timeout: float = 15.0) -> dict[str, Any]:
    """Test 按钮的存活探针：对自动路由入口发一个最小非流式请求。

    200 = 可用（免费池免密钥）；其余状态按网关错误信封归类。与
    :func:`coworker.providers.opencode_provider.probe_free_lane` 同形，供设置页
    「Test」按钮复用。
    """
    import httpx

    base = ((base_url or "").strip().rstrip("/") or KILO_BASE_URL).rstrip("/")
    try:
        resp = httpx.post(
            base + "/chat/completions",
            headers={
                "content-type": "application/json",
                "user-agent": KILO_USER_AGENT,
            },
            json={
                "model": KILO_AUTO_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "max_tokens": 8,
            },
            timeout=timeout,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"连接 Kilo 免费网关失败（{exc.__class__.__name__}）。请检查网络。",
        }
    if resp.status_code < 300:
        return {"ok": True}
    body = ""
    try:
        payload = resp.json()
        body = str((payload or {}).get("error", {}).get("message", ""))[:120]
    except ValueError:
        body = resp.text[:120]
    return {
        "ok": False,
        "error": f"Kilo 免费网关返回 HTTP {resp.status_code}。{body}".strip(),
    }
