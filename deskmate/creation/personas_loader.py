"""创作流水线 persona 加载器。

从官方 PersonaRegistry 取 4 个创作 persona（阶段 0 已新增，带 ``team:`` trait），
封装成 teams 的 ``TeamWorker`` 声明，供 CreativePipeline 建团。

不直接拼 persona id 字符串散落各处——集中在此，且 ``validate_creative_personas``
在 pipeline 启动前自检，缺失/ trait 错误立即报错。
"""
from __future__ import annotations

from coworker.teams.registry import TeamWorker
from coworker.personas.registry import get_registry


LEAD_ID = "editor-in-chief"
OUTLINE_WORKER = "outline-writer"
SCREENPLAY_WORKER = "screenwriter"
REVIEW_WORKER = "script-reviewer"

_WORKER_DESC = {
    OUTLINE_WORKER: "把概念/剧情点子写成结构完整的大纲",
    SCREENPLAY_WORKER: "按大纲撰写剧本（分集/全本）",
    REVIEW_WORKER: "审大纲/剧本，给可执行的修改意见",
}


def build_team_workers(session_for) -> list[TeamWorker]:
    """构建创作团队 worker 声明。

    session_for: ``callable(persona_id) -> session_id``（逻辑占位 id；
    真实接入时可由上游 wake plumbing 解析为持久 agent session）。
    """
    return [
        TeamWorker(
            actor=pid,
            persona=pid,
            session_id=session_for(pid),
            reason=desc,
        )
        for pid, desc in _WORKER_DESC.items()
    ]


def validate_creative_personas() -> dict[str, str]:
    """校验 4 个创作 persona 已加载且 team trait 正确；返回 ``{id: team}``。

    用于 pipeline 启动前的自检，缺失或 trait 错误即抛 ValueError。
    """
    reg = get_registry()
    out: dict[str, str] = {}
    for pid in (LEAD_ID, OUTLINE_WORKER, SCREENPLAY_WORKER, REVIEW_WORKER):
        entry = reg.get(pid)
        if entry is None or entry.manifest is None:
            raise ValueError(f"创作 persona 未加载或缺失 manifest：{pid}")
        out[pid] = entry.manifest.team
    if out[LEAD_ID] != "lead":
        raise ValueError(f"{LEAD_ID} 应为 lead，实际 {out[LEAD_ID]}")
    for w in (OUTLINE_WORKER, SCREENPLAY_WORKER, REVIEW_WORKER):
        if out[w] != "worker":
            raise ValueError(f"{w} 应为 worker，实际 {out[w]}")
    return out
