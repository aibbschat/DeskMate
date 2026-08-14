"""记忆抽取 LLM helper - 统一走 OpenWorker 的 SecretStore 体系。

不再从 .env / os.environ 读 DESKMATE_API_KEY 等变量。
改为复用 OpenWorker 的 provider 体系：
  1. 从 prefs.json 读 default_model（如 "xiaomi:mimo-v2.5-pro"）
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

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini"


def _state_dir() -> Path:
    """OpenWorker 的状态目录，与 SecretStore.state_dir() 一致。"""
    base = os.environ.get("COWORKER_STATE_DIR")
    if base:
        return Path(base).expanduser()
    return Path.home() / ".config" / "coworker"


def _resolve_provider() -> tuple[str, str, str]:
    """从 SecretStore + prefs.json 解析当前激活 provider 的凭证。

    Returns (api_key, base_url, model_name)。
    model_name 是去掉了 provider 前缀的裸模型名（如 "mimo-v2.5-pro"）。
    """
    state = _state_dir()
    secrets_path = state / "secrets.json"
    prefs_path = state / "prefs.json"

    # 1. 读 prefs.json 获取 default_model（决定用哪个 provider）
    default_model = ""
    if prefs_path.is_file():
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            default_model = prefs.get("default_model", "")
        except (OSError, json.JSONDecodeError):
            pass

    # 2. 解析 provider 名（从 model 字符串前缀，如 "xiaomi:mimo-v2.5-pro" -> "xiaomi"）
    provider_name = ""
    bare_model = default_model
    if ":" in default_model:
        prefix, rest = default_model.split(":", 1)
        # 只在 prefix 是已知 provider 时才拆分（与 ProviderRouter._bare 逻辑一致）
        from coworker.providers.registry import get_descriptor
        if get_descriptor(prefix) is not None:
            provider_name = prefix
            bare_model = rest

    # 3. 从 SecretStore 读 provider profile
    api_key = ""
    base_url = ""
    if provider_name and secrets_path.is_file():
        try:
            data = json.loads(secrets_path.read_text(encoding="utf-8"))
            profile = data.get(f"provider:{provider_name}")
            if isinstance(profile, dict):
                api_key = profile.get("api_key", "")
                base_url = profile.get("base_url", "")
        except (OSError, json.JSONDecodeError):
            pass

    # 4. Fallback: 如果 default_model 没配但有 provider:xiaomi，用它（历史兼容）
    if not api_key and not provider_name and secrets_path.is_file():
        try:
            data = json.loads(secrets_path.read_text(encoding="utf-8"))
            # 按优先级试已知 provider
            for pname in ("xiaomi", "deepseek", "qwen", "openai"):
                profile = data.get(f"provider:{pname}")
                if isinstance(profile, dict) and profile.get("api_key"):
                    api_key = profile["api_key"]
                    base_url = profile.get("base_url", "")
                    provider_name = pname
                    if not bare_model:
                        bare_model = _DEFAULT_MODEL
                    break
        except (OSError, json.JSONDecodeError):
            pass

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
    """
    from openai import OpenAI

    client = OpenAI(api_key=_api_key(), base_url=_base_url(), timeout=timeout)
    resp = client.chat.completions.create(
        model=_model(),
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


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

            client = OpenAI(api_key=_api_key(), base_url=_base_url(), timeout=timeout)
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
    """解析 OmniRoute 凭证：优先读 secrets.json 的 ``provider:omniroute`` 配置，否则用本地默认（免密钥）。"""
    state = _state_dir()
    secrets_path = state / "secrets.json"
    if secrets_path.is_file():
        try:
            data = json.loads(secrets_path.read_text(encoding="utf-8"))
            prof = data.get("provider:omniroute")
            if isinstance(prof, dict):
                return (
                    prof.get("api_key") or _OMNIROUTE_API_KEY,
                    prof.get("base_url") or _OMNIROUTE_BASE_URL,
                    prof.get("model") or model,
                )
        except (OSError, json.JSONDecodeError):
            pass
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
) -> str:
    """记忆抽取专用 chat：默认走付费主对话 provider（保中文一字不差）。

    与 ``chat()`` 同签名（少流式），调用方用于后台自动沉淀记忆。
    仅当显式设 ``DESKMATE_MEMORY_PROVIDER=omniroute`` 时走本机免费网关；
    免费网关异常时自动降级回付费主模型重试一次，保证记忆不丢（宁可走付费也不静默失败）。
    """
    ak, bu, md = _resolve_memory_provider(reasoning=reasoning)
    from openai import OpenAI

    def _do(api_key: str, base_url: str, model: str) -> str:
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
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

    try:
        return _do(ak, bu, md)
    except Exception:
        # OmniRoute 免费网关不可用 / 异常：降级回主对话 provider，保证记忆不丢
        if _memory_use_omniroute(reasoning):
            lak, lbu, lmd = _resolve_provider()
            if (lak, lbu, lmd) != (ak, bu, md):
                try:
                    return _do(lak, lbu, lmd)
                except Exception:
                    pass
        raise
