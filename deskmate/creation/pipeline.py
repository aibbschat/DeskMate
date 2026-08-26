"""创作流水线编排（线路 B · 原生 teams 用法）。

剧本改编：用户丢原创小说/概念 → 主编(lead)组队派活 → 大纲专家写大纲 →
审核专家审 → 主编拍板（通过定稿 / 打回改）→ 大纲定稿后剧本项解锁 →
编剧写剧本 → 审核 → 主编拍板。

关键权限（与上游 authority 一致，修正文档 §4.2 旧描述）：
- worker（大纲专家/编剧/审核专家）只把 assigned item 移到 {in_progress, review}，
  即「写/审」并 transition(review) 交结论；
- 主编(lead) 读结论后拍板 transition(done) 验收，或 transition(review→in_progress)
  打回（comment 转给产出处）；worker 无权 done/打回。

串行门禁（PipelineGuard）：剧本项进入 in_progress 前检查其 link(parent) 依赖的
大纲项是否 done；未定稿则拒绝——「大纲定稿才开写剧本」。

真实稿件文本：阶段 1 存于内存草稿表 self._drafts（下游接文件 IO / ChatStore）。
"""
from __future__ import annotations

from typing import Optional

from coworker.teams.model import Actor, Role

from .guard import PipelineGuard, PipelineGuardError
from .personas_loader import (
    LEAD_ID,
    OUTLINE_WORKER,
    SCREENPLAY_WORKER,
    REVIEW_WORKER,
    build_team_workers,
)
from ..teams_client import TeamsClient
from .executor import CreativeExecutor, StubExecutor, ReviewResult


