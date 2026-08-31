"""圆桌 × 原生 teams 适配层（线路 A · loose coupling）。

设计铁律（见 docs/teams-integration-design.md §3）：
- 保留 coordinator 的「到达序并发 + 收窄答辩 + 主持纪要」研讨编排，本层只作
  「持久化 + 结构化 + 成本护栏」基座：coordinator 仍进程内 LLM 直调驱动研讨，
  本层在关键点把结果镜像到 teams 的 ChatStore（辩论场）/ TeamStore（board 审稿维度）。
- 7 专家是「逻辑映射」为 team worker（登记花名册，不预生成真实 agent session），
  以避免破坏研讨并发语义且不过重。
- 所有 board 的 done 收口由主持「小桔」(lead) 执行，符合上游 authority。
- 本层不依赖 coordinator 模块（避免重型 import 与循环依赖），发言以
  (speaker, text, kind) 入参，由 coordinator 在事件流里调用。
"""
from __future__ import annotations

import logging
from typing import Optional

from coworker.teams.model import Actor, Role
from coworker.teams.registry import TeamWorker

from deskmate.teams_client import TeamsClient

logger = logging.getLogger(__name__)

# 审稿维度：(标题, 认领专家 id, 该维度「通过」的验收标准)
# 维度与 7 专家映射：historian=史实、lit-editor=文笔、reader=结构、publisher=市场
# （publisher/adaptation/genre 同属市场化视角，市场适配维度由 publisher 主认领）。
REVIEW_DIMENSIONS = [
    ("史实核验", "historian", "无史实硬伤；存疑处标【待确认】"),
    ("文笔修辞", "lit-editor", "语言质感达标、无 AI 腔"),
    ("结构节奏", "reader", "章节钩子 / 信息密度达标"),
    ("市场适配", "publisher", "符合品类读者预期"),
]


class RoundtableTeamsBridge:
    """把一场圆桌研讨镜像进 teams 基座（建团 + board 维度 + 辩论场 + 终验收）。"""

    def __init__(
        self, client: TeamsClient, *, team_id: str, lead_id: str = "小桔",
    ) -> None:
        self.client = client
        self.team_id = team_id
        self.space = team_id
        self.lead_id = lead_id
        self.lead = Actor(lead_id, Role.LEAD)
        self.group_id: Optional[str] = None
        self.topic_item_id: Optional[int] = None
        self.dimension_items: dict[str, int] = {}

    # ---- 建团 + board 结构 ----
    def setup(
        self, worker_ids: list[str], worker_names: Optional[list[str]] = None, *, topic: str,
    ) -> int:
        """建 chat group + 注册团队 + 建 board 议题/维度项；返回议题 item id。

        注意：workers 只登记进 TeamRegistry 花名册，并不预生成真实 agent session
        （预生成需 lead 在对话里调 propose_team 工具，圆桌不走那条路）——研讨仍由
        coordinator 进程内直调主导，故为 loose coupling。
        """
        names = worker_names or worker_ids
        members = [{"name": self.lead_id}] + [{"name": n} for n in names]
        group = self.client.chat_store.create_group(
            name=f"圆桌终审委员会·{self.team_id}", members=members,
        )
        self.group_id = group["group_id"]
        # TeamWorker 字段为 actor/persona/session_id（无 role 字段）。圆桌为 loose
        # coupling，worker 不预生成真实 session，session_id 仅作占位标识。
        workers = [
            TeamWorker(actor=wid, persona=wid, session_id=f"{self.team_id}:{wid}")
            for wid in worker_ids
        ]
        self.client.teams.create(
            space=self.team_id,
            lead_actor=self.lead_id,
            lead_session=f"{self.team_id}:lead",
            workers=workers,
            chat_enabled=True,
            chat_group=self.group_id,
        )
        self.topic_item_id = self.client.team_store.create_item(
            self.space, self.lead, title="圆桌议题", criteria=topic,
        )["id"]
        for title, owner, criteria in REVIEW_DIMENSIONS:
            dim = self.client.team_store.create_item(
                self.space, self.lead, title=title, criteria=criteria,
            )
            # 维度 item 挂到议题 item 之下（link child → parent 议题）
            self.client.team_store.link(
                self.space, self.lead, src=dim["id"], kind="parent", dst=self.topic_item_id,
            )
            self.dimension_items[title] = dim["id"]
        return self.topic_item_id

    # ---- 辩论场（发言镜像）----
    def post_speech(self, speaker: str, text: str, kind: str = "persona") -> None:
        """把一轮发言镜像进 ChatStore 辩论场；@提及自动解析（原生语义）。"""
        if self.group_id is None or not (text or "").strip():
            return
        role = "host" if kind == "host" else "worker"
        try:
            self.client.chat_store.post(
                self.group_id, author=speaker, text=text, author_role=role,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("圆桌发言镜像 ChatStore 失败(跳过): %s", exc)

    # ---- 终验收（lead 收口）----
    def _close_item(self, item_id: int, label: str) -> None:
        """议题 / 维度 item 走完整状态机 open→in_progress→review→done（lead 全权收口）。"""
        for to in ("in_progress", "review", "done"):
            try:
                self.client.team_store.transition(
                    self.space, self.lead, item_id, to=to, comment=f"{label} 收口",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("item %s transition→%s 失败(跳过): %s", item_id, to, exc)

    def finalize(self, synthesis: str) -> None:
        """主持产出《综合研判报告》后，由 lead 把议题 + 各维度 item done，报告入 journal。"""
        if self.topic_item_id is None:
            return
        # 成本护栏（预留）：圆桌为同步主动触发，wake budget 主要防后台失控，这里仅记录。
        self.check_wake_budget(cap=50)
        self._close_item(self.topic_item_id, "圆桌议题")
        for title, item_id in self.dimension_items.items():
            self._close_item(item_id, f"维度「{title}」")
        self._journal(synthesis)

    def _journal(self, synthesis: str) -> None:
        try:
            body = (synthesis or "").strip() or "(无纪要)"
            if len(body) > 3000:  # JournalStore 有 body 长度硬上限，超长截断
                body = body[:3000] + "\n…（纪要较长，已截断）"
            self.client.journal_store.append(
                self.lead, case=self.team_id, body=body, kind="note",
                space=self.space, item=self.topic_item_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("圆桌纪要 journal 落盘失败(跳过): %s", exc)

    # ---- 成本护栏（预留，阶段 4 调优时接）----
    def check_wake_budget(self, cap: int = 20) -> bool:
        """复用上游 TeamRegistry.count_wake 滚动预算；圆桌为同步主动触发，仅作护栏信号。"""
        try:
            ok = self.client.teams.count_wake(self.team_id, cap=cap)
        except Exception as exc:  # noqa: BLE001
            logger.warning("圆桌 wake 预算查询失败(跳过): %s", exc)
            return True
        if not ok:
            logger.info("圆桌 wake 预算达上限(%s)，本轮不自动唤醒", cap)
        return ok
