"""创作流水线串行门禁（PipelineGuard）。

上游 teams 的 ``link(parent)`` 只记录依赖、不强制顺序。本模块在 item 进入
``in_progress`` 前检查其依赖（父项）是否 ``done``；未完成则拒绝，保证「大纲定稿
才开写剧本」。

薄封装 ``team_store``，不修改上游干净代码（``coworker/teams/store.py`` 保持零改动）。
"""
from __future__ import annotations


class PipelineGuardError(Exception):
    """门禁拦截：依赖未完成，item 不能进入 in_progress。"""


class PipelineGuard:
    def __init__(self, store) -> None:
        self.store = store

    # -- 依赖解析 ----------------------------------------------------------
    def _parent_ids(self, space: str, item_id: int) -> list[int]:
        """依赖仅来自 ``link(kind=parent)`` 边——即「本项依赖某父项定稿」。

        ``_links_of`` 对「本项为子」的 parent 边返回 ``{"kind": "parent", "item": <父id>}``
        （对「本项为父」的同一边返回反向 ``{"kind": "child", ...}``）；故只取 kind=="parent"
        的 link 的 item 作依赖。

        注意：``create_item(parent=...)`` 的 parent 字段是「针对/归属」语义（如审核项
        挂到产出项），**不是**门禁依赖，此处不读取，避免把「大纲审核项针对大纲」误判成
        「审核项依赖大纲 done」而在审核阶段误拦。
        """
        item = self.store.get_item(space, item_id)
        parents: list[int] = []
        for link in item.get("links", []) or []:
            if link.get("kind") == "parent":
                it = link.get("item")
                if it is not None:
                    parents.append(int(it))
        return parents

    # -- 检查 --------------------------------------------------------------
    def check(self, space: str, item_id: int, to: str) -> None:
        """进入 in_progress 前的依赖检查；其余状态（open/review/done…）不拦。"""
        if to != "in_progress":
            return
        for parent_id in self._parent_ids(space, item_id):
            parent = self.store.get_item(space, parent_id)
            if parent.get("state") != "done":
                raise PipelineGuardError(
                    f"项 #{item_id} 依赖父项 #{parent_id}，但父项尚未定稿"
                    f"（当前状态：{parent.get('state')}）。请先完成父项。"
                )

    # -- 带门禁的 transition 透传 ----------------------------------------
    def transition(self, space, actor, item_id, to, *, comment="", refs=None, taint=False):
        """包装上游 store.transition：进入 in_progress 前先过门禁，其余直接透传。

        review/done 的 actor 权限由上游 store 照常校验（worker 不能 done/打回）。
        """
        self.check(space, item_id, to)
        return self.store.transition(
            space, actor, item_id, to, comment=comment, refs=refs, taint=taint
        )
