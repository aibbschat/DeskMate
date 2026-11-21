"""Shared pytest fixtures.

`fake_slack` boots the in-process FakeSlack harness on an ephemeral port and points the Slack
adapter at it via `SLACK_API_URL`, so the real `SlackAdapter` / `slack_bolt` stack runs
end-to-end with no network, tokens, or the Slack app console. See
`coworker.testing.fake_slack` and `platform/docs/FAKE-SLACK-SPEC.md`.
"""

from __future__ import annotations

import os
from pathlib import Path

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
    # ``deskmate.teams_client.resolve_base``（创作流水线注册表、team/board 数据）只认
    # COWORKER_DATA_DIR，不认 COWORKER_STATE_DIR；漏设时 creation 系列测试会把
    # ``~/.config/coworker/creation_pipelines.json`` 写成测试桩（2026-11-11 实测）。
    monkeypatch.setenv("COWORKER_DATA_DIR", str(tmp_path / "coworker-data"))
    # ``deskmate.novel_memory.core._db_path`` 只认 COWORKER_DB；漏设时圣经回流
    # 会把 workspace 记忆写进真机 ``~/.config/coworker/coworker.db``。
    monkeypatch.setenv("COWORKER_DB", str(tmp_path / "coworker-db" / "coworker.db"))
    # Universal scratch provisions a per-session dir for EVERY session — without this,
    # tests would mkdir under the developer's real ~/OpenWorker.
    monkeypatch.setenv("COWORKER_SCRATCH_BASE", str(tmp_path / "coworker-scratch"))
    # 隔离 deskmate 全局数据目录：各业务模块硬编码 ``Path.home()/".deskmate"`` 兜底，
    # 不隔离会污染开发者真机 ~/.deskmate（含神圣稿件、真实认知层/成长/意志数据）。
    # 多数模块按调用时取值读 DESKMATE_DIR，此处设为 tmp 即可兜住；少数在 import 时
    # 绑定模块级常量的，由各测试单独 setenv（见 test_deskmate_will 等）。
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path / "deskmate"))
    # ⚠ 2026-11-11 补：``coworker/agent.py`` 的三处兜底**只认 DESKMATE_MEMORY_DIR**，
    # 不认 DESKMATE_DIR。漏设它时，真实引擎的 post_turn_hook 会把 sessions/ 与记忆抽取
    # 写进真机 ~/.deskmate（已实证：17:41 那次 USER.md 退化的注入源就是这条路径）。
    # 必须与 DESKMATE_DIR 成对设置，否则「测试已隔离」是假象。
    monkeypatch.setenv("DESKMATE_MEMORY_DIR", str(tmp_path / "deskmate"))
    # 语义检索后端链：测试一律**显式关闭**（`_backend_order()` 返回 [] → 立即 fail-open）。
    # 为什么必须钉死：fastembed 是 optional 依赖，一旦本机装了它，`auto` 就会走本地 BGE，
    # 而每个用例的 DESKMATE_DIR 都是全新的 tmp → 各自去下 ~102MB 模型再跑推理
    # （实测 7 个用例 × 102MB，全量耗时从 110s 涨到 4 分钟+，且测试变成依赖外网）。
    # 测试结果不该取决于「本机恰好装没装某个 optional 依赖」。要测后端链/护栏的用例，
    # 自己在用例里 patch `local_embed.available` / `embed` / `_call_embedding_api`。
    monkeypatch.setenv("DESKMATE_EMBEDDING_BACKEND", "off")
    monkeypatch.delenv("COWORKER_API_TOKEN", raising=False)
    # 创作定稿导出根是**模块级常量** ``deskmate.creation.docx_export.OUTPUT_DIR``
    # （import 时绑定到仓库 output/）。逐用例改到 tmp——否则 creation 系列测试每跑
    # 一次就往真实 output/ 留一批「测试概念_*.docx」（2026-11-11 实测 1779 个历史残留）。
    from deskmate.creation import docx_export

    monkeypatch.setattr(docx_export, "OUTPUT_DIR", str(tmp_path / "output"))


