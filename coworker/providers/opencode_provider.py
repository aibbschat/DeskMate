"""OpenCode 免费模型 provider —— 经本机 OpenCode CLI 调用其托管的免费推理档。

重要事实（已核验）：`opencode/deepseek-v4-flash-free` 等「免费」模型是 OpenCode 自家托管
的推理服务（api.opencode.ai / models.opencode.ai，走 OpenCode 鉴权），**并非** DeepSeek 等
厂商的公开端点。桌搭不持有 OpenCode 的 key，唯一能用到这档免费模型的方式就是经 OpenCode 本身。

因此本 provider 不直连任何模型端点，而是复用 `coworker.server.opencode_a2a_server.OpenCodeDriver`
（无头调用 `opencode run`），把一段 prompt 交给本机已登录的 OpenCode，由其路由到免费档并返回文本。
它是桌搭的一个**一等公民 provider**：可在模型配置里像选 DeepSeek/MiMo 一样选
`opencode:deepseek-v4-flash-free`，但内部实现仍是委托 OpenCode——这点与直连 provider 不同，务必如实标注。

能力边界：OpenCode 是编码 agent，经 CLI 无头调用只返回整段文本，不支持工具调用、不支持 token 流式、
也不具备视觉/PDF 能力。故 `ModelCapabilities` 全部关掉，引擎会按「整段文本一次性返回」处理。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .base import AssistantTurn, ModelCapabilities, ProviderClient, StreamChunk


def _normalize_model(model: str) -> Optional[str]:
    """把传入的模型名规整成 OpenCode 认识的 `provider/name` 形式。

    入参可能是：
    - router 剥掉 `opencode:` 前缀后的裸名（如 `deepseek-v4-flash-free`）；
    - 或仍带 `opencode:` 前缀的完整串（如 `opencode:deepseek-v4-flash-free`，测试/手动调用时）；
    - 或其它 OpenCode 命名空间的 `xxx/yyy`（如 `qwen/qwen3.6-plus-free`）。

    规整规则：先去掉可能的 `provider:` 前缀，再若不含 `/` 则补 `opencode/` 前缀；
    空串 → None，由 driver 回退到 OPENCODE_MODEL 环境变量（默认 deepseek-v4-flash-free）。
    这样无论调用方传哪种形式都不会出现 `opencode/opencode:...` 这种双重前缀。
    """
    m = (model or "").strip()
    if ":" in m:
        m = m.split(":", 1)[1].strip()
    if not m:
        return None
    if "/" in m:
        return m
    return f"opencode/{m}"


class OpenCodeProvider(ProviderClient):
    """把桌搭的 completion 请求委托给本机 OpenCode CLI 的免费推理档。"""

    def __init__(self, secrets: Any = None, model: Optional[str] = None) -> None:
        # secrets 在此 provider 用不到（OpenCode 自带鉴权），保留签名以兼容工厂。
        self._default_model = model

    @staticmethod
    def _driver() -> Any:
        # 惰性导入，避免 coworker.providers 与 coworker.server 之间形成导入环。
        from coworker.server.opencode_a2a_server import OpenCodeDriver

        return OpenCodeDriver()

    @staticmethod
    def _to_prompt(messages: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for m in messages:
            role = (m.get("role") or "user").lower()
            content = m.get("content", "")
            if isinstance(content, list):
                # 多模态 content 段：只取文本部分
                content = "".join(
                    c.get("text", "") for c in content if isinstance(c, dict)
                )
            elif not isinstance(content, str):
                content = str(content)
            parts.append(f"[{role}]\n{content}")
        return "\n\n".join(parts)

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **settings: Any,
    ) -> AssistantTurn:
        prompt = self._to_prompt(messages)
        mdl = _normalize_model(model) or self._default_model
        timeout = int(settings.get("timeout", 120))
        text = self._driver().call(prompt, timeout=timeout, model=mdl)
        return AssistantTurn(text=text or None)

    def capabilities(self, model: str) -> ModelCapabilities:
        # 与 capabilities.py 中 `opencode` 分支保持一致：仅整段文本返回。
        return ModelCapabilities(
            tools=False, vision=False, parallel_tool_calls=False, streaming=False
        )

    # stream() 用基类默认实现：一次性返回整段文本（无 token 流式）。