class CreativePipeline:
    def __init__(
        self,
        space: str,
        *,
        executor: Optional[CreativeExecutor] = None,
        base_dir: Optional[str] = None,
        lead_session: Optional[str] = None,
    ) -> None:
        self.space = space
        self.client = TeamsClient(base_dir=base_dir)
        self.store = self.client.team_store
        self.reg = self.client.teams
        self.guard = PipelineGuard(self.store)
        self.lead = Actor(LEAD_ID, Role.LEAD)
        self.executor = executor or StubExecutor()
        self.lead_session = lead_session or f"{space}:lead"
        self.concept = ""
        self.ids: dict[str, int] = {}
        self._team = None
        self._drafts: dict[int, str] = {}

    # -- 建团 --------------------------------------------------------------
    def _staff(self):
        return build_team_workers(lambda pid: f"{self.space}:{pid}")

    def start_screenplay_project(self, concept: str, *, novel_ref: str = "") -> dict:
        self.concept = concept
        self._team = self.reg.create(
            space=self.space,
            lead_session=self.lead_session,
            lead_actor=LEAD_ID,
            workers=self._staff(),
        )
        outline = self.store.create_item(
            self.space, self.lead, title="大纲v1",
            criteria="结构完整、契合原著精神、无逻辑硬伤", description=concept,
        )
        outline_review = self.store.create_item(
            self.space, self.lead, title="大纲审核",
            criteria="史实/逻辑/市场/契合原著精神",
            description=f"针对大纲v1 #{outline['id']}",
        )
        screen = self.store.create_item(
            self.space, self.lead, title="全剧本",
            criteria="可拍性、台词节奏、史实",
        )
        screen_review = self.store.create_item(
            self.space, self.lead, title="剧本审核",
            criteria="台词/节奏/可拍性/史实",
            description=f"针对全剧本 #{screen['id']}",
        )
        # 串行依赖（门禁据此拦截）：全剧本 依赖 大纲v1 定稿。
        # 用 link(kind=parent, src=screen, dst=outline)，区别于 review 项的「针对」parent 字段。
        self.store.link(self.space, self.lead, src=screen["id"], kind="parent", dst=outline["id"])
        self.ids = {
            "outline_item": outline["id"],
            "outline_review_item": outline_review["id"],
            "screen_item": screen["id"],
            "screen_review_item": screen_review["id"],
        }
        return self.ids

    def start_outline_project(self, concept: str) -> dict:
        self.concept = concept
        self._team = self.reg.create(
            space=self.space,
            lead_session=self.lead_session,
            lead_actor=LEAD_ID,
            workers=self._staff(),
        )
        outline = self.store.create_item(
            self.space, self.lead, title="大纲v1",
            criteria="结构完整、契合原著精神、无逻辑硬伤", description=concept,
        )
        outline_review = self.store.create_item(
            self.space, self.lead, title="大纲审核",
            criteria="史实/逻辑/市场/契合原著精神",
            description=f"针对大纲v1 #{outline['id']}",
        )
        self.ids = {
            "outline_item": outline["id"],
            "outline_review_item": outline_review["id"],
        }
        return self.ids

    # -- 辅助 --------------------------------------------------------------
    def _criteria(self, item_id: int) -> str:
        return self.store.get_item(self.space, item_id).get("criteria", "")

    def _outline_text(self) -> str:
        return self._drafts.get(self.ids.get("outline_item", -1), "")

    def _last_review_result(self, review_item_id: int) -> ReviewResult:
        item = self.store.get_item(self.space, review_item_id)
        for c in reversed(item.get("comments", []) or []):
            body = c.get("body") or c.get("comment") or ""
            if body:
                approved = "打回" not in body and "不合格" not in body
                return ReviewResult(approved=approved, opinion=body)
        return ReviewResult(approved=True, opinion="（无审核意见，默认通过）")

    # -- 大纲路线 ----------------------------------------------------------
    def draft_outline(self, outline_item_id: Optional[int] = None) -> str:
        outline_item_id = outline_item_id or self.ids["outline_item"]
        self.store.assign(self.space, self.lead, outline_item_id, assignee=OUTLINE_WORKER)
        w = Actor(OUTLINE_WORKER, Role.WORKER)
        if self.store.get_item(self.space, outline_item_id)["state"] == "open":
            self.guard.transition(self.space, w, outline_item_id, to="in_progress", comment="开始撰写大纲")
        text = self.executor.draft_outline(self.concept)
        self._drafts[outline_item_id] = text
        self.guard.transition(self.space, w, outline_item_id, to="review", comment="大纲已成，请审")
        return text

    def review_outline(self, outline_item_id: Optional[int] = None, review_item_id: Optional[int] = None) -> ReviewResult:
        outline_item_id = outline_item_id or self.ids["outline_item"]
        review_item_id = review_item_id or self.ids["outline_review_item"]
        self.store.assign(self.space, self.lead, review_item_id, assignee=REVIEW_WORKER)
        draft = self._drafts.get(outline_item_id, "")
        result = self.executor.review(draft, criteria=self._criteria(review_item_id))
        w = Actor(REVIEW_WORKER, Role.WORKER)
        self.guard.transition(self.space, w, review_item_id, to="in_progress", comment="开始审核大纲")
        self.guard.transition(self.space, w, review_item_id, to="review", comment=result.opinion)
        return result

    def lead_verdict_outline(self, outline_item_id: Optional[int] = None,
                             review_item_id: Optional[int] = None,
                             result: Optional[ReviewResult] = None) -> str:
        outline_item_id = outline_item_id or self.ids["outline_item"]
        review_item_id = review_item_id or self.ids["outline_review_item"]
        if result is None:
            result = self._last_review_result(review_item_id)
        if result.approved:
            self.guard.transition(self.space, self.lead, review_item_id, to="done", comment="验收通过")
            self.guard.transition(self.space, self.lead, outline_item_id, to="done", comment="大纲定稿")
            return "approved"
        # 打回：审核项保持 review（留待复审），产出项 review→in_progress 打回改。
        # 主编以 lead 身份打回（worker 无权做）。
        self.guard.transition(self.space, self.lead, outline_item_id, to="in_progress",
                              comment=f"打回修改：{result.opinion}")
        return "rejected"

    # -- 剧本路线 ----------------------------------------------------------
    def draft_screenplay(self, screen_item_id: Optional[int] = None) -> str:
        screen_item_id = screen_item_id or self.ids["screen_item"]
        self.store.assign(self.space, self.lead, screen_item_id, assignee=SCREENPLAY_WORKER)
        w = Actor(SCREENPLAY_WORKER, Role.WORKER)
        if self.store.get_item(self.space, screen_item_id)["state"] == "open":
            # 门禁点：剧本 in_progress 前检查大纲是否 done；未定稿则抛 PipelineGuardError
            self.guard.transition(self.space, w, screen_item_id, to="in_progress", comment="开始撰写剧本")
        outline_text = self._outline_text()
        text = self.executor.draft_screenplay(outline_text)
        self._drafts[screen_item_id] = text
        self.guard.transition(self.space, w, screen_item_id, to="review", comment="剧本已成，请审")
        return text

    def review_screenplay(self, screen_item_id: Optional[int] = None, review_item_id: Optional[int] = None) -> ReviewResult:
        screen_item_id = screen_item_id or self.ids["screen_item"]
        review_item_id = review_item_id or self.ids["screen_review_item"]
        self.store.assign(self.space, self.lead, review_item_id, assignee=REVIEW_WORKER)
        draft = self._drafts.get(screen_item_id, "")
        result = self.executor.review(draft, criteria=self._criteria(review_item_id))
        w = Actor(REVIEW_WORKER, Role.WORKER)
        self.guard.transition(self.space, w, review_item_id, to="in_progress", comment="开始审核剧本")
        self.guard.transition(self.space, w, review_item_id, to="review", comment=result.opinion)
        return result

    def lead_verdict_screenplay(self, screen_item_id: Optional[int] = None,
                                review_item_id: Optional[int] = None,
                                result: Optional[ReviewResult] = None) -> str:
        screen_item_id = screen_item_id or self.ids["screen_item"]
        review_item_id = review_item_id or self.ids["screen_review_item"]
        if result is None:
            result = self._last_review_result(review_item_id)
        if result.approved:
            self.guard.transition(self.space, self.lead, review_item_id, to="done", comment="验收通过")
            self.guard.transition(self.space, self.lead, screen_item_id, to="done", comment="剧本定稿")
            return "approved"
        # 打回：审核项保持 review（留待复审），产出项 review→in_progress 打回改。
        self.guard.transition(self.space, self.lead, screen_item_id, to="in_progress",
                              comment=f"打回修改：{result.opinion}")
        return "rejected"
