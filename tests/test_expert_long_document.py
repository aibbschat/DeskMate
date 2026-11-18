"""1v1 专家「长文档工位」行为级契约（2026-11-11）。

萧哥提出：大纲 / 脑洞都可能是长文档，专家现在只能「分段、分文档」写，
产物被切成多个文件，影响成品。要求补长文档能力，且不打断正在跑的那一轮。

只读核查的根因（不是字数闸、不是模型上限、不是人设短答纪律）：
- `write_file` 无字数上限，但 `overwrite=True`，**没有 append**；
- 模型为了保住前一段只能另起文件名 ⇒ 一份大纲散成 N 个文件；
- 单轮塞不进整篇（tool-call 参数流式截断有 `_raw` 兜底路径）⇒ 必须允许分段生成。

本组锁的契约：**分段可以，产物必须是一份完整文档**。
1. 每段存的是**原文**，不摘要、不截断、不改写；
2. 成稿 = 各段原文按序拼接成**一个文件**，工具不注入段号 / 分隔线 / 衔接语；
3. 草稿用 `.part`（不进 Artifacts 白名单），成稿路径仍走既有 root + guarded_write 门禁；
4. 能力声明与实现对齐（11 份专家 manifest ↔ catalog capability ↔ 部署清单）。

先红后绿：本文件在改生产代码之前应全部失败。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# catalog 与 coworker.agents 有已知循环：先让 agents 包完整初始化，
# 顺序照抄 tests/test_catalog.py:12-15 / test_expert_1v1_manuscript_coverage.py:29-34。
from coworker.agents.base import AgentContext  # noqa: F401
from coworker.agents.code import CODE_CAPABILITIES  # noqa: F401
from coworker.catalog import CATALOG, expand
from coworker.roots import RootDir

# 12 位写作专家（deskmate/bots/author.py::EXPERT_PERSONA_IDS 同源，2026-11-11 转 bundle）。
EXPERT_IDS = (
    "brainstormer", "lit-editor", "adaptation", "historian",
    "proofreader", "reader", "publisher", "genre", "editor",
    "outline", "plot", "character",
)
_ROOT = Path(__file__).resolve().parents[1]
_PERSONA_DIR = _ROOT / "coworker" / "personas" / "builtin"

# Artifacts 面板白名单（manager.py:3887-3903），草稿后缀绝不能落在里面。
_ARTIFACT_SUFFIXES = {
    ".md", ".markdown", ".html", ".htm", ".txt", ".json", ".csv",
    ".tsv", ".py", ".js", ".ts", ".tsx", ".css", ".png", ".jpg",
}


def _ctx(tmp_path: Path) -> tuple[AgentContext, Path, Path]:
    ws = tmp_path / "ws"
    scratch = tmp_path / "scratch"
    ws.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    ctx = AgentContext(
        workspace=ws,
        persona_id="outline",
        roots=[
            RootDir(ws, writable=True, label="workspace"),
            RootDir(scratch, writable=True, label="scratch"),
        ],
    )
    return ctx, ws, scratch


def _tools(ctx: AgentContext) -> dict:
    return {getattr(t, "__name__", ""): t for t in expand(["long_document"], ctx)}


def _mod():
    try:
        from coworker.tools import document
    except ImportError as exc:  # 产品红，不是收集错误
        pytest.fail(f"缺少长文档工位模块 coworker/tools/document.py: {exc}")
    return document


# ------------------------------------------------------------------ 行为：原文不失真

def test_append_keeps_every_segment_verbatim(tmp_path: Path) -> None:
    ctx, _, scratch = _ctx(tmp_path)
    tools = _tools(ctx)
    segs = [
        "卷一 · 起势\n第一章 萤窗夜话，萧盛把灯芯挑亮了三分。",
        "卷二 · 转\n康得宝的慢性中毒从第五章开始埋，不在第二章抖出来。",
        "第六章 空白不是空白——上一轮我偷懒没读，这次逐章读完再判。",
    ]
    for i, s in enumerate(segs, 1):
        tools["document_append"](doc_id="yingchuang-gang", section=f"第{i}段", content=s)
    parts = sorted((scratch / "docbuild" / "yingchuang-gang").glob("*.part"))
    assert len(parts) == 3
    for p, s in zip(parts, segs):
        assert p.read_text(encoding="utf-8") == s, "分段原文被改写/摘要/截断了"


def test_long_segment_is_not_truncated(tmp_path: Path) -> None:
    ctx, _, scratch = _ctx(tmp_path)
    tools = _tools(ctx)
    body = "长句" * 25_000  # 5 万字，远超任何单轮舒适区
    tools["document_append"](doc_id="long", section="全卷", content=body)
    p = next((scratch / "docbuild" / "long").glob("*.part"))
    assert len(p.read_text(encoding="utf-8")) == len(body)


def test_finish_produces_exactly_one_deliverable(tmp_path: Path) -> None:
    ctx, ws, _ = _ctx(tmp_path)
    tools = _tools(ctx)
    for i in range(4):
        tools["document_append"](doc_id="v1", section=f"卷{i}", content=f"第{i}卷原文。")
    tools["document_finish"](doc_id="v1", path="萤窗案-卷纲.md")
    produced = [p for p in ws.rglob("*") if p.suffix.lower() in _ARTIFACT_SUFFIXES]
    assert len(produced) == 1, f"产物被切成了多个文件: {[p.name for p in produced]}"


def test_finish_is_verbatim_ordered_join(tmp_path: Path) -> None:
    ctx, ws, _ = _ctx(tmp_path)
    tools = _tools(ctx)
    segs = ["第一段原文，含，标点。", "第二段原文\n带换行", "第三段原文"]
    for i, s in enumerate(segs, 1):
        tools["document_append"](doc_id="join", section=f"{i}", content=s)
    result = tools["document_finish"](doc_id="join", path="成稿.md")
    text = (ws / "成稿.md").read_text(encoding="utf-8")
    assert text == "\n\n".join(segs), "成稿不是各段原文按序拼接（被摘要/改写/插入了内容）"
    assert "3 段" in result or "3段" in result


def test_finish_injects_no_bridge_markers(tmp_path: Path) -> None:
    ctx, ws, _ = _ctx(tmp_path)
    tools = _tools(ctx)
    for i in range(3):
        tools["document_append"](doc_id="clean", section=f"卷{i}", content=f"第{i}卷成品文字。")
    tools["document_finish"](doc_id="clean", path="成稿.md")
    text = (ws / "成稿.md").read_text(encoding="utf-8")
    for marker in ("未完待续", "（上接）", "以下省略", "---", "第 1 段", "## 分段"):
        assert marker not in text, f"工具往产物里注入了工程标记: {marker}"


def test_draft_files_are_invisible_to_artifacts_panel(tmp_path: Path) -> None:
    ctx, _, scratch = _ctx(tmp_path)
    tools = _tools(ctx)
    tools["document_append"](doc_id="draft", section="一", content="原文")
    drafts = list((scratch / "docbuild" / "draft").iterdir())
    assert drafts, "草稿目录没建起来"
    assert all(p.suffix.lower() not in _ARTIFACT_SUFFIXES for p in drafts), (
        f"草稿会被 Artifacts 面板当成产物列出来: {[p.name for p in drafts]}"
    )


def test_status_reports_order_and_total(tmp_path: Path) -> None:
    ctx, _, _ = _ctx(tmp_path)
    tools = _tools(ctx)
    tools["document_append"](doc_id="s", section="卷一", content="甲")
    tools["document_append"](doc_id="s", section="卷二", content="乙乙")
    out = tools["document_status"](doc_id="s")
    assert "卷一" in out and "卷二" in out
    assert "3" in out  # 累计字数 1 + 2
    assert ("2 段" in out) or ("2段" in out)


def test_append_after_finish_allows_revision(tmp_path: Path) -> None:
    ctx, ws, _ = _ctx(tmp_path)
    tools = _tools(ctx)
    tools["document_append"](doc_id="rev", section="卷一", content="甲")
    tools["document_finish"](doc_id="rev", path="成稿.md")
    tools["document_append"](doc_id="rev", section="卷二", content="乙")
    tools["document_finish"](doc_id="rev", path="成稿.md")
    assert (ws / "成稿.md").read_text(encoding="utf-8") == "甲\n\n乙"


def test_finish_without_segments_is_refused(tmp_path: Path) -> None:
    ctx, ws, _ = _ctx(tmp_path)
    tools = _tools(ctx)
    out = tools["document_finish"](doc_id="empty", path="成稿.md")
    assert "没有" in out or "空" in out or "先" in out
    assert not (ws / "成稿.md").exists()


# ------------------------------------------------------------------ 安全：路径与红线

def test_doc_id_rejects_path_traversal(tmp_path: Path) -> None:
    ctx, _, scratch = _ctx(tmp_path)
    tools = _tools(ctx)
    out = tools["document_append"](doc_id="../escape", section="x", content="原文")
    assert "不合法" in out or "非法" in out or "不能" in out
    assert not (tmp_path / "escape").exists()


def test_finish_rejects_path_outside_writable_roots(tmp_path: Path) -> None:
    ctx, ws, _ = _ctx(tmp_path)
    tools = _tools(ctx)
    tools["document_append"](doc_id="safe", section="一", content="原文")
    outside = tmp_path / "outside.md"
    out = tools["document_finish"](doc_id="safe", path=str(outside))
    assert "不允许" in out or "越界" in out or "root" in out.lower() or "目录" in out
    assert not outside.exists()


def test_finish_refuses_author_manuscript_directory(tmp_path: Path) -> None:
    """guarded_write 红线：含 .deskmate 圣经的目录＝原稿正文，绝不直改写。"""
    ctx, _, _ = _ctx(tmp_path)
    book = tmp_path / "我的书"
    (book / ".deskmate").mkdir(parents=True)
    ctx.roots.append(RootDir(book, writable=True, label="book"))
    tools = _tools(ctx)
    tools["document_append"](doc_id="guard", section="一", content="原文")
    target = book / "正文.md"
    out = tools["document_finish"](doc_id="guard", path=str(target))
    assert "原稿" in out or "不允许" in out or "拒绝" in out
    assert not target.exists()


def test_document_finish_is_classified_as_write() -> None:
    """成稿写用户可见文件，必须落在 WRITE_LOCAL 地板上（path scoping + 只读模式门禁）。"""
    from coworker.risk import RiskClass, classify

    assert classify("document_finish") is RiskClass.WRITE_LOCAL


# ------------------------------------------------------------------ 链路：声明与实现对齐

def test_catalog_has_long_document_capability() -> None:
    assert "long_document" in CATALOG
    cap = CATALOG["long_document"]
    assert any(getattr(r, "value", "") == "write_local" for r in cap.risk)


def test_expand_returns_three_named_tools(tmp_path: Path) -> None:
    ctx, _, _ = _ctx(tmp_path)
    names = set(_tools(ctx))
    assert {"document_append", "document_status", "document_finish"} <= names


def test_files_capability_still_expands_write_file(tmp_path: Path) -> None:
    """回归：挂新能力不能动既有 files 工具面。"""
    ctx, _, _ = _ctx(tmp_path)
    names = {getattr(t, "__name__", "") for t in expand(["files"], ctx)}
    assert "write_file" in names
    assert "document_append" not in names, "长文档工具不该混进通用 files 能力"


def test_tool_descriptions_carry_long_document_discipline(tmp_path: Path) -> None:
    ctx, _, _ = _ctx(tmp_path)
    tools = _tools(ctx)
    append_doc = tools["document_append"].__doc__ or ""
    finish_doc = tools["document_finish"].__doc__ or ""
    assert "原文" in append_doc and "摘要" in append_doc
    assert "一个文件" in finish_doc or "一份" in finish_doc
    assert "write_file" in finish_doc, "必须明说别再用 write_file 分文档写"


@pytest.mark.parametrize("pid", EXPERT_IDS)
def test_every_expert_declares_long_document(pid: str) -> None:
    """人设声明与注册表对齐——上一轮刚修过 read_book 错配，不许重犯。"""
    text = (_PERSONA_DIR / pid / "manifest.md").read_text(encoding="utf-8")
    tools_line = next((l for l in text.splitlines() if l.startswith("tools:")), "")
    assert "long_document" in tools_line, f"{pid} 没声明长文档能力"


def test_nonexpert_persona_does_not_get_long_document() -> None:
    """只给写作专家，不扩到小桔本体 / 安全类 persona 的工具面。"""
    for rel in ("deskmate.md", "security/manifest.md"):
        p = _PERSONA_DIR / rel
        if p.is_file():
            assert "long_document" not in p.read_text(encoding="utf-8")


def test_registry_end_to_end_mounts_document_tools(tmp_path: Path, monkeypatch) -> None:
    """端到端：registry 读 manifest → agent.build_tools → 工具面里真有长文档工位。
    同时确认小桔本体没被扩到工具面（附体专家与小桔方向分离）。"""
    from coworker.personas.registry import PersonaRegistry

    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path / "deskmate"))
    ctx, _, _ = _ctx(tmp_path)
    reg = PersonaRegistry(state_path=tmp_path / "personas.json")
    for pid in ("outline", "brainstormer", "character"):
        entry = reg.get(pid)
        assert entry is not None, f"{pid} 没注册上"
        names = {getattr(t, "__name__", "") for t in entry.agent().build_tools(ctx)}
        assert {"document_append", "document_status", "document_finish"} <= names, (
            f"{pid} 的运行时工具面缺长文档工位"
        )
    xj = reg.get("deskmate")
    if xj is not None:
        xj_names = {getattr(t, "__name__", "") for t in xj.agent().build_tools(ctx)}
        assert not any(n.startswith("document_") for n in xj_names)


def test_new_python_module_is_in_deploy_manifest() -> None:
    """红线：新增 .py 必须进 deploy_cloud_sync.sh 清单，漏传＝云端静默失效。"""
    script = (_ROOT / "scripts" / "deploy_cloud_sync.sh").read_text(encoding="utf-8")
    assert "coworker/tools/document.py" in script


def test_deploy_manifest_lists_all_expert_bundle_files() -> None:
    script = (_ROOT / "scripts" / "deploy_cloud_sync.sh").read_text(encoding="utf-8")
    for pid in EXPERT_IDS:
        for rel in (f"coworker/personas/builtin/{pid}/manifest.md",
                    f"coworker/personas/builtin/{pid}/skills/{pid}-method/SKILL.md"):
            assert rel in script, f"部署清单缺 {rel}"
