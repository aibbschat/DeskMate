"""创作对话流（chat_flow）：把创作流水线放入原生对话流。

核心设计：
- 用户在对话流里发消息 → 意图识别 → 执行对应创作动作 → 结果以消息回流
- 消息类型与前端 Markdown 形态对应：user / system / progress / artifact / review /
  milestone / expert / expert_card / roundtable / help
- 状态机（pipeline）保留为数据层约束，用户不再面对按钮关卡
- 专家召唤：全部 persona 可 @ 召唤，用 name+tagline 构造角色设定

持久化：消息存到 pipeline.base_dir / f"chat_{pid}.json"，sidecar 重启可恢复。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import AsyncGenerator, Optional

from .pipeline import CreativePipeline
from .executor import ReviewResult

logger = logging.getLogger(__name__)

# ---- 消息类型 ----
MSG_USER = "user"
MSG_SYSTEM = "system"
MSG_PROGRESS = "progress"
MSG_ARTIFACT = "artifact"
MSG_REVIEW = "review"
MSG_MILESTONE = "milestone"
MSG_EXPERT = "expert"
MSG_EXPERT_CARD = "expert_card"
MSG_ROUNDTABLE = "roundtable"
MSG_HELP = "help"
MSG_ERROR = "error"
MSG_INSPIRE = "inspire"  # 小桔的创作灵感（基于认知库，带真实世界溯源）


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _msg(type_: str, **kw) -> dict:
    msg = {"id": uuid.uuid4().hex[:8], "type": type_, "ts": _now()}
    msg.update(kw)
    return msg


# 灵感主动触发冷却（小时）：自由聊创作时，超过该间隔才主动递一条灵感，防刷屏
_INSPIRE_COOLDOWN_HOURS = 6


def _inspire_cooldown_path(base_dir) -> Path:
    base = Path(base_dir) if base_dir else Path.home() / ".deskmate"
    return base / "agency" / "last_inspire.json"


def _inspire_due(base_dir) -> bool:
    """语境主动灵感是否可触发。触发即刷冷却时间戳（无论是否真生成到内容）。"""
    try:
        path = _inspire_cooldown_path(base_dir)
        if path.exists():
            ts = json.loads(path.read_text(encoding="utf-8")).get("ts")
            if ts and (time.time() - float(ts)) < _INSPIRE_COOLDOWN_HOURS * 3600:
                return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": time.time()}, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return True


# ---- 意图识别 ----
class Intent:
    def __init__(self, type_: str, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


def _parse_episode(text: str) -> Optional[int]:
    """从文本里解析集号：第1集 / 第一集 / 1集。"""
    m = re.search(r"第?\s*(\d+)\s*集", text)
    if m:
        return int(m.group(1))
    cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    m = re.search(r"第?\s*([一二三四五六七八九十])\s*集", text)
    if m:
        return cn.get(m.group(1))
    return None


def parse_intent(text: str, state: dict) -> Intent:
    """关键词规则意图识别（不调 LLM，快且确定）。

    state: {phase, outline_state, outline_review_state, screen_state, screen_review_state,
            current_episode, episodes, is_screenplay}
    """
    t = text.strip()
    low = t.lower()

    # @专家召唤
    m = re.match(r"@([\w\-\u4e00-\u9fa5]+)\s*(.*)", t)
    if m:
        return Intent("expert", expert_name=m.group(1), question=m.group(2) or t)

    # 一键直通
    if re.search(r"一键直通|直通到完|自动写完|全部跑完|一口气", t):
        return Intent("auto_pilot")

    # 圆桌终审
    if re.search(r"圆桌|终审|研讨|七专家", t):
        return Intent("roundtable")

    # 导出
    if re.search(r"导出|下载|docx|保存文件", t):
        return Intent("export")

    # 写大纲
    if re.search(r"写大纲|生成大纲|大纲.*写|写.*大纲", t):
        return Intent("draft_outline")

    # 写剧本 / 第X集
    ep = _parse_episode(t)
    if ep is not None or re.search(r"写剧本|写第.*集|开始写|动笔", t):
        return Intent("draft_screen", episode=ep)

    # 审 / 审核（可带 @专家名，如"审一下，由@文学编辑审核"）
    if re.search(r"^审|审核|审一下|把关|评审", t):
        expert_name = None
        m = re.search(r"@([\w\-\u4e00-\u9fa5]+)", t)
        if m:
            expert_name = m.group(1)
        # 根据当前阶段判断审大纲还是审剧本
        if state.get("outline_state") == "review" and state.get("phase") in ("review_outline", "lead_outline"):
            return Intent("review_outline", expert_name=expert_name)
        return Intent("review_screen", episode=state.get("current_episode"), expert_name=expert_name)

    # 定稿 / 通过
    if re.search(r"定稿|通过|可以了|没问题|过了|ok$|好的.*定", low):
        if state.get("phase") == "lead_outline":
            return Intent("lead_outline", approved=True, note="")
        return Intent("lead_screen", approved=True, note="", episode=state.get("current_episode"))

    # 重写 / 打回
    if re.search(r"重写|打回|改成|不要这样|换个|重新写|不对|有问题", t):
        note = re.sub(r"^(重写|打回|改成|不要这样|换个|重新写|不对|有问题)[，,。.\s]*", "", t).strip()
        if state.get("phase") == "lead_outline":
            return Intent("lead_outline", approved=False, note=note or t)
        return Intent("lead_screen", approved=False, note=note or t, episode=state.get("current_episode"))

    # 帮助
    if re.search(r"帮助|help|怎么用|能做什么|指令", low):
        return Intent("help")

    # 灵感 / 创作建议（主动给启发，基于她观察到的真实世界）
    if re.search(r"灵感|给.*灵感|帮我想|缺创意|创作建议|没思路|卡住|写不下去|启发我|怎么找灵感", t):
        return Intent("inspire")

    return Intent("unknown", raw=t)


# ---- 创作对话核心 ----
class CreationChat:
    def __init__(self, pipeline: CreativePipeline, pid: str):
        self.pipeline = pipeline
        self.pid = pid
        self.messages: list[dict] = []
        self._load()
        if not self.messages:
            self._add(self._welcome_msg())

    # ---- 持久化 ----
    @property
    def _persist_path(self) -> Path:
        base = getattr(self.pipeline, "base_dir", None) or Path.home() / ".deskmate" / "creation_chat"
        return Path(base) / f"chat_{self.pid}.json"

    def _save(self) -> None:
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._persist_path.write_text(json.dumps(self.messages, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:
            logger.warning("创作对话持久化失败: %s", exc)

    def _load(self) -> None:
        try:
            if self._persist_path.exists():
                self.messages = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("创作对话加载失败: %s", exc)
            self.messages = []

    def _add(self, msg: dict) -> None:
        self.messages.append(msg)
        self._save()

    # ---- 反馈 → 信任/成长（P0-3）----
    def _record_creation_feedback(self, approved: bool) -> None:
        """拍板反馈写入成长系统（best-effort，不阻塞创作流程）。

        定稿/通过 → trust↑默契↑；打回重做 → trust↓默契↓（P0-3：反馈→信任/成长闭环）。
        同时写行为日志（P2-3 长期模式学习）：让「创作定稿/打回」这类主场景事件进入
        behavior_log.jsonl，供 mine_patterns 提炼用户的创作节奏规律。
        """
        try:
            from deskmate.growth.engine import GrowthEngine

            base = getattr(self.pipeline, "base_dir", None) or Path.home() / ".deskmate"
            GrowthEngine(base_dir=Path(base)).record_creation_feedback(approved)
        except Exception as exc:
            logger.debug("creation feedback -> growth 失败: %s", exc)
        # P2-3：行为事件落盘（零 LLM，fail-open）
        try:
            from deskmate.proactivity.behavior_patterns import log_event

            base = getattr(self.pipeline, "base_dir", None) or Path.home() / ".deskmate"
            kind = "creation_approved" if approved else "creation_rejected"
            log_event(kind, {"pipeline": type(self.pipeline).__name__}, base_dir=Path(base))
        except Exception as exc:
            logger.debug("creation feedback -> behavior_log 失败: %s", exc)

    # ---- 圣经回流（P1-6）----
    def _record_bible_feedback(
        self, item_id: int | None, episode: str | int | None = None, source: str = ""
    ) -> dict:
        """定稿后把该章产出回流进改编圣经（best-effort，不阻塞创作流程）。

        读 pipeline 草稿正文 → 提取新出现关键词 → 追加 bible.md 创作回流区 + 写日志。
        同一 novel+episode 幂等；无原著/无正文/圣经缺失时静默跳过。
        Returns: record_bible_feedback 的结果 dict（供定稿消息展示回流提示）。
        """
        try:
            pl = self.pipeline
            ref = getattr(pl, "novel_ref", "") or ""
            if not ref or item_id is None:
                return {"ok": False, "skipped": True, "reason": "无原著或产出项"}
            draft = (getattr(pl, "_drafts", {}) or {}).get(item_id)
            if not draft:
                return {"ok": False, "skipped": True, "reason": "无产出正文"}
            cache_root = getattr(pl, "_novel_cache_root", None) or None
            from deskmate.creation.bible_feedback import record_bible_feedback

            return record_bible_feedback(
                ref,
                episode if episode is not None else str(item_id),
                draft,
                cache_root=cache_root,
                source=source,
            )
        except Exception as exc:
            logger.debug("bible feedback 失败: %s", exc)
            return {"ok": False, "skipped": True, "reason": str(exc)}

    # ---- 状态快照 ----
    def _state(self) -> dict:
        pl = self.pipeline
        store = pl.store
        space = pl.space
        ids = pl.ids
        outline_item = store.get_item(space, ids.get("outline_item", -1)) if ids.get("outline_item") else {}
        outline_review = store.get_item(space, ids.get("outline_review_item", -1)) if ids.get("outline_review_item") else {}
        screen_items = ids.get("screen_items") or {}
        current_ep = pl._next_episode() if pl._is_episodic() else None
        # screen_items 内存中是 int 键（pipeline.py:415），JSON 恢复后是 str 键；
        # 统一用 int(ep) 取（恢复路径已在 service._restore_pipeline 归一化回 int）
        screen_item = (
            store.get_item(space, screen_items[int(current_ep)])
            if current_ep and int(current_ep) in screen_items
            else {}
        )
        return {
            "phase": self._phase(),
            "outline_state": outline_item.get("state", "open"),
            "outline_review_state": outline_review.get("state", "open"),
            "screen_state": screen_item.get("state", "open"),
            "current_episode": current_ep,
            "episodes": pl.episodes,
            "is_screenplay": pl._is_episodic() or ids.get("screen_item") is not None,
        }

    def _phase(self) -> str:
        try:
            from .service import _phase_of
            return _phase_of(self.pipeline)
        except Exception:
            return "draft_outline"

    # ---- 欢迎/帮助消息 ----
    def _welcome_msg(self) -> dict:
        concept = getattr(self.pipeline, "concept", "")
        episodes = getattr(self.pipeline, "episodes", 1)
        return _msg(MSG_SYSTEM, text=(
            f"🎬 创作空间已就绪：《{concept or "未命名"}》（{episodes} 集）\n\n"
            "你可以直接说话推进创作，例如：\n"
            "· 「写大纲」\n"
            "· 「写第1集」\n"
            "· 「审一下」\n"
            "· 「定稿」 / 「重写，强调动机」\n"
            "· 「@历史学家 说说双屿港背景」\n"
            "· 「一键直通到完」\n\n"
            "所有决策你说了算，专家只是顾问。"
        ))

    def _help_msg(self) -> dict:
        return _msg(MSG_HELP, text=(
            "**可用指令：**\n\n"
            "| 指令 | 作用 |\n|---|---|\n"
            "| 写大纲 | 生成大纲 |\n"
            "| 写第X集 / 写剧本 | 生成第X集剧本 |\n"
            "| 审 / 审核 | 审核当前产出 |\n"
            "| 定稿 / 通过 | 定稿当前产出 |\n"
            "| 重写，XXX | 打回并附修改意见 |\n"
            "| @专家名 XXX | 召唤任意专家参与讨论 |\n"
            "| 圆桌 / 终审 | 触发7专家圆桌研讨 |\n"
            "| 一键直通 | 自动跑完大纲→剧本→定稿 |\n"
            "| 导出 | 导出当前定稿 docx |\n"
        ))

    # ---- 主入口：用户发消息 ----
    async def post(self, text: str) -> AsyncGenerator[dict, None]:
        user_msg = _msg(MSG_USER, text=text)
        self._add(user_msg)
        yield user_msg
        state = self._state()
        intent = parse_intent(text, state)
        try:
            async for msg in self._execute(intent, state):
                self._add(msg)
                yield msg
        except Exception as exc:
            logger.exception("创作对话执行失败")
            err = _msg(MSG_ERROR, text=f"执行出错：{exc}")
            self._add(err)
            yield err

    async def _execute(self, intent: Intent, state: dict) -> AsyncGenerator[dict, None]:
        handler = {
            "draft_outline": self._do_draft_outline,
            "review_outline": self._do_review_outline,
            "lead_outline": self._do_lead_outline,
            "draft_screen": self._do_draft_screen,
            "review_screen": self._do_review_screen,
            "lead_screen": self._do_lead_screen,
            "expert": self._do_expert,
            "roundtable": self._do_roundtable,
            "auto_pilot": self._do_auto_pilot,
            "export": self._do_export,
            "help": self._do_help,
            "inspire": self._do_inspire,
            "unknown": self._do_unknown,
        }.get(intent.type)
        if handler is None:
            yield self._help_msg()
            return
        async for msg in handler(intent, state):
            yield msg

    # ---- 进度事件桥：on_event → progress 消息 ----
    async def _progress_stream(self, label: str, fn, *args, **kwargs):
        """在线程池执行同步 LLM 管线 fn，避免阻塞 asyncio 事件循环。

        on_event 回调在 worker 线程内累积事件；fn 完成后才返回
        (progress_msg_or_None, result)。原本是同步 def，会在 async 生成器里
        直接跑同步 LLM 调用，阻塞整个事件循环（A3 修复）。
        """
        events: list[dict] = []

        def on_event(ev: dict):
            events.append(ev)

        kwargs["on_event"] = on_event
        result = await asyncio.to_thread(fn, *args, **kwargs)
        progress_msg = None
        if events:
            last = events[-1]
            progress_msg = _msg(MSG_PROGRESS, label=label, message=last.get("message", label),
                                progress=last.get("progress", 100),
                                events=[{"message": e.get("message", ""), "progress": e.get("progress")} for e in events])
        return progress_msg, result

    # ---- 动作：写大纲 ----
    async def _do_draft_outline(self, intent: Intent, state: dict) -> AsyncGenerator[dict, None]:
        if state.get("outline_state") == "done":
            yield _msg(MSG_SYSTEM, text="大纲已定稿，如需修改请说「重写大纲」。")
            return
        progress_msg, result = await self._progress_stream("撰写大纲", self.pipeline.draft_outline)
        if progress_msg:
            yield progress_msg
        if result:
            yield _msg(MSG_ARTIFACT, kind="outline", title="大纲 v1", text=result)
            yield _msg(MSG_SYSTEM, text="大纲已生成。可以说「审一下」让审核专家把关，或「定稿」直接通过。")

    # ---- 指定专家审核（用圆桌 persona 的 system_prompt，结果落盘） ----
    def _review_with_persona(self, expert_name: str, kind: str = "outline",
                              episode: Optional[int] = None) -> Optional[ReviewResult]:
        """用指定圆桌专家审核文本，结果落盘到 pipeline store。返回 ReviewResult；专家未找到返回 None。"""
        from deskmate.roundtable.personas import load_personas
        from deskmate.memory.llm import chat, llm_available
        from coworker.teams.model import Actor, Role
        from .pipeline import REVIEW_WORKER

        # 按 name / id / 模糊匹配专家
        persona = None
        for p in load_personas():
            if (p.name == expert_name or p.id == expert_name
                    or expert_name.lower() in p.name.lower()
                    or expert_name.lower() in p.id.lower()):
                persona = p
                break
        if persona is None:
            return None

        # 取待审文本和审核标准
        if kind == "outline":
            item_id = self.pipeline.ids.get("outline_item")
            review_id = self.pipeline.ids.get("outline_review_item")
        else:
            screen_items = self.pipeline.ids.get("screen_items") or {}
            review_items = self.pipeline.ids.get("screen_review_items") or {}
            ep = episode or self.pipeline._next_episode()
            item_id = screen_items.get(int(ep)) if ep is not None else None
            review_id = review_items.get(int(ep)) if ep is not None else None
        draft = self.pipeline._drafts.get(item_id, "") if item_id else ""
        criteria = self.pipeline._criteria(review_id) if review_id else ""

        if not llm_available():
            return ReviewResult(approved=True, opinion=f"（LLM 未配置，{persona.name} 默认通过）")

        # 用 persona.system_prompt + 结构化审核要求
        system = (
            f"{persona.system_prompt}\n\n"
            "现在请你以审核专家身份，依据验收标准评审以下文本。\n"
            "输出格式：\n"
            "结论：通过/打回\n"
            "分维度核验：（逐项说明好在哪里、弱在哪里）\n"
            "可执行修改意见：（每条包含 问题是什么 + 为什么有问题 + 怎么改）\n"
            "若提供原著参考，须对照原著评判改编适配度。"
        )
        novel_text = getattr(self.pipeline, "novel_text", "") or ""
        novel_block = f"\n\n【原著参考】\n{novel_text}\n" if novel_text else ""
        raw = chat(system=system, user=f"验收标准：{criteria}\n\n文本：\n{draft}{novel_block}", max_tokens=6000)
        approved = "打回" not in raw and "不合格" not in raw
        result = ReviewResult(approved=approved, opinion=raw)

        # 落盘（复制 pipeline.review_outline 的存储逻辑，确保后续定稿能找到审核结果）
        if review_id:
            w = Actor(REVIEW_WORKER, Role.WORKER)
            self.pipeline.store.assign(self.pipeline.space, self.pipeline.lead, review_id, assignee=REVIEW_WORKER)
            # comment 带实际审核专家名（前端/定稿追溯可识别是谁审的，而非通用 script-reviewer）
            self.pipeline.guard.transition(self.pipeline.space, w, review_id, to="in_progress", comment=f"{persona.name} 开始审核")
            self.pipeline.guard.transition(self.pipeline.space, w, review_id, to="review", comment=f"{persona.name} 审核意见：{result.opinion}")
            self.pipeline._drafts[review_id] = f"{persona.name} 审核意见：{result.opinion}"
            self.pipeline._save_drafts()
        return result

    # ---- 动作：审大纲 ----
    async def _do_review_outline(self, intent: Intent, state: dict) -> AsyncGenerator[dict, None]:
        if state.get("outline_state") not in ("review",):
            yield _msg(MSG_SYSTEM, text="大纲还没写，先说「写大纲」。")
            return
        expert_name = getattr(intent, "expert_name", None)
        result = None
        if expert_name:
            result = self._review_with_persona(expert_name, kind="outline")
            if result is None:
                yield _msg(MSG_SYSTEM, text=f"没有找到「{expert_name}」专家，回退到通用审核。")
                _, result = await self._progress_stream("审核大纲", self.pipeline.review_outline)
            else:
                yield _msg(MSG_PROGRESS, label=f"{expert_name} 审核", message=f"{expert_name} 已完成审核", progress=100)
        else:
            progress_msg, result = await self._progress_stream("审核大纲", self.pipeline.review_outline)
            if progress_msg:
                yield progress_msg
        if result:
            approved = getattr(result, "approved", True)
            opinion = getattr(result, "opinion", "")
            reviewer = expert_name or "审核专家"
            yield _msg(MSG_REVIEW, kind="outline", approved=approved, text=opinion, expert=reviewer,
                       criteria="结构完整 · 契合原著精神 · 无逻辑硬伤 · 史实/逻辑/市场")
            if approved:
                yield _msg(MSG_SYSTEM, text=f"{reviewer}建议通过。可以说「定稿」确认，或「重写，XXX」打回修改。")
            else:
                yield _msg(MSG_SYSTEM, text=f"{reviewer}建议打回。可以说「重写，XXX」附上你的修改意见。")

    # ---- 动作：大纲定稿/打回 ----
    async def _do_lead_outline(self, intent: Intent, state: dict) -> AsyncGenerator[dict, None]:
        approved = getattr(intent, "approved", True)
        note = getattr(intent, "note", "")
        result = ReviewResult(approved=approved, opinion="（主编拍板通过）" if approved else f"（主编打回）{note}")
        try:
            verdict = self.pipeline.lead_verdict_outline(result=result, user_note=note or None)
        except Exception as exc:
            yield _msg(MSG_ERROR, text=f"定稿失败：{exc}")
            return
        # P0-3：拍板反馈 → 信任/成长闭环（best-effort）
        self._record_creation_feedback(verdict == "approved")
        if verdict == "approved":
            # P1-6：大纲定稿 → 回流进圣经（best-effort）
            bf = self._record_bible_feedback(
                self.pipeline.ids.get("outline_item"), "大纲", "大纲定稿"
            )
            _text = "你是主编，决策已记录。大纲 docx 已自动落地。"
            if bf.get("ok") and not bf.get("skipped") and bf.get("entities"):
                _text += f" 圣经已回流：新增 {len(bf['entities'])} 条关键词。"
            yield _msg(MSG_MILESTONE, kind="outline", title="大纲已定稿", text=_text)
            if self.pipeline._is_episodic() or self.pipeline.ids.get("screen_item"):
                yield _msg(MSG_SYSTEM, text="大纲已定稿，剧本已解锁。可以说「写第1集」开始写剧本。")
        else:
            yield _msg(MSG_SYSTEM, text=f"大纲已打回。{f'修改意见：{note}' if note else ''} 说「写大纲」重新生成。")

    # ---- 动作：写剧本 ----
    async def _do_draft_screen(self, intent: Intent, state: dict) -> AsyncGenerator[dict, None]:
        if state.get("outline_state") != "done":
            yield _msg(MSG_SYSTEM, text="大纲还没定稿，先完成大纲（「写大纲」→「审」→「定稿」）。")
            return
        episode = getattr(intent, "episode", None) or state.get("current_episode")
        if self.pipeline._is_episodic() and episode is None:
            episode = self.pipeline._next_episode()
        if episode is not None and episode > self.pipeline.episodes:
            yield _msg(MSG_SYSTEM, text=f"只有 {self.pipeline.episodes} 集，第{episode}集不存在。")
            return
        kw = {"episode": episode} if episode else {}
        progress_msg, result = await self._progress_stream(f"撰写第{episode or ''}集剧本", self.pipeline.draft_screenplay, **kw)
        if progress_msg:
            yield progress_msg
        if result:
            title = f"第{episode}集" if episode else "全剧本"
            yield _msg(MSG_ARTIFACT, kind="screenplay", title=title, episode=episode, text=result)
            yield _msg(MSG_SYSTEM, text=f"{title}剧本已生成。可以说「审一下」审核，或「定稿」通过。")

    # ---- 动作：审剧本 ----
    async def _do_review_screen(self, intent: Intent, state: dict) -> AsyncGenerator[dict, None]:
        episode = getattr(intent, "episode", None) or state.get("current_episode")
        expert_name = getattr(intent, "expert_name", None)
        result = None
        if expert_name:
            result = self._review_with_persona(expert_name, kind="screen", episode=episode)
            if result is None:
                yield _msg(MSG_SYSTEM, text=f"没有找到「{expert_name}」专家，回退到通用审核。")
                kw = {"episode": episode} if episode else {}
                _, result = await self._progress_stream(f"审核第{episode or ''}集剧本", self.pipeline.review_screenplay, **kw)
            else:
                yield _msg(MSG_PROGRESS, label=f"{expert_name} 审核", message=f"{expert_name} 已完成审核", progress=100)
        else:
            kw = {"episode": episode} if episode else {}
            progress_msg, result = await self._progress_stream(f"审核第{episode or ''}集剧本", self.pipeline.review_screenplay, **kw)
            if progress_msg:
                yield progress_msg
        if result:
            approved = getattr(result, "approved", True)
            opinion = getattr(result, "opinion", "")
            reviewer = expert_name or "审核专家"
            yield _msg(MSG_REVIEW, kind="screenplay", episode=episode, approved=approved, text=opinion, expert=reviewer,
                       criteria="可拍性 · 台词节奏 · 史实 · 单集起承转合 · 契合原著")
            if approved:
                yield _msg(MSG_SYSTEM, text=f"{reviewer}建议通过。说「定稿」确认，或「重写，XXX」打回。")
            else:
                yield _msg(MSG_SYSTEM, text=f"{reviewer}建议打回。说「重写，XXX」附上修改意见。")

    # ---- 动作：剧本定稿/打回 ----
    async def _do_lead_screen(self, intent: Intent, state: dict) -> AsyncGenerator[dict, None]:
        approved = getattr(intent, "approved", True)
        note = getattr(intent, "note", "")
        episode = getattr(intent, "episode", None) or state.get("current_episode")
        result = ReviewResult(approved=approved, opinion="（主编拍板通过）" if approved else f"（主编打回）{note}")
        kw = {"episode": episode} if episode else {}
        try:
            verdict = self.pipeline.lead_verdict_screenplay(result=result, user_note=note or None, **kw)
        except Exception as exc:
            yield _msg(MSG_ERROR, text=f"定稿失败：{exc}")
            return
        # P0-3：拍板反馈 → 信任/成长闭环（best-effort）
        self._record_creation_feedback(verdict == "approved")
        title = f"第{episode}集" if episode else "剧本"
        if verdict == "approved":
            # P1-6：剧本定稿 → 回流进圣经（best-effort；episode 去重）
            bf = self._record_bible_feedback(
                self.pipeline._screen_item_id(episode), episode, title
            )
            _text = f"你是主编，决策已记录。{title} docx 已自动落地。"
            if bf.get("ok") and not bf.get("skipped") and bf.get("entities"):
                _text += f" 圣经已回流：{title}新增 {len(bf['entities'])} 条关键词。"
            yield _msg(MSG_MILESTONE, kind="screenplay", title=f"{title}已定稿", episode=episode,
                       text=_text)
            # 自动推进下一集提示
            if self.pipeline._is_episodic():
                nxt = self.pipeline._next_episode()
                if nxt and nxt <= self.pipeline.episodes:
                    yield _msg(MSG_SYSTEM, text=f"可以说「写第{nxt}集」继续，或「一键直通」自动跑完。")
        else:
            yield _msg(MSG_SYSTEM, text=f"{title}已打回。{f'修改意见：{note}' if note else ''} 说「写第{episode or ''}集」重新生成。")

    # ---- 动作：专家召唤 ----
    async def _do_expert(self, intent: Intent, state: dict) -> AsyncGenerator[dict, None]:
        expert_name = getattr(intent, "expert_name", "")
        question = getattr(intent, "question", "") or getattr(intent, "raw", "")
        # 从 persona registry 加载
        try:
            from coworker.personas.registry import get_registry
            reg = get_registry()
            persona = None
            for pid, entry in reg.items():
                if pid.lower() == expert_name.lower() or getattr(entry, "name", "") == expert_name:
                    persona = entry
                    break
            if persona is None:
                # 模糊匹配
                for pid, entry in reg.items():
                    if expert_name.lower() in pid.lower() or expert_name in getattr(entry, "name", ""):
                        persona = entry
                        break
        except Exception as exc:
            logger.warning("加载 persona 失败: %s", exc)
            persona = None

        if persona is None:
            all_names = []
            try:
                from coworker.personas.registry import get_registry
                all_names = [f"@{getattr(e,'name',pid)}" for pid, e in get_registry().items()][:15]
            except Exception:
                pass
            yield _msg(MSG_SYSTEM, text=f"没有找到「{expert_name}」专家。可用专家示例：{', '.join(all_names)}")
            return

        name = getattr(persona, "name", expert_name)
        tagline = getattr(persona, "tagline", "")
        system_prompt = f"你是{name}（{tagline}）。请以你的专业视角回答用户的问题，给出具体、可操作的建议。"
        # 流式生成专家发言
        try:
            from deskmate.memory.llm import chat_stream, llm_available
            if not llm_available():
                yield _msg(MSG_EXPERT, expert=name, text="（LLM 未配置，专家暂无法发言）")
                return
            full = []
            yield _msg(MSG_PROGRESS, label=f"{name} 思考中…", message=f"@{name} 正在分析…", progress=30)
            async for delta in chat_stream(system_prompt, question, temperature=0.7, max_tokens=3000):
                full.append(delta)
            text = "".join(full).strip()
            yield _msg(MSG_EXPERT, expert=name, tagline=tagline, text=text or "（专家未给出回复）")
        except Exception as exc:
            yield _msg(MSG_ERROR, text=f"专家召唤失败：{exc}")

    # ---- 动作：圆桌终审 ----
    async def _do_roundtable(self, intent: Intent, state: dict) -> AsyncGenerator[dict, None]:
        yield _msg(MSG_SYSTEM, text="🎭 圆桌终审需要先选择研讨主题和专家。请在创作空间里点击「送圆桌终审」，或告诉我要研讨什么（例如「圆桌研讨第2集栖凤村血仇处理」）。")
        # 简化：直接触发圆桌提案（复用现有 final_review 逻辑需要四要素，这里先给引导）
        yield _msg(MSG_EXPERT_CARD, text="圆桌终审建议：选择 3-7 位专家，围绕当前产出的争议点研讨。专家发言会以对话流呈现。")

    # ---- 动作：一键直通 ----
    async def _do_auto_pilot(self, intent: Intent, state: dict) -> AsyncGenerator[dict, None]:
        yield _msg(MSG_SYSTEM, text="⚡ 一键直通模式：自动跑完 大纲→审核→定稿→剧本→审核→定稿。你可以随时打断。")
        # 大纲
        if state.get("outline_state") != "done":
            async for msg in self._do_draft_outline(intent, self._state()):
                yield msg
            async for msg in self._do_review_outline(intent, self._state()):
                yield msg
            async for msg in self._do_lead_outline(Intent("lead_outline", approved=True, note=""), self._state()):
                yield msg
        # 剧本（逐集）
        if self.pipeline._is_episodic():
            for ep in range(1, self.pipeline.episodes + 1):
                st = self._state()
                screen_items = self.pipeline.ids.get("screen_items") or {}
                sid = screen_items.get(int(ep))
                if sid:
                    item = self.pipeline.store.get_item(self.pipeline.space, sid)
                    if item.get("state") == "done":
                        continue
                async for msg in self._do_draft_screen(Intent("draft_screen", episode=ep), st):
                    yield msg
                async for msg in self._do_review_screen(Intent("review_screen", episode=ep), self._state()):
                    yield msg
                async for msg in self._do_lead_screen(Intent("lead_screen", approved=True, note="", episode=ep), self._state()):
                    yield msg
        else:
            st = self._state()
            if st.get("screen_state") != "done":
                async for msg in self._do_draft_screen(Intent("draft_screen"), st):
                    yield msg
                async for msg in self._do_review_screen(Intent("review_screen"), self._state()):
                    yield msg
                async for msg in self._do_lead_screen(Intent("lead_screen", approved=True, note=""), self._state()):
                    yield msg
        yield _msg(MSG_MILESTONE, kind="complete", title="全部完成", text="🎉 一键直通完成，所有产出已定稿并导出 docx。")

    # ---- 动作：导出 ----
    async def _do_export(self, intent: Intent, state: dict) -> AsyncGenerator[dict, None]:
        docx = getattr(self.pipeline, "last_docx", {})
        if not docx:
            yield _msg(MSG_SYSTEM, text="还没有定稿的产出。先完成「定稿」再导出。")
            return
        lines = [f"· {k}: {v}" for k, v in docx.items()]
        yield _msg(MSG_SYSTEM, text=f"已导出 docx：\n{chr(10).join(lines)}")

    # ---- 帮助 / 未知 ----
    async def _do_help(self, intent: Intent, state: dict) -> AsyncGenerator[dict, None]:
        yield self._help_msg()

    async def _do_unknown(self, intent: Intent, state: dict) -> AsyncGenerator[dict, None]:
        raw = getattr(intent, "raw", "")
        # 语境主动（B 主动发言）：自由聊创作且冷却允许时，主动递一条灵感
        if _inspire_due(getattr(self.pipeline, "base_dir", None)):
            async for msg in self._do_inspire(intent, state):
                yield msg
            return
        yield _msg(MSG_SYSTEM, text=(
            f"我理解你说的是「{raw[:40]}」，但不确定要做什么。\n\n"
            "你可以试试这些指令：\n"
            "· 「写大纲」 / 「写第1集」\n"
            "· 「审一下」 / 「定稿」 / 「重写，XXX」\n"
            "· 「@专家名 问题」召唤专家\n"
            "· 「一键直通」自动跑完\n"
            "· 「帮助」查看全部指令"
        ))


    # ---- 动作：创作灵感（基于认知库的真实世界启发） ----
    async def _do_inspire(self, intent: Intent, state: dict) -> AsyncGenerator[dict, None]:
        user_text = getattr(intent, "raw", "") or ""
        base = getattr(self.pipeline, "base_dir", None) or Path.home() / ".deskmate"
        concept = getattr(self.pipeline, "concept", "") or ""
        try:
            from deskmate.agency.behaviors.creative_suggest import generate as gen_suggest

            out = gen_suggest(base, concept, user_text=user_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("灵感生成失败: %s", exc)
            out = None
        if not out or not out.get("suggestion"):
            yield _msg(
                MSG_SYSTEM,
                text="我翻了翻最近观察到的世界，暂时没找到能照进这本书的素材——等我看得多一点，再跟你说。",
            )
            return
        refs = out.get("refs") or []
        ref_facts: list[str] = []
        for r in refs:
            if isinstance(r, list):
                ref_facts.extend(str(x) for x in r)
            elif r:
                ref_facts.append(str(r))
        ref_s = ""
        if ref_facts:
            ref_s = "\n\n（锚定她观察到的真实世界：" + "；".join(ref_facts[:3]) + "）"
        yield _msg(MSG_INSPIRE, text=out.get("suggestion", "") + ref_s, refs=ref_facts)


# ---- 全局注册表（与 service._PIPELINES 对应） ----
_CHATS: dict[str, CreationChat] = {}


def get_chat(pid: str, pipeline: Optional[CreativePipeline] = None) -> Optional[CreationChat]:
    if pid in _CHATS:
        return _CHATS[pid]
    if pipeline is not None:
        chat = CreationChat(pipeline, pid)
        _CHATS[pid] = chat
        return chat
    return None
