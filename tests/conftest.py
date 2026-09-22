"""Shared pytest fixtures.

`fake_slack` boots the in-process FakeSlack harness on an ephemeral port and points the Slack
adapter at it via `SLACK_API_URL`, so the real `SlackAdapter` / `slack_bolt` stack runs
end-to-end with no network, tokens, or the Slack app console. See
`coworker.testing.fake_slack` and `platform/docs/FAKE-SLACK-SPEC.md`.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from coworker.testing.fake_slack import FakeSlack


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """EVERY test gets an isolated SecretStore/state dir. Without this, any test that builds
    a SessionManager reads the developer's real machine-global state — including their cloud
    sign-in, which made test session creation emit REAL telemetry to prod (found 2026-07-03
    as burst noise in the ocw-connect-telemetry-events table)."""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "coworker-state"))
    # Universal scratch provisions a per-session dir for EVERY session — without this,
    # tests would mkdir under the developer's real ~/OpenWorker.
    monkeypatch.setenv("COWORKER_SCRATCH_BASE", str(tmp_path / "coworker-scratch"))
    # 隔离 deskmate 全局数据目录：各业务模块硬编码 ``Path.home()/".deskmate"`` 兜底，
    # 不隔离会污染开发者真机 ~/.deskmate（含神圣稿件、真实认知层/成长/意志数据）。
    # 多数模块按调用时取值读 DESKMATE_DIR，此处设为 tmp 即可兜住；少数在 import 时
    # 绑定模块级常量的，由各测试单独 setenv（见 test_deskmate_will 等）。
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path / "deskmate"))
    # 语义检索后端链：测试一律**显式关闭**（`_backend_order()` 返回 [] → 立即 fail-open）。
    # 为什么必须钉死：fastembed 是 optional 依赖，一旦本机装了它，`auto` 就会走本地 BGE，
    # 而每个用例的 DESKMATE_DIR 都是全新的 tmp → 各自去下 ~102MB 模型再跑推理
    # （实测 7 个用例 × 102MB，全量耗时从 110s 涨到 4 分钟+，且测试变成依赖外网）。
    # 测试结果不该取决于「本机恰好装没装某个 optional 依赖」。要测后端链/护栏的用例，
    # 自己在用例里 patch `local_embed.available` / `embed` / `_call_embedding_api`。
    monkeypatch.setenv("DESKMATE_EMBEDDING_BACKEND", "off")
    monkeypatch.delenv("COWORKER_API_TOKEN", raising=False)


@pytest_asyncio.fixture
async def fake_slack(monkeypatch):
    """A running FakeSlack control object; `SLACK_API_URL` is set to it for the test's duration.

    The harness is strictly in-process (loopback WebSocket, no network), so it must not inherit
    the developer machine's HTTP(S)_PROXY — slack_sdk's Socket Mode client reads those from the
    environment and would route the loopback WS through the proxy, which 404s the handshake.
    """
    for _v in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(_v, raising=False)
    fake = FakeSlack()
    await fake.start()
    monkeypatch.setenv("SLACK_API_URL", fake.api_url)
    try:
        yield fake
    finally:
        await fake.stop()
