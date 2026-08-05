"""OpenCode A2A 适配层 —— 让 opencode 也能作为 A2A peer 被圆桌发现与调用。

背景：opencode 桌面版是 GUI Electron 应用（`ai.opencode.desktop`），主二进制直接当 CLI
调会触发 Electron 的「被其它应用调用」保护、掉进 node 帮助，**没有可脚本化的独立 CLI 二进制**。
但在 opencode 的 asar 里能看到 `--prompt` / `--print` / `--non-interactive` / `--run` / `--stdin`
这些无头参数，说明它具备无头能力，只是需要一个真正的 CLI 入口。

因此本适配层的策略是：
- 复用官方 `a2a-sdk`（canonical v1.0），对外暴露标准 Agent Card + `SendMessage` 端点；
- `OpenCodeDriver` 在收到请求时**自动探测**可用的 opencode CLI：
  * 优先看环境变量 `OPENCODE_BIN`；
  * 否则看 `PATH` 里有没有 `opencode`；
  * 找到就用无头模式（`--prompt ... --print --non-interactive`）调用，在临时目录沙箱里跑、
    并明确要求「只回答、不要改动文件」；
  * **找不到则优雅降级**：返回一段说明文字（而非崩溃），圆桌里 opencode 即「声明暂不可用」。

这样一旦用户装上 opencode CLI（`npm i -g opencode-ai` 或独立二进制），本适配层即自动生效，
无需改代码。

运行方式（独立守护进程，避免与主 openworker 的 `/` A2A 路由冲突）：
    python -m coworker.server.opencode_a2a_server
默认监听 0.0.0.0:9912，可用环境变量 `OPENCODE_A2A_URL`(对外 base url) 与 `OPENCODE_A2A_PORT` 覆盖。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile

from a2a.helpers.proto_helpers import (
    get_message_text,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.routes.rest_routes import create_rest_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TaskState,
)

logger = logging.getLogger(__name__)


class OpenCodeDriver:
    """把一段 prompt 交给 opencode（无头模式）并取回文本。

    找不到可脚本化 CLI 时返回「暂不可用」说明，绝不抛异常拖垮连接。
    """

    def detect_binary(self) -> str | None:
        env = os.environ.get("OPENCODE_BIN", "").strip()
        if env and os.path.isfile(env) and os.access(env, os.X_OK):
            return env
        return shutil.which("opencode")

    def call(self, prompt: str, timeout: int = 120) -> str:
        binpath = self.detect_binary()
        if not binpath:
            return (
                "⚠️ opencode 暂不可用：未检测到可脚本化的 opencode CLI。\n"
                "opencode 桌面版为 GUI Electron 应用，无独立 CLI 二进制；请安装 opencode CLI\n"
                "（`npm i -g opencode-ai` 或下载独立二进制）并确保在 PATH 中，或在环境变量\n"
                "OPENCODE_BIN 指定其路径，本适配层即自动生效。"
            )
        # 默认用 opencode 免费模型（实测可用、无需 API key）；可用 OPENCODE_MODEL 覆盖。
        model = os.environ.get("OPENCODE_MODEL", "opencode/deepseek-v4-flash-free")
        try:
            # opencode 是编码 agent，会在给定目录里读写文件；用临时目录沙箱，
            # 并明确要求「只回答、不要改动文件」，避免它去动用户工程。
            workdir = tempfile.mkdtemp(prefix="opencode_rt_")
            safe_prompt = (
                "你正在参与一场文学创作圆桌讨论，请仅以文字回答、"
                f"不要创建或修改任何文件。问题：{prompt}"
            )
            # 无头调用：`opencode run` 为当前 CLI 的无头执行子命令；
            # `--pure` 关闭外部插件避免干扰；模型显式指定避免依赖用户默认 provider。
            args = [binpath, "run", "--pure"]
            if model:
                args += ["-m", model]
            args.append(safe_prompt)
            proc = subprocess.run(
                args,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            out = (proc.stdout or "").strip()
            if not out:
                err = (proc.stderr or "").strip()
                return (
                    "⚠️ opencode 调用未产生文本输出。"
                    + (f"\n(stderr: {err[:600]})" if err else "")
                )
            return out
        except subprocess.TimeoutExpired:
            return f"⚠️ opencode 在 {timeout}s 内未响应（编码 agent 可能陷入长任务）。"
        except Exception as exc:  # noqa: BLE001
            logger.exception("opencode call failed")
            return f"⚠️ opencode 调用失败：{exc}"


_driver = OpenCodeDriver()


def build_opencode_card(base_url: str) -> AgentCard:
    skills = [
        AgentSkill(
            id="opencode.coding",
            name="代码与技术问答",
            description=(
                "opencode 编码 agent 的圆桌视角：从技术/工程/设定自洽角度评审文本，"
                "例如制度细节是否经得起推敲、是否存在技术性硬伤。"
            ),
            tags=["coding", "tech", "review"],
            examples=["从工程与设定自洽角度，评点这段关于明代火器的描写"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
        ),
    ]
    return AgentCard(
        name="OpenCode",
        description="opencode 编码 agent 的 A2A 适配层（无头模式调用 opencode CLI）。",
        version="1.0.0",
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
            extended_agent_card=False,
        ),
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url=base_url.rstrip("/") or "http://localhost:9912",
                protocol_version="1.0",
            )
        ],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=skills,
    )


class OpenCodeAgentExecutor(AgentExecutor):
    """把 A2A 请求交给 OpenCodeDriver。"""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if not context.current_task:
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task.id,
            context_id=task.context_id or "",
        )
        await updater.start_work(message=new_text_message("OpenCode 正在处理…"))

        query = get_message_text(context.message) or ""
        try:
            reply = await asyncio.to_thread(_driver.call, query)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OpenCode dispatch failed")
            await updater.failed(message=new_text_message(f"处理失败：{exc}"))
            return

        await updater.add_artifact(
            parts=[new_text_part(text=reply, media_type="text/plain")]
        )
        await updater.complete(message=new_text_message("OpenCode 已完成"))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if not task:
            return
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task.id,
            context_id=task.context_id or "",
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_CANCELED,
            message=new_text_message("任务已取消"),
        )


def build_request_handler(card: AgentCard) -> DefaultRequestHandler:
    return DefaultRequestHandler(
        agent_executor=OpenCodeAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )


def build_app(base_url: str | None = None):
    """构造独立的 FastAPI app（opencode A2A 守护进程用）。"""
    base_url = base_url or os.environ.get("OPENCODE_A2A_URL", "http://localhost:9912")
    card = build_opencode_card(base_url)
    handler = build_request_handler(card)
    from fastapi import FastAPI

    app = FastAPI(title="OpenCode A2A Adapter")
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/"),
        rest_routes=create_rest_routes(handler),
    )
    return app


def register_opencode_a2a(app, base_url: str | None = None) -> None:
    """（可选）把 opencode A2A 路由并入一个已有 FastAPI app 的 `/opencode` 前缀下。

    注意：Agent Card 标准路径 `/.well-known/agent-card.json` 会与 deskmate 服务端冲突，
    故一般情况下推荐用 `build_app()` 起独立守护进程（见 __main__），而非并入主 app。
    """
    base_url = base_url or os.environ.get("OPENCODE_A2A_URL", "http://localhost:9912")
    card = build_opencode_card(base_url)
    handler = build_request_handler(card)
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/opencode"),
        rest_routes=create_rest_routes(handler),
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("OPENCODE_A2A_PORT", "9912"))
    base = os.environ.get("OPENCODE_A2A_URL", f"http://localhost:{port}")
    app = build_app(base)
    uvicorn.run(app, host="0.0.0.0", port=port)