# ---------------------------------------------------------------------------
# 真实用户目录守卫（防回归）
# ---------------------------------------------------------------------------
# 事故：17:41 USER.md 被写成「画像」×4000。根因不是某个业务 bug，而是**测试进程
# 用了真实 base_dir**：
#   1) conftest 只设了 DESKMATE_DIR，``coworker/agent.py:360`` 只认
#      DESKMATE_MEMORY_DIR → 真实引擎的 post_turn_hook 拿到 ~/.deskmate；
#   2) ``TurnEngine`` 在 **daemon 线程**里跑 post_turn_hook（engine.py:672-690），
#      线程可能活过当前用例；
#   3) 下一个用例（test_context_budget）把 ``deskmate.memory.manager.load_user``
#      monkeypatch 成 ``"画像"*4000``，仍在跑的那条线程读到的正是替身内容，
#      于是把「画像×4000 + 提示词残句」写进真实 USER.md。
# 同类泄漏还产出过真实 sessions/ 里的测试会话、creation_pipelines.json 的 stub
# 条目、output/ 里的测试 docx。这层守卫让「跑测试写到真实目录」直接红，而不是靠人记。
_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOME = Path.home()
_DESKMATE_HOME = _HOME / ".deskmate"
_COWORKER_CFG = _HOME / ".config" / "coworker"
# 文本正文：比**内容摘要**（不是 mtime），外部进程 touch 不会误报；
# 真被写（含同 size 改写）一定变摘要。
_REAL_TEXT_FILES = (
    _DESKMATE_HOME / "USER.md",
    _DESKMATE_HOME / "MEMORY.md",
    _DESKMATE_HOME / "SOUL.md",
    _DESKMATE_HOME / "growth.json",
    _DESKMATE_HOME / "style_profile.json",
    _COWORKER_CFG / "creation_pipelines.json",
    _COWORKER_CFG / "secrets.json",
)
# SQLite 库只看**存在性**：桌面端常驻会持续写库，比较 mtime/size 必然误报；
# 但「测试凭空造出真实库」仍然要红。
_REAL_DB_FILES = (
    _DESKMATE_HOME / "deskmate.db",
    _COWORKER_CFG / "automation.db",
    _COWORKER_CFG / "coworker.db",
)
# 项目记忆递归盯着（文件多，存 mtime+size）；其余目录只记 mtime（新文件会改目录 mtime）。
_REAL_TEXT_TREES = (_DESKMATE_HOME / "projects",)
_REAL_DIR_MTIME = (
    _DESKMATE_HOME / "sessions",
    _DESKMATE_HOME / "vault",
    _DESKMATE_HOME / "diary",
    _DESKMATE_HOME / "traces",
    _REPO_ROOT / "output",
)


def _text_digest(path: Path) -> str:
    import hashlib

    return hashlib.sha1(path.read_bytes()).hexdigest()


def _snapshot_real_data() -> dict[str, tuple]:
    """固定入口快照。

    2026-11-11 复检修（m13）：原实现 `rglob` 整个 ``~/.deskmate``（1696 个文件），
    每用例 2 次约 64ms；全量 5380 用例 ≈ 5.7 分钟，且桌面端后台写库时必红。
    改为「只盯固定入口 + 按类型比较」：文本比内容、库只看存在、目录看 mtime。
    """
    snap: dict[str, tuple] = {}
    for p in _REAL_TEXT_FILES:
        try:
            snap[str(p)] = ("text", _text_digest(p))
        except OSError:
            pass
    for p in _REAL_DB_FILES:
        snap[str(p)] = ("db", p.is_file())
    for root in _REAL_TEXT_TREES:
        if not root.is_dir():
            continue
        for p in root.rglob("*.md"):
            try:
                st = p.stat()
                # 项目文件量大（实测 ~290 个）：存 mtime+size，不逐次读内容。
                snap[str(p)] = ("stat", st.st_mtime_ns, st.st_size)
            except OSError:
                pass
    for d in _REAL_DIR_MTIME:
        try:
            snap[str(d)] = ("dir", d.stat().st_mtime_ns)
        except OSError:
            pass
    return snap


@pytest.fixture(autouse=True)
def _real_data_guard():
    """任何用例把文件写进真实 ~/.deskmate / ~/.config/coworker / 仓库 output/ 即失败。

    ⚠ 桌面端常驻时（它会持续写 USER.md / 数据库），可能出现「不是测试写的也红」；
    此时可临时 ``OW_TEST_REALDATA_GUARD=0`` 关闭本用例跑了的变化比对（会失去这层
    保护，不建议长期关）。
    """
    if os.environ.get("OW_TEST_REALDATA_GUARD", "1") == "0":
        yield
        return
    before = _snapshot_real_data()
    yield
    after = _snapshot_real_data()
    changed = sorted(
        {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    )
    if changed:
        preview = "\n".join(f"  - {p}" for p in changed[:20])
        raise AssertionError(
            "测试改动了真实用户目录（应全部指向 tmp）。\n"
            f"共 {len(changed)} 个文件：\n{preview}\n"
            "排查方向：数据目录 env（DESKMATE_DIR / DESKMATE_MEMORY_DIR / "
            "COWORKER_STATE_DIR）是否在 import 期就被绑定；或后台线程跑过了用例边界。"
        )


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
    # 同步 WebClient（SlackAdapter）同样读代理环境；强制回环直连。
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    try:
        yield fake
    finally:
        await fake.stop()
