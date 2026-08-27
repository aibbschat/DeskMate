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

import logging
from typing import Optional

from coworker.teams.model import Actor, Role

logger = logging.getLogger(__name__)

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
from .final_review import build_final_review_proposal, apply_final_review


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
        self.last_docx: dict[str, str] = {}  # item_kind -> 上次定稿 docx 路径（供状态查询）
        self.novel_ref: str = ""            # 原著路径（txt/docx），前端创建时填写
        self.novel_text: str = ""           # 读出的原著正文窗口（改编参考），空=仅凭概念创作

    # -- 建团 --------------------------------------------------------------
    def _staff(self):
        return build_team_workers(lambda pid: f"{self.space}:{pid}")

    def _read_novel(self, ref: str, max_chars: int = 6000) -> str:
        """读原著路径（txt/docx）作为改编参考，返回前 max_chars 字正文窗口。

        复用 deskmate.perception.file_reader.read_file_safe（安全只读、支持 docx）。
        剥离其「未读完」警告头只留正文；坏路径/不支持类型返回空串（不污染 prompt）。
        """
        if not ref or not ref.strip():
            return ""
        try:
            from deskmate.perception.file_reader import read_file_safe
        except Exception:
            return ""
        raw = read_file_safe(ref.strip(), max_chars=max_chars)
        if not raw or (raw.startswith("（") and "\n" not in raw):
            return ""  # 文件不存在/不支持/读取失败等错误提示，不当原著用
        marker = "—— 以下为本次读到的片段 ——\n"
        if marker in raw:
            body = raw.split(marker, 1)[1]
            note = (
                f"（原著《{ref.strip().split('/')[-1]}》节选前 {max_chars} 字作为改编参考，"
                f"完整稿见 {ref.strip()}）\n\n"
            )
            return note + body
        return raw

    def start_screenplay_project(self, concept: str, *, novel_ref: str = "") -> dict:
        self.concept = concept
        self.novel_ref = novel_ref
        self.novel_text = self._read_novel(novel_ref) if novel_ref else ""
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

    def start_outline_project(self, concept: str, *, novel_ref: str = "") -> dict:
        self.concept = concept
        self.novel_ref = novel_ref
        self.novel_text = self._read_novel(novel_ref) if novel_ref else ""
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

    # -- 定稿导出（docx 落地，容错） -------------------------------------
    def _export_docx(self, item_kind: str, text: str) -> None:
        """定稿文本导出 .docx（失败仅记日志，不阻断状态机）。

        用本地 officecli 把「剧名标题 + 正文」展开为规范 Word 文档；
        officecli 缺失时降级写 .md。详见 deskmate/creation/docx_export.py。
        """
        try:
            from .docx_export import export_creation_docx
        except Exception as exc:  # noqa: BLE001
            logger.warning("docx 模块加载失败（跳过导出）: %s", exc)
            return
        title = (self.concept or "未命名剧本")[:20]
        md = f"# {title}\n\n{text}"
        try:
            res = export_creation_docx(item_kind, title, md)
        except Exception as exc:  # noqa: BLE001
            logger.warning("docx 导出异常（不影响定稿）: %s", exc)
            return
        if res.get("ok"):
            label = "剧本" if item_kind == "screenplay" else "大纲"
            suffix = "" if not res.get("degraded") else "（md 降级）"
            self.last_docx[item_kind] = res.get("path") or ""
            logger.info("%s定稿已导出%s: %s", label, suffix, res.get("path"))
        else:
            logger.warning("定稿 docx 导出失败（不影响定稿）: %s", res.get("error"))

    # -- 大纲路线 ----------------------------------------------------------
    def draft_outline(self, outline_item_id: Optional[int] = None) -> str:
        outline_item_id = outline_item_id or self.ids["outline_item"]
        self.store.assign(self.space, self.lead, outline_item_id, assignee=OUTLINE_WORKER)
        w = Actor(OUTLINE_WORKER, Role.WORKER)
        if self.store.get_item(self.space, outline_item_id)["state"] == "open":
            self.guard.transition(self.space, w, outline_item_id, to="in_progress", comment="开始撰写大纲")
        text = self.executor.draft_outline(self.concept, novel_text=self.novel_text)
        self._drafts[outline_item_id] = text
        self.guard.transition(self.space, w, outline_item_id, to="review", comment="大纲已成，请审")
        return text

    def review_outline(self, outline_item_id: Optional[int] = None, review_item_id: Optional[int] = None) -> ReviewResult:
        outline_item_id = outline_item_id or self.ids["outline_item"]
        review_item_id = review_item_id or self.ids["outline_review_item"]
        self.store.assign(self.space, self.lead, review_item_id, assignee=REVIEW_WORKER)
        draft = self._drafts.get(outline_item_id, "")
        result = self.executor.review(draft, criteria=self._criteria(review_item_id), novel_text=self.novel_text)
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
            self._export_docx("outline", self._drafts.get(outline_item_id, ""))
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
        text = self.executor.draft_screenplay(outline_text, novel_text=self.novel_text)
        self._drafts[screen_item_id] = text
        self.guard.transition(self.space, w, screen_item_id, to="review", comment="剧本已成，请审")
        return text

    def review_screenplay(self, screen_item_id: Optional[int] = None, review_item_id: Optional[int] = None) -> ReviewResult:
        screen_item_id = screen_item_id or self.ids["screen_item"]
        review_item_id = review_item_id or self.ids["screen_review_item"]
        self.store.assign(self.space, self.lead, review_item_id, assignee=REVIEW_WORKER)
        draft = self._drafts.get(screen_item_id, "")
        result = self.executor.review(draft, criteria=self._criteria(review_item_id), novel_text=self.novel_text)
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
            self._export_docx("screenplay", self._drafts.get(screen_item_id, ""))
            return "approved"
        # 打回：审核项保持 review（留待复审），产出项 review→in_progress 打回改。
        self.guard.transition(self.space, self.lead, screen_item_id, to="in_progress",
                              comment=f"打回修改：{result.opinion}")
        return "rejected"

    # -- 终审桥接（线路 B → 线路 A）----
    def propose_final_review(self, item_kind: str, *, genre_hint: Optional[str] = None) -> "FinalReviewProposal":
        """主编在 review 阶段生成「终审提案」（纯数据，不拉圆桌，待用户知情批准）。

        返回对象含四要素：送审片段 / 推荐专家 / 验收标准 / 前序意见——前端须完整呈现
        给用户看清后才能批准（设计铁律，见 docs §5.2）。
        """
        return build_final_review_proposal(self, item_kind, genre_hint=genre_hint)

    def finalize_final_review(self, proposal: "FinalReviewProposal", synthesis: str, verdict: bool) -> str:
        """用户知情批准后，把圆桌终审结论回灌创作项（approve=done / 打回=review→in_progress）。"""
        return apply_final_review(self, proposal, synthesis, verdict)
