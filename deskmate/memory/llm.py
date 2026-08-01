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
