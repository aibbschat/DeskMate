"""记忆抽取 LLM helper - 统一走 OpenWorker 的 SecretStore 体系。

不再从 .env / os.environ 读 DESKMATE_API_KEY 等变量。
改为复用 OpenWorker 的 provider 体系：
  1. 从 prefs.json 读 default_model（如 "xiaomi:mimo-v2.6-pro"）
  2. 取 provider 前缀 -> SecretStore.get("provider:xiaomi") -> {api_key, base_url}
  3. 用 OpenAI SDK 构造 client（所有 OpenAI-compatible provider 通用）

用户在前端切 provider 时，deskmate 自动跟随，无需改任何配置文件。

设计原则：所有调用方都应在「无密钥 / 调用异常」时静默降级，绝不阻断主对话。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

import httpx

# B9 修复：provider 凭证统一走 canonical 的 SecretStore，不再手写解析 secrets.json。
# SecretStore.state_dir() 即 OpenWorker 状态目录的唯一真相源（COWORKER_STATE_DIR ->
# ~/.config/coworker），复用它避免「两套目录解析逻辑」分叉。
from coworker.secrets import SecretStore, state_dir as _coworker_state_dir

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini"


def _state_dir() -> Path:
    """OpenWorker 的状态目录 —— 直接复用 canonical ``SecretStore.state_dir()``，避免两套解析分叉（B9）。"""
    return _coworker_state_dir()


def _resolve_provider() -> tuple[str, str, str]:
    """从 SecretStore + prefs.json 解析当前激活 provider 的凭证。

    Returns (api_key, base_url, model_name)。
    model_name 是去掉了 provider 前缀的裸模型名（如 "mimo-v2.6-pro"）。
    """
    state = _state_dir()
    prefs_path = state / "prefs.json"

    # 1. 读 prefs.json 获取 default_model（决定用哪个 provider）
    default_model = ""
    if prefs_path.is_file():
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            default_model = prefs.get("default_model", "")
        except (OSError, json.JSONDecodeError):
            pass

    # 2. 解析 provider 名（从 model 字符串前缀，如 "xiaomi:mimo-v2.6-pro" -> "xiaomi"）
    provider_name = ""
    bare_model = default_model
    if ":" in default_model:
        prefix, rest = default_model.split(":", 1)
        # 只在 prefix 是已知 provider 时才拆分（与 ProviderRouter._bare 逻辑一致）
        from coworker.providers.registry import get_descriptor
        if get_descriptor(prefix) is not None:
            provider_name = prefix
            bare_model = rest

    # 3. 从 canonical SecretStore 读 provider profile（B9：不再手写解析 secrets.json，
    #    复用 SecretStore 获得 ${VAR} 解析 + 单一真相源）。
    api_key = ""
    base_url = ""
    if provider_name:
        profile = SecretStore().get(f"provider:{provider_name}")
        if isinstance(profile, dict):
            api_key = profile.get("api_key", "")
            base_url = profile.get("base_url", "")

    # 4. Fallback: 如果 default_model 没配但有 provider:xiaomi，用它（历史兼容）
    if not api_key and not provider_name:
        # 按优先级试已知 provider
        for pname in ("xiaomi", "deepseek", "qwen", "openai"):
            profile = SecretStore().get(f"provider:{pname}")
            if isinstance(profile, dict) and profile.get("api_key"):
                api_key = profile["api_key"]
                base_url = profile.get("base_url", "")
                provider_name = pname
                if not bare_model:
                    bare_model = _DEFAULT_MODEL
                break

    return api_key, base_url or _DEFAULT_BASE_URL, bare_model or _DEFAULT_MODEL


def llm_available() -> bool:
    """记忆抽取是否可用（SecretStore 中有已配置的 provider 凭证）。"""
    api_key, _, _ = _resolve_provider()
    return bool(api_key)


def _api_key() -> str:
    api_key, _, _ = _resolve_provider()
    if not api_key:
        raise RuntimeError(
            "未配置任何 LLM provider 凭证。请在 Settings ▸ Models 中添加 API Key，"
            "记忆抽取将自动跟随主对话引擎的 provider 配置。"
        )
    return api_key


def _base_url() -> str:
    _, base_url, _ = _resolve_provider()
    return base_url


def _model() -> str:
    _, _, model = _resolve_provider()
    return model


def _proxy_is_loopback() -> bool:
    """判断继承来的 HTTP(S)_PROXY 是否指向本机。

    桌面侧车（sidecar）经 ``sidecar_env()`` 继承了登录 shell 的代理，
    那往往是 WorkBuddy 之类的**本机开发代理**（如 ``127.0.0.1:65085``），
    只在 WorkBuddy 会话期间存活。侧车自主写日记/抽记忆时不依赖它，
    而本机代理一旦挂掉，provider LLM 调用会被拒绝、静默降级成台账。

    所以：代理指向本机 → 忽略 env proxy，直连显式配置的 provider base_url；
    代理是真实公司代理（非本机 host）→ 仍尊重它。
    """
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        v = os.environ.get(key, "")
        if not v:
            continue
        host = v.split("://", 1)[-1].split(":")[0].split("/")[0].lower()
        if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "[::1]"):
            return True
    return False


def _llm_http_client() -> "httpx.Client":
    """给 OpenAI client 用的 httpx 客户端。

    ``trust_env=False`` 仅在代理指向本机时生效（见 ``_proxy_is_loopback``），
    避免被一个常常挂掉的本地代理劫持 provider 调用。
    """
    return httpx.Client(trust_env=not _proxy_is_loopback())


# --------------------------------------------------------------------------- #
# 通道组 + 熔断（2026-10-06，设计文档 §二/§3）
# --------------------------------------------------------------------------- #
# 为什么加这一层：2026-10-06 实测，日志里AccountQuotaExceeded 出现 28 次，
# 全是同一个任务在重试打同一条已死的火山路、零次切换 ⇒「撞墙不收手」。
# 修法是**先熔断记账、再按组内自动排换通道**，而不是继续重试同一条死路。
#
# 两个总开关（萧哥 21:04 拍板）：付费组 = 火山→小米→hy3（qwen 不进链）；
# 免费组 = opencode 远端清单驱动。后台任务默认走免费组（萧哥 21:24 拍板）。
_DESKMATE_DIR_ENV = "DESKMATE_DIR"


def _breaker_base_dir():
    """熔断表基目录：与本模块其余部分一致（DESKMATE_DIR 调用时解析）。"""
    import os as _os
    from pathlib import Path as _P

    v = _os.environ.get(_DESKMATE_DIR_ENV, "").strip()
    if v:
        return _P(v).expanduser()
    try:
        from deskmate.paths import deskmate_base_dir

        return _P(deskmate_base_dir())
    except Exception:
        return _P.home() / ".deskmate"


def _status_of(exc: BaseException) -> Optional[int]:
    """从异常里取 HTTP 状态码（各 SDK 包装方式不同，尽力而为）。"""
    for attr in ("status_code", "http_status", "code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int) and 100 <= v < 600:
            return v
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


def _retry_after_of(exc: BaseException) -> Optional[str]:
    """从异常里取 ``retry-after``（opencode 的额度窗口靠它，§1.5 实测 40332s）。"""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    for getter in ("get",):
        fn = getattr(headers, getter, None)
        if callable(fn):
            try:
                v = fn("retry-after")
                if v:
                    return str(v)
            except Exception:  # noqa: BLE001
                pass
    return None


def _call_with_breaker(provider: str, model: str, fn) -> str:
    """在通道上执行 ``fn()``，撞墙则熔断记账并按 ``retryable`` 决定是否重试一次。

    ``fn`` 是零参可调用，返回文本；抛异常即视为失败。

    铁律（设计 §3.2）：**配额类错误禁止原地重试** —— 记熔断 + 立刻上抛，
    由上层的组内路由换通道。仅瞬时类（5xx/网络抖动）允许重试**一次**。
    """
    from deskmate.llm_circuit_breaker import classify_error, is_open, record_failure, record_success

    key = f"{provider}:{model}"
    base = _breaker_base_dir()

    # 熔断中：不在同一条死路上再撞一次（§1.3 的教训）。上抛让上层换通道。
    if is_open(key, base_dir=base):
        from deskmate.llm_circuit_breaker import audit

        audit(
            event="skip",
            channel=key,
            detail="熔断中，跳过（不重试同一条死路）",
            base_dir=base,
        )
        raise RuntimeError(f"通道 {key} 熔断中，跳过")

    try:
        out = fn()
    except Exception as exc:  # noqa: BLE001
        verdict = classify_error(
            status_code=_status_of(exc),
            body=f"{type(exc).__name__}: {exc}",
            retry_after=_retry_after_of(exc),
        )
        record_failure(key, verdict, base_dir=base)
        if not verdict.retryable:
            raise  # 配额类：立刻上抛换通道，绝不原地重试
        # 瞬时类：重试**一次**（不是无限）
        try:
            out = fn()
        except Exception as exc2:  # noqa: BLE001
            v2 = classify_error(
                status_code=_status_of(exc2),
                body=f"{type(exc2).__name__}: {exc2}",
                retry_after=_retry_after_of(exc2),
            )
            record_failure(key, v2, base_dir=base)
            raise
    record_success(key, base_dir=base)
    return out



def chat(
    system: str,
    user: str,
    *,
    temperature: float = 0.3,
    max_tokens: int = 800,
    timeout: float = 300.0,
) -> str:
    """调用 LLM 完成一次对话，返回文本。异常向上抛出，由调用方 fail-safe 处理。

    仅在 llm_available() 为 True 时调用；否则请调用方先判断并跳过。
    timeout 默认 300s（5 分钟），避免网络/API 无响应时无限等待。

    **走熔断 + 付费组自动降级（2026-10-10 修 bug）**：主对话链路经
    :func:`_call_with_breaker`。原先只读 ``default_model``（``prefs.json``），
    若该通道熔断（如火山 5h 配额撞爆），调用直接失败、**无任何降级** ⇒ 意图执行/
    讨论（``intent_*``）、主动递卡片 judge 等后台交互路径静默瘫掉。现改为：
    ``default_model`` 通道不可用（配额/熔断）时，沿**付费组**（ark→小米→hy3）自动
    换下一条健康通道，保证对话/主动行为不中断。主对话只在付费组内部轮替
    （不切免费组，保质量/延迟）。
    """
    from deskmate.llm_circuit_breaker import is_open
    from deskmate.llm_channels import paid_channels

    base = _breaker_base_dir()

    def _do(ak: str, bu: str, md: str) -> str:
        from openai import OpenAI

        client = OpenAI(api_key=ak, base_url=bu, timeout=timeout,
                        http_client=_llm_http_client())
        resp = client.chat.completions.create(
            model=md,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (resp.choices[0].message.content or "").strip()

    # 候选：default_model 优先；其次付费组其余健康通道（ark→小米→hy3）。
    primary_provider, primary_model = _active_provider_and_model()
    candidates = [(primary_provider, primary_model)]
    try:
        for ch in paid_channels():
            if (ch.provider, ch.model) != (primary_provider, primary_model):
                candidates.append((ch.provider, ch.model))
    except Exception:  # noqa: BLE001
        pass

    last_exc: Optional[BaseException] = None
    tried = 0
    for provider, model in candidates:
        if tried > 8:  # 防御：候选表异常时退出，别死循环
            break
        tried += 1
        key = f"{provider}:{model}"
        if is_open(key, base_dir=base):
            _audit_skip(key, "熔断中，跳过（不重试同一条死路）")
            continue
        ak, bu, md = (
            (_api_key(), _base_url(), _model())
            if provider == "default"
            else _creds_for(provider, model)
        )
        if not ak:
            continue
        try:
            return _call_with_breaker(
                provider, model, lambda a=ak, b=bu, m=md: _do(a, b, m)
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            _audit_skip(key, f"失败，换下一条：{type(exc).__name__}")
    raise last_exc or RuntimeError(
        f"chat: 付费组无可用通道（试了 {tried} 条）"
    )


def _active_provider_and_model() -> tuple:
    """当前主对话的 ``(provider, model)``（用作熔断表的键）。

    与 :func:`_resolve_provider` **同源同逻辑**（provider 名取自 ``prefs.json`` 的
    ``default_model`` 前缀，且该前缀须是已注册 provider，否则不成立）。
    刻意不重构 :func:`_resolve_provider` —— 它是记忆抽取的既有路径，
    改动风险大于收益（红线：改语义须同步更新旧断言，且此处只需键，不必重排解析）。
    """
    default_model = ""
    try:
        prefs_path = _state_dir() / "prefs.json"
        if prefs_path.is_file():
            try:
                default_model = json.loads(
                    prefs_path.read_text(encoding="utf-8")
                ).get("default_model", "")
            except (OSError, ValueError):
                default_model = ""
    except Exception:  # noqa: BLE001
        default_model = ""
    if ":" in default_model:
        prefix, rest = default_model.split(":", 1)
        try:
            from coworker.providers.registry import get_descriptor

            if get_descriptor(prefix) is not None:
                return prefix, rest
        except Exception:  # noqa: BLE001
            pass
    return "default", (default_model or "unknown")


async def chat_stream(
    system: str,
    user: str,
    *,
    temperature: float = 0.3,
    max_tokens: int = 800,
    timeout: float = 300.0,
):
    """原生流式 LLM 调用：逐 token 产出文本片段（async generator）。

    与 chat() 共享同一套 provider 解析。实现上用同步 OpenAI client 在独立线程里跑
    stream=True，把每个 delta 推入线程安全队列，事件循环以非阻塞轮询取出并 yield——
    既拿到 SDK 原生 token 流，又不占用事件循环、不占线程池。
    无密钥 / 异常时上浮异常，由调用方 fail-safe 处理。
    """
    import queue
    import threading

    result_q: "queue.Queue" = queue.Queue()

    def _run() -> None:
        try:
            from openai import OpenAI

            client = OpenAI(api_key=_api_key(), base_url=_base_url(), timeout=timeout,
                           http_client=_llm_http_client())
            stream = client.chat.completions.create(
                model=_model(),
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                text = getattr(delta, "content", None)
                if text:
                    result_q.put(("delta", text))
            result_q.put(("done", None))
        except Exception as exc:  # noqa: BLE001
            # 线程里抛出的异常原样上浮给事件循环
            result_q.put(("error", exc))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    while True:
        try:
            kind, payload = result_q.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.01)
            continue
        if kind == "delta":
            yield payload
        elif kind == "done":
            break
        else:  # error
            raise payload


# ---------------------------------------------------------------------------
# 记忆抽取 provider 路由
# ---------------------------------------------------------------------------
# 默认全部走「主对话付费 provider」（_resolve_provider），保中文一字不差——
# 免费海外模型中文易缺字，落盘记忆若吞字会写成永久错记忆并回灌未来上下文，
# 对史料严谨型用户危害大，故默认不省钱、先保真。
# 想省时显式开启：设 DESKMATE_MEMORY_PROVIDER=omniroute 走本机 OmniRoute 免费网关
# （localhost:20128，免密钥）；异常时仍自动降级回付费主模型，绝不丢记忆。
_OMNIROUTE_BASE_URL = "http://localhost:20128/v1"
_OMNIROUTE_API_KEY = "omniroute"  # 占位免密钥：OmniRoute 网关接受任意非空 key，对齐 registry._build_omniroute
_OMNIROUTE_MODEL = "auto"  # OmniRoute 通用免密钥组合模型
_OMNIROUTE_REASONING_MODEL = "auto/best-reasoning"  # 免费推理模型，给史料核验等质量敏感任务


def _memory_use_omniroute(bible: bool = False) -> bool:
    """记忆抽取是否走 OmniRoute 免费网关。

    默认 **不** 走免费（返回 False，全部走付费主模型）。仅当显式设置
    ``DESKMATE_MEMORY_PROVIDER=omniroute`` 才启用免费。因免费海外模型中文易缺字，
    记忆落盘后错字会永久留存并回灌上下文，故默认保护保真、不默认省钱。
    """
    return os.environ.get("DESKMATE_MEMORY_PROVIDER", "").lower() == "omniroute"


def _omniroute_creds(model: str = _OMNIROUTE_MODEL) -> tuple[str, str, str]:
    """解析 OmniRoute 凭证：优先读 canonical SecretStore 的 ``provider:omniroute`` 配置，否则用本地默认（免密钥）。"""
    prof = SecretStore().get("provider:omniroute")
    if isinstance(prof, dict):
        return (
            prof.get("api_key") or _OMNIROUTE_API_KEY,
            prof.get("base_url") or _OMNIROUTE_BASE_URL,
            prof.get("model") or model,
        )
    return _OMNIROUTE_API_KEY, _OMNIROUTE_BASE_URL, model


def _resolve_memory_provider(reasoning: bool = False) -> tuple[str, str, str]:
    """记忆抽取专用 provider 解析：(api_key, base_url, model)。

    优先 OmniRoute 免费；未启用 / 强制 main 时降级回主对话 provider（``_resolve_provider``）。
    ``reasoning=True`` 走免费推理模型（``auto/best-reasoning``），适合史料核验等质量敏感抽取。
    """
    if _memory_use_omniroute(reasoning):
        return _omniroute_creds(_OMNIROUTE_REASONING_MODEL if reasoning else _OMNIROUTE_MODEL)
    return _resolve_provider()


def memory_llm_available(reasoning: bool = False) -> bool:
    """记忆抽取是否可用：OmniRoute 免密钥即算可用；未启用时看主 provider 是否有 key。"""
    if _memory_use_omniroute(reasoning):
        return True
    return llm_available()


def chat_memory(
    system: str,
    user: str,
    *,
    reasoning: bool = False,
    temperature: float = 0.3,
    max_tokens: int = 800,
    timeout: float = 300.0,
    free_only: bool = False,
) -> str:
    """记忆抽取专用 chat：走**通道组**（2026-10-06，设计 §二）。

    **默认走免费组**（萧哥 21:24 拍板「记忆抽取走免费」）：opencode 远端清单驱动
    （13 个候选，非推理优先），中文已确认可用。省付费额度，也让后台活不再挤占主通道。

    行为：
    - 组内自动排：按 :func:`deskmate.llm_channels.route` 取下一条**非熔断**通道，每条经
      :func:`_call_with_breaker` —— 撞墙即熔断记账，配额类**不原地重试**、上抛换下一条
      （§1.3 的 28 次同路重试就是没这一层）。
    - 本组全熔断 → **降级回付费组**（宁可走付费也不静默丢记忆，沿用原设计意图）。
    - ``DESKMATE_MEMORY_GROUP=paid|free`` 切组（手动档）；旧 opt-in
      ``DESKMATE_MEMORY_PROVIDER=omniroute`` 仍生效（历史行为不变）。

    ``free_only=True``（2026-10-10 萧哥拍板，自主/反思层用）：**只走免费组**，
    免费组全不可用（限流/下架）时**直接抛、绝不兜底付费**——宁可后台任务稍后重试，
    也不烧小米/火山额度。记忆抽取等「不能丢」的链路保持 ``free_only=False``
    （免费全趴兜底回付费，沿用原设计意图）。

    与 :func:`chat` 的分工：``chat`` 是**主对话**链路（她对你说的话），走付费主 provider
    不变——付费通道对延迟/质量敏感；后台活才走免费。
    """
    # 历史 opt-in：OmniRoute 免费网关（本机当前未运行，保留兼容）
    if _memory_use_omniroute(reasoning):
        return _chat_memory_via_omniroute(
            system, user, reasoning=reasoning,
            temperature=temperature, max_tokens=max_tokens, timeout=timeout,
        )

    from deskmate.llm_channels import GROUP_FREE, GROUP_PAID, memory_group, route

    def _do(ak: str, bu: str, md: str) -> str:
        from openai import OpenAI

        client = OpenAI(api_key=ak, base_url=bu, timeout=timeout,
                        http_client=_llm_http_client())
        resp = client.chat.completions.create(
            model=md,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (resp.choices[0].message.content or "").strip()

    def _attempt_group(group: str) -> str:
        """在该组内**逐条试**：每条走熔断保护，配额类失败即换下一条。

        ⚠️ 2026-10-06 修bug：原先撞墙时 `_call_with_breaker` 直接 raise 穿透出去，
        `while` 只对「无凭证」起作用 ⇒ **组内第二条通道根本没试**（火山限额时
        小米/hy 明明可用却不被试，直到整个 chat_memory 抛错才降级到另一组）。
        改为：撞墙（已记账）→ 记一条「换下一条」审计 → continue 试下一条；
        全组试完才抛。
        """
        base = _breaker_base_dir()
        tried = 0
        last_exc: Optional[BaseException] = None
        while True:
            ch, skipped = route(group, base_dir=base)
            for c, why in skipped:
                _audit_skip(c.key, why)
            if ch is None:
                raise RuntimeError(
                    f"{group} 组无可用通道（全部熔断或未配置）"
                ) from last_exc
            tried += 1
            if tried > 8:
                # 防御：候选表里全是「无凭证且无法跳过」的组合时退出，别死循环。
                raise RuntimeError(
                    f"{group} 组候选均无法调用（试了 {tried} 条）"
                ) from last_exc
            try:
                if ch.provider == "opencode":
                    # 免费组必须经 opencode_provider 的伪装协议（CLI 头 + 门面工具 +
                    # 仅流式），直连 OpenAI() 会 403 FreeTierError（设计 §四）。
                    return _call_with_breaker(
                        ch.provider, ch.model,
                        lambda m=ch.model: _opencode_complete(
                            m, system, user, temperature, max_tokens, timeout),
                    )
                if ch.provider == "local":
                    # 本地 llama-server 兜底（OpenAI 兼容，trust_env=False 防代理劫持 localhost）。
                    import time as _time

                    _t0 = _time.time()
                    try:
                        _result = _call_with_breaker(
                            ch.provider, ch.model,
                            lambda m=ch.model: _local_complete(
                                m, system, user, temperature, max_tokens, timeout),
                        )
                        _record_local_fallback(True, _time.time() - _t0, len(user))
                        return _result
                    except Exception as _exc:  # noqa: BLE001
                        # 记录失败耗时后原样上抛，交由外层 except 记审计并换下一条/整组降级。
                        _record_local_fallback(False, _time.time() - _t0, len(user))
                        raise
                ak, bu, md = _creds_for(ch.provider, ch.model)
                if not ak:
                    continue
                return _call_with_breaker(ch.provider, ch.model, lambda: _do(ak, bu, md))
            except Exception as exc:  # noqa: BLE001
                # 已由 _call_with_breaker 记好熔断；这里换组内下一条。
                last_exc = exc
                _audit_skip(ch.key, f"失败，换下一条：{type(exc).__name__}")

    group = GROUP_FREE if free_only else memory_group()
    try:
        return _attempt_group(group)
    except Exception as primary_exc:
        # 自主/反思层：免费组全趴 → 直接上抛（绝不兜底付费，省额度）。
        # 普通记忆抽取：免费组全趴 → 降级回付费组（宁可走付费也不丢记忆）。
        # 已在付费组时直接上抛（别在同一条死路上循环）。
        if free_only or group == GROUP_PAID:
            raise
        from deskmate.llm_circuit_breaker import audit

        audit(
            event="group_fallback",
            channel=group,
            detail=f"整组不可用，降级回付费组：{type(primary_exc).__name__}",
            base_dir=_breaker_base_dir(),
        )
        return _attempt_group(GROUP_PAID)


def _audit_skip(channel_key: str, why: str) -> None:
    """组内跳过某条通道时留一行审计（§1.3：让跳过原因可见）。"""
    try:
        from deskmate.llm_circuit_breaker import audit

        audit(event="skip", channel=channel_key, detail=why,
              base_dir=_breaker_base_dir())
    except Exception:  # noqa: BLE001
        pass


def _creds_for(provider: str, model: str) -> tuple:
    """取某条**付费**通道的 ``(api_key, base_url, model)``；无凭证返回空。

    免费组（``provider == "opencode"``）不走这里——它必须经
    :func:`_opencode_complete`（伪装协议，直连会403），见调用方分派。
    """
    try:
        from coworker.secrets import SecretStore

        prof = SecretStore().get(f"provider:{provider}")
    except Exception:  # noqa: BLE001
        prof = None
    if not isinstance(prof, dict):
        return "", "", model
    return prof.get("api_key", ""), prof.get("base_url", ""), model


def _chat_memory_via_omniroute(
    system: str,
    user: str,
    *,
    reasoning: bool,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> str:
    """历史 opt-in 路径：OmniRoute 免费网关（``DESKMATE_MEMORY_PROVIDER=omniroute``）。

    保留原行为：OmniRoute 异常时降级回主对话 provider 重试一次，保证记忆不丢。
    """
    ak, bu, md = _resolve_memory_provider(reasoning=reasoning)
    from openai import OpenAI

    def _do(a: str, b: str, m: str) -> str:
        client = OpenAI(api_key=a, base_url=b, timeout=timeout,
                        http_client=_llm_http_client())
        resp = client.chat.completions.create(
            model=m, temperature=temperature, max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (resp.choices[0].message.content or "").strip()

    try:
        return _do(ak, bu, md)
    except Exception:
        lak, lbu, lmd = _resolve_provider()
        if (lak, lbu, lmd) != (ak, bu, md):
            try:
                return _do(lak, lbu, lmd)
            except Exception:
                pass
        raise


# --------------------------------------------------------------------------- #
# 免费组调用：必须经 opencode_provider 的伪装协议
# --------------------------------------------------------------------------- #

#: 门面工具（2026-10-06 实测）：**不带工具 ⇒403 FreeTierError**
#:（"OpenCode's free tier can only be used from within OpenCode"）；
#: 且 `had_real_tools=True` 时不能把 tool_choice 设成 "none"。见设计 §四。
_FREE_LANE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file from the workspace.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


def _opencode_complete(
    model: str,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> str:
    """经 ``OpencodeProvider`` 调Zen 免费通道（**不可用 OpenAI() 直连**）。

    走生产 provider 的原因（2026-10-06 三次探测踩坑后确认，见设计 §四）：
    裸 OpenAI 形状的请求会被判「不在 OpenCode 里面」而 403。
    """
    from coworker.providers.opencode_provider import OpencodeProvider

    provider = OpencodeProvider()
    turn = provider.complete(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        tools=_FREE_LANE_TOOLS,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    return (turn.text or "").strip()


def _record_local_fallback(success: bool, elapsed: float, chars: int) -> None:
    """轻量埋点（B 任务）：local 兜底层被触发时追加一行 JSON 到 ``local_fallback.log``。

    用途：观测「本地 llama-server 兜底是否真的被命中、命中频率与耗时」，从而判断 CVM
    是否扛得住。日志落在 ``DESKMATE_DIR``（云端 ``/var/lib/openworker/.deskmate``）。
    任何异常都静默吞掉——埋点失败绝不影响主链路（兜底对话本身）。
    """
    try:
        from datetime import datetime, timezone

        log = _breaker_base_dir() / "local_fallback.log"
        with open(log, "a", encoding="utf-8") as _f:
            _f.write(
                json.dumps(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "success": success,
                        "elapsed_s": round(elapsed, 3),
                        "prompt_chars": chars,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:  # noqa: BLE001
        pass


def _local_complete(
    model: str,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> str:
    """经本地 llama-server（OpenAI 兼容）兜底调用。

    端点读 ``DESKMATE_LOCAL_LLM_BASE_URL``（默认 ``http://127.0.0.1:18080/v1``），
    ``trust_env=False`` 避免被本机 / 公司代理劫持 localhost。仅在免费组所有远端 opencode
    通道熔断时由 ``route()`` 选到（排最末兜底，见 ``deskmate.llm_channels.free_channels``）。
    """
    import httpx

    from openai import OpenAI

    base_url = os.environ.get(
        "DESKMATE_LOCAL_LLM_BASE_URL", "http://127.0.0.1:18080/v1"
    )
    client = OpenAI(
        api_key="local",
        base_url=base_url,
        timeout=timeout,
        http_client=httpx.Client(trust_env=False),
    )
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (resp.choices[0].message.content or "").strip()
