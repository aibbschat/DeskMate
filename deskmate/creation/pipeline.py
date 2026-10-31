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

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional

from coworker.teams.model import Actor, Role

logger = logging.getLogger(__name__)

from .guard import PipelineGuard, PipelineGuardError
from .novel_index import NovelIndex, NovelIndexError
from .personas_loader import (
    LEAD_ID,
    OUTLINE_WORKER,
    SCREENPLAY_WORKER,
    REVIEW_WORKER,
    build_team_workers,
)
from ..teams_client import TeamsClient
from .executor import CreativeExecutor, StubExecutor, ReviewResult
from .final_review import (
    build_final_review_proposal,
    apply_final_review,
    serialize_pending_final,
    deserialize_pending_final,
)


class CreativePipeline:
    def __init__(
        self,
        space: str,
        *,
        executor: Optional[CreativeExecutor] = None,
        base_dir: Optional[str] = None,
        lead_session: Optional[str] = None,
        novel_cache_root: Optional[str] = None,
    ) -> None:
        self.space = space
        self.client = TeamsClient(base_dir=base_dir)
        self._profile_base = base_dir  # 8.5 全局画像注入用（None=默认 ~/.deskmate）
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
        self._user_notes: dict[str, str] = {}  # 用户打回时的修改意见（按 stage 存，重写时注入 prompt）
        self.last_docx: dict[str, str] = {}  # item_kind -> 上次定稿 docx 路径（供状态查询）
        self.novel_ref: str = ""            # 原著路径（txt/docx），前端创建时填写
        # 结构化制作要求（透明化 · 可提要求）：风格/节奏/集数
        self.style: list[str] = []          # 风格标签（如 ["权谋正剧", "悬疑"]）
        self.pacing: str = ""               # 节奏（如 "紧凑快节奏"）
        self.episodes: int = 1              # 剧本总集数（1=全本单文件）
        self.episode_range: list[int] = []  # 实际建项/产出的集号（空=1..episodes）
        self._episode_summaries: dict[int, str] = {}  # 每集前情摘要（跨集续写衔接，随草稿落盘）
        # 前情上下文档位（加集续写衔接质量 vs token 成本）：
        #   summary        = 仅前集摘要（每集约150字）
        #   summary_recent = 摘要 + 紧邻上集定稿全文（默认，续写最平衡）
        self.prior_context: str = "summary_recent"
        self.include_review_notes: bool = True  # 注入前集历史审改意见（审核评语/打回理由）
        self.novel_index: NovelIndex | None = None  # 懒加载：首次取原著上下文时建索引
        self._novel_index_failed: bool = False  # 索引构建失败标记（快照据此返回 failed）
        self._novel_ctx: str = ""           # 改编圣经 + 章节目录（一次性缓存）
        self._novel_related_cache: str = ""  # 圣经 + 相关章节（一次性缓存）
        self._related_override: list[int] = []  # 用户指定创作参考章节（1..n）；空=自动 pick
        self._novel_cache_root = novel_cache_root  # 测试注入用；None=默认 ~/.deskmate/novels
        # 人物小传（防漂移硬锚点，独立持久化文档，类比圣经）：
        # 含主要人物的 目标/动机/弧光/关系/红线，每次写剧本重注入，保证人物不漂移。
        self._char_profiles: str = ""        # 人物小传 markdown（空=尚未生成）
        # 终审结果暂存（B4③）：圆桌 synthesis 按 (item_kind, episode) 分槽，
        # 原先只存内存，重启/被 LRU 淘汰后 final_review_apply 会报「尚未运行终审研讨」。
        # 随草稿落盘，恢复时一并读回。
        self._pending_final: dict = {}
        self._load_drafts()  # 恢复上次的草稿文本（sidecar 重启后 board 状态仍在，草稿不再丢）

    # -- 草稿持久化（P1-4：board 状态在 teams.db，草稿文本也须落盘） ------
    def _drafts_file(self) -> Path:
        """草稿落盘位置：与 board 同数据根（``<base>/creation/<space>.drafts.json``）。"""
        return self.client.base / "creation" / f"{self.space}.drafts.json"

    def _save_drafts(self) -> None:
        """把内存草稿原子写盘（临时文件 + os.replace），供重启恢复。"""
        try:
            path = self._drafts_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            payload = dict(self._drafts)
            payload["_eps"] = self._episode_summaries  # 跨集前情摘要一并落盘
            payload["_cfg"] = {  # 前情上下文档位（加集续写配置）
                "prior_context": self.prior_context,
                "include_review_notes": self.include_review_notes,
            }
            payload["_user_notes"] = dict(self._user_notes)  # 用户打回意见持久化
            payload["_chars"] = self._char_profiles  # 人物小传（防漂移锚点）落盘
            payload["_pf"] = serialize_pending_final(self._pending_final)  # 终审结果暂存（B4③）
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("草稿落盘失败（不影响本次运行）: %s", exc)

    def _load_drafts(self) -> None:
        """启动/恢复时加载既有草稿（文件不存在则空表）。"""
        try:
            path = self._drafts_file()
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                for k, v in (raw or {}).items():
                    if k == "_eps":
                        self._episode_summaries = {int(e): s for e, s in (v or {}).items()}
                    elif k == "_cfg":
                        cfg = v or {}
                        self.prior_context = cfg.get("prior_context") or "summary_recent"
                        self.include_review_notes = bool(cfg.get("include_review_notes", True))
                    elif k == "_user_notes":
                        self._user_notes = dict(v or {})
                    elif k == "_chars":
                        self._char_profiles = v or ""  # 人物小传（防漂移锚点）恢复
                    elif k == "_pf":
                        # 终审结果暂存恢复（B4③）：失败按空表继续，不影响草稿
                        try:
                            self._pending_final = deserialize_pending_final(v)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("终审结果恢复失败（按空表继续）: %s", exc)
                            self._pending_final = {}
                    elif str(k).lstrip("-").isdigit():
                        self._drafts[int(k)] = v
        except Exception as exc:  # noqa: BLE001
            logger.warning("草稿恢复失败（按空草稿继续）: %s", exc)

    # -- 原著上下文（阶段 5：摘要+分章混合，废除 6000 字硬截断） ----------
    @property
    def novel_text(self) -> str:
        """兼容字段：返回改编圣经上下文（全局）。首次访问触发建索引（一次性成本）。"""
        return self._novel_ctx or self._novel_context("bible")

    def _novel_context(self, what: str = "bible") -> str:
        """取原著上下文（懒加载，缓存一次）：
        bible    = 改编圣经 + 章节目录（全局把握，写大纲/审核/终审对照用）
        related  = bible + 按大纲主题挑出的相关章节原文（写剧本用，细节靠分章补）
        """
        if not self.novel_index:
            return ""
        if self._novel_ctx:
            return self._novel_ctx
        idx = self.novel_index
        try:
            # stub 执行器强制降级圣经（零 LLM 不烧 token）；llm 执行器生成精炼版。
            # 缓存为降级且本次 llm 可用时，build 内部会自动升级为精炼版。
            from .executor import StubExecutor

            degrade = isinstance(self.executor, StubExecutor)
            meta = idx.build(degrade=degrade)
        except NovelIndexError as exc:
            logger.warning("原著索引失败（按无原著继续）: %s", exc)
            return ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("原著索引异常（按无原著继续）: %s", exc)
            return ""
        toc = idx.chapter_toc()
        raw_ctx = f"{meta['bible']}\n\n【章节目录】\n{toc}"
        # 8.5 跨项目迁移：自动注入全局创作偏好（换新书也懂你），幂等、fail-open
        try:
            from .global_profile import load_and_inject

            self._novel_ctx = load_and_inject(raw_ctx, base_dir=self._profile_base)
        except Exception:  # noqa: BLE001
            self._novel_ctx = raw_ctx
        return self._novel_ctx

    def _novel_related(self) -> str:
        """bible + 相关章节原文（写剧本时按需喂细节）。

        优先用户指定章节（set_related_chapters 的 _related_override），
        未指定时按大纲主题 bigram 自动挑 3 章。
        """
        if not self.novel_index:
            return ""
        base = self._novel_context("bible")
        if not base:
            return ""
        if self._novel_related_cache:
            return self._novel_related_cache
        idx = self.novel_index
        try:
            if self._related_override:
                chapters = [
                    ch for i in self._related_override if (ch := idx.get_chapter(i)) is not None
                ]
                label = "用户指定的相关章节原文"
            else:
                chapters = idx.pick_chapters(self._outline_text()[:800], k=3)
                label = "与本次改编相关的原著章节原文"
        except Exception:  # noqa: BLE001
            chapters, label = [], ""
        if chapters:
            block = "\n\n".join(
                f"{c['title']}\n{c['body'][:8000]}" for c in chapters
            )
            self._novel_related_cache = base + f"\n\n【{label}】\n{block}"
        else:
            self._novel_related_cache = base
        return self._novel_related_cache

    def set_related_chapters(self, indices: list[int]) -> None:
        """指定后续创作引用的原著章节（1..n）；空列表恢复自动挑选。"""
        n = 0
        if self.novel_index is not None:
            meta = self.novel_index.get_meta()
            n = meta["n"] if meta else 0
        cleaned = [i for i in indices if isinstance(i, int) and 1 <= i <= n]
        self._related_override = sorted(set(cleaned))
        self._novel_related_cache = ""  # 失效缓存，下次 _novel_related 重建
        logger.info("创作参考章节已设定: %s", self._related_override or "（自动匹配）")

    # -- 人物小传（防漂移锚点：独立持久化文档，类比改编圣经） --------------
    def extract_characters(self, *, force: bool = False) -> str:
        """生成/刷新「人物小传」硬锚点（独立 markdown 文档，供每集剧本重注入）。

        抽取源：有原著取改编圣经（最全的人物信息），否则取大纲文本。
        - LLM 可用 → ``_extract_chars_llm`` 产出结构化小传；
        - LLM 不可用 → ``_chars_from_bible_stub`` 从圣经 markdown 抽「人物」段降级。
        幂等：已有小传且不 force 直接返回，不重复烧 token；结果落盘，重启/跨生成复用。
        """
        if self._char_profiles and not force:
            return self._char_profiles
        source = self._novel_context("bible") or self._outline_text()
        if not source.strip():
            self._char_profiles = (
                "# 人物小传（暂无原著/大纲可提取）\n\n"
                "请先提供原著或撰写大纲，再生成人物小传；也可手动在此填写主要人物设定。"
            )
            self._save_drafts()
            return self._char_profiles
        from deskmate.memory.llm import llm_available

        try:
            if llm_available():
                self._char_profiles = self._extract_chars_llm(source)
            else:
                self._char_profiles = self._chars_from_bible_stub(source)
        except Exception as exc:  # noqa: BLE001
            logger.warning("人物小传生成失败（降级从圣经抽取）: %s", exc)
            self._char_profiles = self._chars_from_bible_stub(source)
        self._save_drafts()
        return self._char_profiles

    def _extract_chars_llm(self, source: str) -> str:
        """LLM 路径：把圣经/大纲提炼为结构化人物小传（目标/动机/弧光/关系/红线）。"""
        from deskmate.memory.llm import chat

        src_label = "改编圣经" if self.novel_index else "大纲"
        text = chat(
            system=(
                "你是资深影视改编角色策划。基于给定的{src}提炼主要人物的「人物小传」，"
                "作为后续剧本创作防人物漂移的硬性锚点。每个主要人物给出：\n"
                "- 身份与社会关系\n"
                "- 核心目标（他在这本书里最想要什么）\n"
                "- 内心动机（目标背后的深层驱动，须与{src}一致）\n"
                "- 性格弧光（起点 → 终点，关键转变点）\n"
                "- 关键关系与冲突（与谁对立/同盟，冲突根源）\n"
                "- 人设红线（绝对不能 OOC 的行为/台词）\n"
                "只输出 markdown，每人物一个二级标题（## 姓名），不要复述剧情。"
            ).format(src=src_label),
            user=f"【{src_label}】\n{source}",
            max_tokens=3000,
        )
        return (text or "").strip() or self._chars_from_bible_stub(source)

    def _chars_from_bible_stub(self, source: str) -> str:
        """LLM 不可用时的降级：从圣经/大纲 markdown 抽「人物」相关段作小传底稿。"""
        kept: list[str] = []
        capture = False
        for ln in source.splitlines():
            head = ln.lstrip()
            if re.search(r"人物", ln) and (head.startswith(("#", "1.", "一", "*", "-"))):
                capture = True
            if capture:
                kept.append(ln)
                # 段结束：遇到下一个非人物相关二级/三级标题则停
                if kept and re.match(r"^#{2,3}\s", ln) and "人物" not in ln:
                    break
        if kept:
            return (
                "# 人物小传（降级：从圣经抽取人物段，建议配置 LLM 后点「生成人物小传」刷新）\n\n"
                + "\n".join(kept)
            )
        return (
            "# 人物小传（降级占位）\n\n"
            "未从原著/大纲中识别到结构化人物信息；配置 LLM 后点击「生成人物小传」可自动提炼。"
        )

    # -- 集号辅助（分集模式：ids["screen_items"] = {ep: item_id}） ----------
    def _is_episodic(self) -> bool:
        """分集模式：剧本拆成多集（每集独立 item）。episodes<=1 时走全本单文件。"""
        return bool(self.ids.get("screen_items"))

    def _screen_item_id(self, episode: Optional[int] = None) -> int:
        """取剧本产出项 id：分集模式取指定集（默认下一未完成集），单集取全剧本。"""
        items = self.ids.get("screen_items")
        if items:
            ep = episode or self._next_episode()
            return items[ep]
        return self.ids.get("screen_item", next(iter(self.ids.get("screen_items", {}).values()), -1))

    def _screen_review_item_id(self, episode: Optional[int] = None) -> int:
        """取剧本审核项 id：分集模式取对应集，单集取全剧本审核。"""
        rev = self.ids.get("screen_review_items")
        if rev:
            ep = episode or self._next_episode()
            return rev[ep]
        return self.ids.get("screen_review_item", next(iter(self.ids.get("screen_review_items", {}).values()), -1))

    def _next_episode(self) -> int:
        """分集模式下默认推进的集号：最小的未定稿集。"""
        items = self.ids.get("screen_items", {})
        for ep in sorted(items):
            st = self.store.get_item(self.space, items[ep], actor=self.lead).get("state")
            if st != "done":
                return ep
        return max(items) if items else 1

    def _episode_of(self, item_id: int) -> Optional[int]:
        """item_id -> 集号（分集模式反向查产出项与审核项；单集返回 None）。"""
        for ep, sid in (self.ids.get("screen_items", {}) or {}).items():
            if sid == item_id:
                return ep
        for ep, sid in (self.ids.get("screen_review_items", {}) or {}).items():
            if sid == item_id:
                return ep
        return None

    def _prior_episode_summaries(self, episode: Optional[int]) -> str:
        """当前集之前已定稿/已写集的前情摘要（跨集续写衔接）。"""
        if not episode or episode <= 1:
            return ""
        parts = []
        for ep in sorted(self._episode_summaries):
            if ep < episode:
                parts.append(f"第{ep}集：{self._episode_summaries[ep][:150]}")
        return "\n".join(parts)

    def _prior_review_notes(self, episode: Optional[int]) -> str:
        """前集最后一条审核意见（审改史注入，续写不重蹈覆辙）。"""
        if not episode:
            return ""
        notes = []
        for ep in sorted(self.ids.get("screen_review_items", {}) or {}):
            if ep >= episode:
                break
            rid = self.ids["screen_review_items"][ep]
            try:
                item = self.store.get_item(self.space, rid, actor=self.lead)
            except Exception:  # noqa: BLE001
                continue
            for c in reversed(item.get("comments", []) or []):
                body = (c.get("body") or c.get("comment") or "").strip()
                if body:
                    notes.append(f"第{ep}集：{body[:300]}")
                    break
        return "\n".join(notes)

    def _prior_context_block(self, episode: Optional[int]) -> str:
        """前情上下文（跨集续写衔接）：

        summary        = 全部前集摘要；
        summary_recent = 摘要 + 紧邻上集定稿全文（默认，续写衔接基准）；
        include_review_notes = 附前集历史审改意见。
        """
        if not episode or episode <= 1:
            return ""
        parts = []
        s = self._prior_episode_summaries(episode)
        if s:
            parts.append(f"【前情摘要】\n{s}")
        if self.prior_context == "summary_recent":
            prev_id = (self.ids.get("screen_items", {}) or {}).get(episode - 1)
            prev_text = self._drafts.get(prev_id, "") if prev_id else ""
            if prev_text:
                parts.append(f"【第{episode - 1}集定稿全文（续写衔接基准）】\n{prev_text}")
        if self.include_review_notes:
            notes = self._prior_review_notes(episode)
            if notes:
                parts.append(f"【历史审改意见（续写勿重蹈覆辙）】\n{notes}")
        return "\n\n".join(parts)

    # -- 建团 --------------------------------------------------------------
    def _staff(self):
        return build_team_workers(lambda pid: f"{self.space}:{pid}")

    def _read_novel(self, ref: str, max_chars: int = 6000) -> str:
        """（已退役，保留兼容）读原著前 max_chars 字窗口。

        阶段 5 起由 novel_index（改编圣经 + 按需章节）取代，见 _novel_context。
        """
        if not ref or not ref.strip():
            return ""
        try:
            from deskmate.perception.file_reader import read_file_safe
        except Exception:
            return ""
        raw = read_file_safe(ref.strip(), max_chars=max_chars)
        if not raw or (raw.startswith("（") and "\n" not in raw):
            return ""
        marker = "—— 以下为本次读到的片段 ——\n"
        if marker in raw:
            body = raw.split(marker, 1)[1]
            note = (
                f"（原著《{ref.strip().split('/')[-1]}》节选前 {max_chars} 字作为改编参考，"
                f"完整稿见 {ref.strip()}）\n\n"
            )
            return note + body
        return raw

    def start_screenplay_project(
        self,
        concept: str,
        *,
        novel_ref: str = "",
        episodes: int = 1,
        episode_range: Optional[list[int]] = None,
        style: Optional[list[str]] = None,
        pacing: str = "",
    ) -> dict:
        """启动剧本流水线：大纲 v1 + 审核，剧本按集拆分（episodes>1 时逐集建项）。

        episodes<=1：单「全剧本」+「剧本审核」两个 item（向后兼容既有全本流程）；
        episodes>1：每集「第X集」+「第X集审核」，均依赖大纲定稿（门禁）。
        episode_range：仅产出的集号（空=1..episodes）。
        style/pacing：结构化制作要求，透传给 executor 强约束。
        """
        self.concept = concept
        self.novel_ref = novel_ref
        self.style = list(style or [])
        self.pacing = pacing or ""
        self.episodes = max(1, int(episodes or 1))
        self.episode_range = sorted(
            {int(e) for e in (episode_range or []) if 1 <= int(e) <= self.episodes}
        ) or list(range(1, self.episodes + 1))
        self.novel_index = (
            NovelIndex(novel_ref, cache_root=Path(self._novel_cache_root))
            if novel_ref and self._novel_cache_root
            else NovelIndex(novel_ref) if novel_ref else None
        )
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
        if self.episodes <= 1:
            # 全本模式（向后兼容）：单「全剧本」+「剧本审核」
            screen = self.store.create_item(
                self.space, self.lead, title="全剧本",
                criteria="可拍性、台词节奏、史实",
            )
            screen_review = self.store.create_item(
                self.space, self.lead, title="剧本审核",
                criteria="台词/节奏/可拍性/史实",
                description=f"针对全剧本 #{screen['id']}",
            )
            # 串行依赖：全剧本 依赖 大纲v1 定稿（link parent 区别于 review 项的「针对」）
            self.store.link(self.space, self.lead, src=screen["id"], kind="parent", dst=outline["id"])
            self.ids["screen_item"] = screen["id"]
            self.ids["screen_review_item"] = screen_review["id"]
        else:
            # 分集模式：每集「第X集」产出 + 「第X集审核」，均依赖大纲定稿
            screen_items: dict[int, int] = {}
            screen_review_items: dict[int, int] = {}
            for ep in self.episode_range:
                s = self.store.create_item(
                    self.space, self.lead, title=f"第{ep}集",
                    criteria="可拍性、台词节奏、史实、单集完整起承转合",
                    description=f"{concept}（第{ep}/{self.episodes}集）",
                )
                r = self.store.create_item(
                    self.space, self.lead, title=f"第{ep}集审核",
                    criteria="台词/节奏/可拍性/史实",
                    description=f"针对第{ep}集 #{s['id']}",
                )
                self.store.link(self.space, self.lead, src=s["id"], kind="parent", dst=outline["id"])
                screen_items[ep] = s["id"]
                screen_review_items[ep] = r["id"]
            self.ids["screen_items"] = screen_items
            self.ids["screen_review_items"] = screen_review_items
        return self.ids

    def extend_episodes(self, episode_range: list[int]) -> dict:
        """加集续写：为新增集号建项（既有集原样保留），要求大纲已定稿。

        前情自动衔接：新增集动笔时注入前集摘要（+紧邻上集定稿全文，
        视 prior_context 档位）与历史审改意见；风格/节奏天然继承本流水线。
        """
        if not self._is_episodic():
            raise PipelineGuardError("当前为全本模式（单文件剧本），不支持加集；请新建分集流水线")
        outline_item = self.store.get_item(self.space, self.ids.get("outline_item", -1), actor=self.lead)
        if outline_item.get("state") != "done":
            raise PipelineGuardError("大纲尚未定稿，不能加集：请先完成大纲审核与拍板")
        existing: dict = dict(self.ids.get("screen_items", {}) or {})
        wanted = sorted({int(e) for e in (episode_range or []) if int(e) >= 1})
        new_eps = [e for e in wanted if e not in existing]
        if not new_eps:
            return {"added": [], "episodes": self.episodes, "episode_range": sorted(existing)}
        screen_items = dict(self.ids.get("screen_items", {}) or {})
        review_items = dict(self.ids.get("screen_review_items", {}) or {})
        total = max(self.episodes, max(new_eps))
        for ep in new_eps:
            s = self.store.create_item(
                self.space, self.lead, title=f"第{ep}集",
                criteria="可拍性、台词节奏、史实、单集完整起承转合",
                description=f"{self.concept}（第{ep}/{total}集·续写加集）",
            )
            r = self.store.create_item(
                self.space, self.lead, title=f"第{ep}集审核",
                criteria="台词/节奏/可拍性/史实",
                description=f"针对第{ep}集 #{s['id']}",
            )
            self.store.link(self.space, self.lead, src=s["id"], kind="parent", dst=self.ids["outline_item"])
            screen_items[ep] = s["id"]
            review_items[ep] = r["id"]
        self.ids["screen_items"] = screen_items
        self.ids["screen_review_items"] = review_items
        self.episode_range = sorted(set(self.episode_range) | set(new_eps))
        self.episodes = total
        self._save_drafts()  # _cfg 档位随草稿落盘
        return {"added": new_eps, "episodes": self.episodes, "episode_range": list(self.episode_range)}

    def start_outline_project(
        self,
        concept: str,
        *,
        novel_ref: str = "",
        style: Optional[list[str]] = None,
        pacing: str = "",
    ) -> dict:
        """启动大纲流水线：大纲 v1 + 审核（不产剧本）。style/pacing 透传给大纲生成。"""
        self.concept = concept
        self.novel_ref = novel_ref
        self.style = list(style or [])
        self.pacing = pacing or ""
        self.episodes = 1
        self.episode_range = []
        self.novel_index = (
            NovelIndex(novel_ref, cache_root=Path(self._novel_cache_root))
            if novel_ref and self._novel_cache_root
            else NovelIndex(novel_ref) if novel_ref else None
        )
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
        return self.store.get_item(self.space, item_id, actor=self.lead).get("criteria", "")

    def _outline_text(self) -> str:
        return self._drafts.get(self.ids.get("outline_item", -1), "")

    def _last_review_result(self, review_item_id: int) -> ReviewResult:
        item = self.store.get_item(self.space, review_item_id, actor=self.lead)
        for c in reversed(item.get("comments", []) or []):
            body = c.get("body") or c.get("comment") or ""
            if body:
                # 与 executor.review / 圆桌终审复用同一套 verdict 解析（含 VERDICT 行
                # 结构化 + 否定式清洗），避免「建议修改三处」这种弱意见被误判打回、
                # 或「不建议打回」被裸「打回」误杀。
                from .final_review import parse_verdict

                approved = parse_verdict(body)
                return ReviewResult(approved=approved, opinion=body)
        return ReviewResult(approved=True, opinion="（无审核意见，默认通过）")

    # -- 定稿导出（docx 落地，容错） -------------------------------------
    def _export_docx(self, item_kind: str, text: str, label: Optional[str] = None) -> None:
        """定稿文本导出 .docx（失败仅记日志，不阻断状态机）。

        空文本直接跳过导出（LLM 未配置/不可用时 draft 可能为空串），
        避免产出「只有标题的空 docx/md」（P2-7）。
        label：分集导出标注（如"第3集"），落 last_docx 时用键 "第3集" 区分。
        """
        if not (text or "").strip():
            logger.warning("定稿文本为空，跳过导出（请检查 LLM 是否可用，或先重写草稿）")
            return
        try:
            from .docx_export import export_creation_docx
        except Exception as exc:  # noqa: BLE001
            logger.warning("docx 模块加载失败（跳过导出）: %s", exc)
            return
        title = (self.concept or "未命名剧本")[:20]
        if label:
            title = f"{title} {label}"
        md = f"# {title}\n\n{text}"
        try:
            res = export_creation_docx(item_kind, title, md)
        except Exception as exc:  # noqa: BLE001
            logger.warning("docx 导出异常（不影响定稿）: %s", exc)
            return
        if res.get("ok"):
            label2 = "剧本" if item_kind == "screenplay" else "大纲"
            suffix = "" if not res.get("degraded") else "（md 降级）"
            key = label or item_kind
            self.last_docx[key] = res.get("path") or ""
            logger.info("%s定稿已导出%s: %s", label2, suffix, res.get("path"))
        else:
            logger.warning("定稿 docx 导出失败（不影响定稿）: %s", res.get("error"))

    # -- 大纲路线 ----------------------------------------------------------
    def draft_outline(self, outline_item_id: Optional[int] = None, *, on_event=None,
                       abort: "Optional[threading.Event]" = None) -> str:
        outline_item_id = outline_item_id or self.ids["outline_item"]
        self.store.assign(self.space, self.lead, outline_item_id, assignee=OUTLINE_WORKER)
        w = Actor(OUTLINE_WORKER, Role.WORKER)
        if self.store.get_item(self.space, outline_item_id, actor=self.lead)["state"] == "open":
            self.guard.transition(self.space, w, outline_item_id, to="in_progress", comment="开始撰写大纲")
        # 重写注入：用户打回意见（_user_notes["outline"]）+ 最近一次审核打回意见，
        # 一并拼进 prompt，让 LLM 真正按反馈改，而非原地重来。
        revision = self._user_notes.get("outline", "")
        rev_item = self.ids.get("outline_review_item")
        if rev_item is not None:
            rr = self._last_review_result(rev_item)
            if rr.opinion and "通过" not in rr.opinion:
                revision = (revision + "\n" + rr.opinion).strip() if revision else rr.opinion
        text = self.executor.draft_outline(
            self.concept,
            novel_text=self.novel_text,
            style=self.style,
            pacing=self.pacing,
            revision_notes=revision,
            character_profiles=self._char_profiles,
            on_event=on_event,
            abort=abort,
        )
        self._drafts[outline_item_id] = text
        self._save_drafts()  # 落盘：sidecar 重启后草稿可恢复
        self.guard.transition(self.space, w, outline_item_id, to="review", comment="大纲已成，请审")
        return text

    def review_outline(self, outline_item_id: Optional[int] = None,
                       review_item_id: Optional[int] = None, *, on_event=None,
                       abort: "Optional[threading.Event]" = None) -> ReviewResult:
        outline_item_id = outline_item_id or self.ids["outline_item"]
        review_item_id = review_item_id or self.ids["outline_review_item"]
        self.store.assign(self.space, self.lead, review_item_id, assignee=REVIEW_WORKER)
        draft = self._drafts.get(outline_item_id, "")
        result = self.executor.review(draft, criteria=self._criteria(review_item_id),
                                      novel_text=self.novel_text, on_event=on_event,
                                      abort=abort)
        w = Actor(REVIEW_WORKER, Role.WORKER)
        self.guard.transition(self.space, w, review_item_id, to="in_progress", comment="开始审核大纲")
        self.guard.transition(self.space, w, review_item_id, to="review", comment=result.opinion)
        # 审核意见落盘：经 item_content 供前端「看意见」预览（此前只写进状态机 comment，预览永远空白）
        self._drafts[review_item_id] = result.opinion
        self._save_drafts()
        return result

    def lead_verdict_outline(self, outline_item_id: Optional[int] = None,
                             review_item_id: Optional[int] = None,
                             result: Optional[ReviewResult] = None,
                             *, user_note: Optional[str] = None) -> str:
        outline_item_id = outline_item_id or self.ids["outline_item"]
        review_item_id = review_item_id or self.ids["outline_review_item"]
        if result is None:
            result = self._last_review_result(review_item_id)
        # 状态守卫（与剧本路线一致）：大纲须处于 review 才可定稿
        cur_state = self.store.get_item(self.space, outline_item_id, actor=self.lead)["state"]
        if result.approved and cur_state != "review":
            raise ValueError(
                f"大纲当前处于「{cur_state}」状态，须先重新撰写（写大纲）进入待审，再定稿"
            )
        if result.approved:
            self.guard.transition(self.space, self.lead, review_item_id, to="done", comment="验收通过")
            self.guard.transition(self.space, self.lead, outline_item_id, to="done", comment="大纲定稿")
            self._export_docx("outline", self._drafts.get(outline_item_id, ""))
            return "approved"
        # 打回：记录用户修改意见（重写时注入 prompt），产出项 review→in_progress 打回改。
        # 主编以 lead 身份打回（worker 无权做）。
        if user_note:
            self._user_notes["outline"] = user_note.strip()
            self._save_drafts()
        comment = f"打回修改：{user_note.strip()}" if user_note else f"打回修改：{result.opinion}"
        self.guard.transition(self.space, self.lead, outline_item_id, to="in_progress",
                              comment=comment)
        return "rejected"

    # -- 剧本路线 ----------------------------------------------------------
    def draft_screenplay(self, screen_item_id: Optional[int] = None, *, episode: Optional[int] = None,
                         on_event=None, abort: "Optional[threading.Event]" = None) -> str:
        """撰写剧本（分集模式按集写，episode 空则自动推进下一未定稿集）。"""
        # 防覆写：分集模式全部定稿后，「写剧本」若不指定集号会落到最后一集并
        # 静默重写其已定稿草稿——改为明确报错，要求用户指定要重写的集号。
        if screen_item_id is None and episode is None and self._is_episodic():
            items = self.ids.get("screen_items", {}) or {}
            nxt = self._next_episode()
            if nxt in items and self.store.get_item(self.space, items[nxt], actor=self.lead)[
                "state"
            ] == "done":
                raise ValueError(
                    f"全部 {len(items)} 集都已定稿。若要重写某一集，请明确指定集号"
                    f"（如「重写第{nxt}集」），以免覆盖已定稿的草稿"
                )
        screen_item_id = screen_item_id or self._screen_item_id(episode)
        review_item_id = self._screen_review_item_id(episode)
        if episode is None:
            episode = self._episode_of(screen_item_id)
        self.store.assign(self.space, self.lead, screen_item_id, assignee=SCREENPLAY_WORKER)
        w = Actor(SCREENPLAY_WORKER, Role.WORKER)
        if self.store.get_item(self.space, screen_item_id, actor=self.lead)["state"] == "open":
            # 门禁点：剧本 in_progress 前检查大纲是否 done；未定稿则抛 PipelineGuardError
            self.guard.transition(self.space, w, screen_item_id, to="in_progress", comment="开始撰写剧本")
        # 防漂移锚点：首次写剧本且无小传时，自动从圣经/大纲抽取人物小传（透明、幂等、落盘复用）。
        # 有原著取圣经（最全）；无原著但有大纲则取大纲（绕过门禁时大纲必已定稿）。
        if not self._char_profiles and (
            self.novel_index is not None or self._outline_text().strip()
        ):
            try:
                self.extract_characters()
            except Exception as exc:  # noqa: BLE001
                logger.warning("首次写剧本自动生成人物小传失败（跳过，后续可手动生成）: %s", exc)
        outline_text = self._outline_text()
        # 重写注入：用户打回意见（分集按集号存 key）+ 最近一次审核打回意见
        stage_key = f"screen:{episode}" if episode else "screen"
        revision = self._user_notes.get(stage_key, "")
        rev_item = self._screen_review_item_id(episode)
        rr = self._last_review_result(rev_item)
        if rr.opinion and "通过" not in rr.opinion:
            revision = (revision + "\n" + rr.opinion).strip() if revision else rr.opinion
        text = self.executor.draft_screenplay(
            outline_text,
            novel_text=self._novel_related(),
            style=self.style,
            pacing=self.pacing,
            episode=episode,
            episodes=self.episodes,
            prior_summaries=self._prior_context_block(episode),
            revision_notes=revision,
            character_profiles=self._char_profiles,
            on_event=on_event,
            abort=abort,
        )
        self._drafts[screen_item_id] = text
        if episode:
            from .executor import _summarize_segment

            self._episode_summaries[episode] = _summarize_segment(text)
        self._save_drafts()  # 落盘：sidecar 重启后草稿可恢复
        self.guard.transition(self.space, w, screen_item_id, to="review", comment="剧本已成，请审")
        return text

    def review_screenplay(self, screen_item_id: Optional[int] = None,
                          review_item_id: Optional[int] = None,
                          *, episode: Optional[int] = None, on_event=None,
                          abort: "Optional[threading.Event]" = None) -> ReviewResult:
        screen_item_id = screen_item_id or self._screen_item_id(episode)
        review_item_id = review_item_id or self._screen_review_item_id(episode)
        self.store.assign(self.space, self.lead, review_item_id, assignee=REVIEW_WORKER)
        draft = self._drafts.get(screen_item_id, "")
        result = self.executor.review(draft, criteria=self._criteria(review_item_id),
                                      novel_text=self.novel_text, on_event=on_event,
                                      abort=abort)
        w = Actor(REVIEW_WORKER, Role.WORKER)
        self.guard.transition(self.space, w, review_item_id, to="in_progress", comment="开始审核剧本")
        self.guard.transition(self.space, w, review_item_id, to="review", comment=result.opinion)
        # 审核意见落盘：经 item_content 供前端「看意见」预览
        self._drafts[review_item_id] = result.opinion
        self._save_drafts()
        return result

    def lead_verdict_screenplay(self, screen_item_id: Optional[int] = None,
                                review_item_id: Optional[int] = None,
                                result: Optional[ReviewResult] = None,
                                *, episode: Optional[int] = None,
                                user_note: Optional[str] = None) -> str:
        screen_item_id = screen_item_id or self._screen_item_id(episode)
        review_item_id = review_item_id or self._screen_review_item_id(episode)
        if result is None:
            result = self._last_review_result(review_item_id)
        # 状态守卫：产出项须处于 review（已写稿待拍板）才可定稿。
        # 打回后产出项为 in_progress（重写中），直接拍板会触发 board 底层
        # `in_progress → done` 非法转移 —— 改为明确中文报错，让用户先重写。
        cur_state = self.store.get_item(self.space, screen_item_id, actor=self.lead)["state"]
        if result.approved and cur_state != "review":
            raise ValueError(
                f"{('第'+str(episode)+'集') if episode else '剧本'}当前处于「{cur_state}」状态，须先重新撰写（写剧本）进入待审，再定稿"
            )
        if result.approved:
            self.guard.transition(self.space, self.lead, review_item_id, to="done", comment="验收通过")
            self.guard.transition(self.space, self.lead, screen_item_id, to="done", comment="剧本定稿")
            label = f"第{episode}集" if episode else None
            self._export_docx("screenplay", self._drafts.get(screen_item_id, ""), label=label)
            return "approved"
        # 打回：记录用户修改意见（重写时注入 prompt），产出项 review→in_progress 打回改。
        if user_note:
            stage_key = f"screen:{episode}" if episode else "screen"
            self._user_notes[stage_key] = user_note.strip()
            self._save_drafts()
        comment = f"打回修改：{user_note.strip()}" if user_note else f"打回修改：{result.opinion}"
        self.guard.transition(self.space, self.lead, screen_item_id, to="in_progress",
                              comment=comment)
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
