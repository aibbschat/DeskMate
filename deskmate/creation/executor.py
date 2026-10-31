"""创作执行器：把「写大纲/写剧本/审」的具体生成从编排中解耦。

- ``StubExecutor``：不调 LLM，返回确定性占位文本，``review`` 默认通过（可注入一次
  拒绝用于验证打回链路）。开发/验证用。
- ``LLMExecutor``：复用 ``deskmate.memory.llm.chat`` 真实接入（阶段 1 留作清晰对接点，
  环境需配置 provider 凭证）。

两者都实现 ``CreativeExecutor`` 协议，pipeline 不关心背后是否真调 LLM。
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ReviewResult:
    approved: bool
    opinion: str


def _emit(on_event, *, type: str, **kw) -> None:
    """把一条进度事件上报给回调（SSE 流式的阶段/进度消息）。

    on_event 为 None（老调用方/同步路径）时不发；回调自身异常一律吞掉，
    绝不让进度上报中断生成主流程。
    """
    if not on_event:
        return
    try:
        on_event({"type": type, **kw})
    except Exception:  # noqa: BLE001
        pass


def _requirement_block(
    style: Optional[list[str]] = None,
    pacing: str = "",
    episode: Optional[int] = None,
    episodes: int = 1,
    style_fingerprint: Optional[str] = None,
) -> str:
    """把结构化制作要求（风格/节奏/集数/文风指纹）拼成强约束段，注入创作 prompt。

    与 concept 的「软性剧情点子」不同，这一段是**硬性制作规范**：
    prompt 明示「必须严格遵守，违者返工」，让 LLM 不再靠自觉跟随风格。

    style_fingerprint：P1-5 量化风格指纹（萧哥声口的统计特征）。默认 None 时自动
    从 ~/.deskmate/style_profile.json 加载（零配置：无画像则返回空串、不污染）。
    """
    if style_fingerprint is None:
        from deskmate.style_profile_store import format_style_fingerprint

        style_fingerprint = format_style_fingerprint()
    parts: list[str] = []
    if style:
        parts.append(f"- 风格基调：{'、'.join(str(s) for s in style)}")
    if pacing:
        parts.append(f"- 叙事节奏：{pacing}")
    if episodes and episodes > 1:
        ep = int(episode or 1)
        parts.append(f"- 集数定位：第 {ep}/{episodes} 集（单集须有完整起承转合，结尾留钩子）")
    if style_fingerprint:
        indented = "\n  ".join(style_fingerprint.splitlines())
        parts.append(f"- 文风指纹（萧哥写作风格量化特征，须贴合）：\n  {indented}")
    if not parts:
        return ""
    return "\n\n【制作要求（必须严格遵守，违者返工）】\n" + "\n".join(parts)


def _revision_block(notes: str) -> str:
    """把用户对上一版的修改意见拼成强约束段，注入重写 prompt。

    与 _requirement_block（风格/节奏硬规范）不同，这是**相对上版的增量
    修订要求**——明确告诉 LLM「上一版哪里不行、这一版改什么」。空意见返回 ""。
    """
    notes = (notes or "").strip()
    if not notes:
        return ""
    return "\n\n【修改要求（针对上一版的修订，必须满足）】\n" + notes


def _character_block(profiles: str, *, for_outline: bool = False) -> str:
    """把「人物小传」拼成防漂移硬锚点段，注入创作 prompt。

    每集创作时重注入，约束人物动机/性格/弧光/关系须一致，禁止 OOC（正派无端行恶、
    凭空反转关系等）。空小传返回 ""。for_outline=True 时语义为「大纲人物设定须以
    小传为准」；否则为「剧本创作须严格遵循」。
    """
    profiles = (profiles or "").strip()
    if not profiles:
        return ""
    if for_outline:
        tail = (
            "（若提供人物小传，大纲中的人物设定须以人物小传为准，"
            "不得擅自增减主要人物或改变其核心动机。）"
        )
    else:
        tail = (
            "（铁律：人物动机/性格/弧光/关系须与人物小传一致；禁止人物漂移——"
            "不得擅自改变角色核心动机、让正派无端行恶、或写出与既定关系矛盾的互动；"
            "若剧情需要发展人物，须在原著动机延伸线上，不可凭空反转。）"
        )
    return f"\n\n【人物小传（防漂移硬锚点，须严格遵循）】\n{profiles}\n{tail}"


def _parse_scenes(plan: str) -> list[tuple[str, str]]:
    """解析场景清单行「1.场景名|一句话目标」→ [(场景名, 目标), ...]。

    容错：无序号前缀 / 无「|」分隔的行也收纳（场景名或整行）。
    """
    out: list[tuple[str, str]] = []
    for line in (plan or "").splitlines():
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        body = re.sub(r"^\d+[\.、\)）]\s*", "", line)  # 去数字序号（1. / 2、/ 3)）
        body = re.sub(r"^[-–—*]\s*", "", body).strip()  # 去列表符号（- / — / *）
        if "|" in body:
            name, goal = body.split("|", 1)
            name = name.strip() or f"场景{len(out) + 1}"
            goal = goal.strip()
            out.append((name, goal))
        elif body:
            out.append((body, ""))
    return out


def _summarize_segment(seg: str) -> str:
    """把刚生成的剧本段压缩为简短前情摘要（LLM；不可用则截断兜底）。

    分段续写的关键：每段 prompt 只带「前情摘要」而非前文全文——保连贯的同时
    不随段数线性放大 token。
    """
    from deskmate.memory.llm import chat, llm_available

    fallback = f"（前情：{seg[:120]}…）"
    if not llm_available():
        return fallback
    try:
        s = chat(
            system="把剧本片段压缩为 150 字以内的前情摘要：只保留人物当前状态、"
            "关键事件、未解决的悬念，不要评价。",
            user=seg,
            max_tokens=300,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("前情摘要失败（截断兜底）: %s", exc)
        return fallback
    return (s or "").strip() or fallback


class CreativeExecutor:
    def draft_outline(
        self,
        concept: str,
        *,
        novel_text: str = "",
        style: Optional[list[str]] = None,
        pacing: str = "",
        revision_notes: str = "",
        character_profiles: str = "",
        on_event=None,
        abort: "Optional[threading.Event]" = None,
    ) -> str: ...
    def draft_screenplay(
        self,
        outline: str,
        *,
        novel_text: str = "",
        style: Optional[list[str]] = None,
        pacing: str = "",
        episode: Optional[int] = None,
        episodes: int = 1,
        prior_summaries: str = "",
        revision_notes: str = "",
        character_profiles: str = "",
        on_event=None,
        abort: "Optional[threading.Event]" = None,
    ) -> str: ...
    def review(self, text: str, criteria: str, novel_text: str = "", on_event=None,
                abort: "Optional[threading.Event]" = None) -> ReviewResult: ...


class StubExecutor:
    def __init__(self, reject_once: bool = False) -> None:
        self.reject_once = reject_once
        self._rejected = False

    def draft_outline(
        self,
        concept: str,
        *,
        novel_text: str = "",
        style: Optional[list[str]] = None,
        pacing: str = "",
        revision_notes: str = "",
        character_profiles: str = "",
        on_event=None,
        abort: "Optional[threading.Event]" = None,
    ) -> str:
        _emit(on_event, type="note", message="正在撰写大纲（stub）…", progress=50)
        tag = f"·附原著{len(novel_text)}字" if novel_text else ""
        req = f"·{','.join(style)}" if style else ""
        req += f"·{pacing}" if pacing else ""
        char = f"·人物小传{len(character_profiles)}字" if character_profiles else ""
        rev = f"·改：{revision_notes[:40]}" if revision_notes else ""
        _emit(on_event, type="note", message="大纲初稿完成", progress=100)
        return f"[大纲·stub{tag}{req}{char}{rev}] {concept[:60]}"

    def draft_screenplay(
        self,
        outline: str,
        *,
        novel_text: str = "",
        style: Optional[list[str]] = None,
        pacing: str = "",
        episode: Optional[int] = None,
        episodes: int = 1,
        prior_summaries: str = "",
        revision_notes: str = "",
        character_profiles: str = "",
        on_event=None,
        abort: "Optional[threading.Event]" = None,
    ) -> str:
        _emit(on_event, type="note", message=f"正在拆解场景（stub）…", progress=10)
        _emit(on_event, type="note", message=f"正在写场景 1/1（stub）…", progress=60)
        tag = f"·附原著{len(novel_text)}字" if novel_text else ""
        req = f"·{','.join(style)}" if style else ""
        req += f"·{pacing}" if pacing else ""
        char = f"·人物小传{len(character_profiles)}字" if character_profiles else ""
        ep = f"·第{episode}/{episodes}集" if episodes and episodes > 1 else ""
        prior = f"\n{prior_summaries}" if prior_summaries else ""
        rev = f"\n·改：{revision_notes[:40]}" if revision_notes else ""
        _emit(on_event, type="note", message="本集草稿完成", progress=100)
        return f"[剧本·stub{tag}{req}{char}{ep}] {outline[:60]}{prior}{rev}"

    def review(self, text: str, criteria: str, novel_text: str = "", on_event=None,
                abort: "Optional[threading.Event]" = None) -> ReviewResult:
        _emit(on_event, type="note", message="审核专家正在逐项核验（stub）…", progress=60)
        if self.reject_once and not self._rejected:
            self._rejected = True
            _emit(on_event, type="note", message="已给出审核意见：打回", progress=100)
            return ReviewResult(approved=False, opinion="第二幕逻辑断裂，建议重写")
        _emit(on_event, type="note", message="已给出审核意见：通过", progress=100)
        return ReviewResult(approved=True, opinion="结构完整、无硬伤，通过")


class LLMExecutor:
    def _call_chat(
        self, system: str, user: str, *, max_tokens: int, on_event=None, stage: str = ""
    ) -> str:
        """统一的 LLM 调用封装：失败时记日志、上报进度 note，并抛出中文友好异常。

        避免 `chat()` 的原始 provider 异常（如「付费组无可用通道」）直接冒到前端；
        SSE 层（service._stream_step）会把异常转成 `{"type":"error","message":...}`
        事件，用户看到可读提示而非 Python 栈。
        """
        from deskmate.memory.llm import chat, llm_available

        if not llm_available():
            msg = "未配置 LLM provider 凭证，请在设置中添加 API Key 后重试"
            _emit(on_event, type="note", message=msg, progress=0)
            raise RuntimeError(msg)
        try:
            return chat(system=system, user=user, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 生成失败（%s）: %s", stage or "chat", exc)
            msg = f"生成失败：{exc}（可重试；若持续失败请检查 provider 凭证与配额）"
            _emit(on_event, type="note", message=msg, progress=0)
            raise RuntimeError(msg) from exc

    def draft_outline(
        self,
        concept: str,
        *,
        novel_text: str = "",
        style: Optional[list[str]] = None,
        pacing: str = "",
        revision_notes: str = "",
        character_profiles: str = "",
        on_event=None,
        abort: "Optional[threading.Event]" = None,
    ) -> str:
        from deskmate.memory.llm import llm_available

        if not llm_available():
            return ""
        # 中止检查（B4②）：客户端已断开则不启动生成，省 token
        if abort is not None and abort.is_set():
            return ""
        novel_block = (
            f"\n\n【原著参考（改编依据）】\n{novel_text}\n"
            "（改编硬约束：①不得改动原著人物的核心动机与性格弧光；"
            "②关键事件、真相与悬念（如死因疑点、冤案、血仇、阴谋）须保留原著设定；"
            "③剧情推进须由原著动机驱动，禁止用与原著动机无关的桥段"
            "（如偶遇、偷窃被抓、无端逃债）替换关键情节。）" if novel_text else ""
        )
        req = _requirement_block(style, pacing)
        rev = _revision_block(revision_notes)
        char_block = _character_block(character_profiles, for_outline=True)
        _emit(on_event, type="note", message="正在撰写大纲…", progress=30)
        text = self._call_chat(
            system=(
                "你是资深剧本大纲专家，从业二十年，专精结构与人物。按以下方法论工作：\n"
                "① 结构：用三幕/序列节拍——第一幕建立欲望与缺口并出现激励事件；"
                "第二幕制造升级的障碍与中点反转；第三幕收束并给出代价。每一幕必须有明确的"
                "转折点，禁止平铺直叙。\n"
                "② 人物：每个主要人物写清「欲望—缺陷—秘密—弧光终点」，其每个关键行动都要"
                "有动机支撑；不得为推进剧情让人物做违背性格的事。\n"
                "③ 冲突：每个情节节点必须含「目标｜障碍｜转折」，删掉无冲突、无功能的过场。\n"
                "④ 主题：用行动与代价表达主题，不用台词直说。\n"
                "输出格式：标题行；【人物表】（姓名/身份/欲望/缺陷/弧光）；"
                "分幕列出情节节点（每节点一行：事件｜冲突｜转折/结果）；结尾给结局与悬念钩子。\n"
                "原著改编：只换叙事手法，不换人物内核——核心动机、性格弧光、关键真相与悬念"
                "必须与原著一致，每个主要行为都能在原著找到动机依据。\n"
                "制作要求（节奏/风格）必须落到结构上（如快节奏=节点更密、更早给冲突）。\n"
                "若给出修改要求，逐条对照上一版的具体问题修订，不得回避。"
            ),
            user=concept + novel_block + req + rev + char_block,
            max_tokens=2000,
            on_event=on_event,
            stage="draft_outline",
        )
        _emit(on_event, type="note", message="大纲初稿完成", progress=100)
        return text

    def draft_screenplay(
        self,
        outline: str,
        *,
        novel_text: str = "",
        style: Optional[list[str]] = None,
        pacing: str = "",
        episode: Optional[int] = None,
        episodes: int = 1,
        prior_summaries: str = "",
        revision_notes: str = "",
        character_profiles: str = "",
        on_event=None,
        abort: "Optional[threading.Event]" = None,
    ) -> str:
        """分段续写拼接（阶段 5 输出侧）——单次 8k token 出不了长剧，工程拆：

        1) LLM 把（本集的）大纲拆成场景清单——分集模式下场景数限 1–3（单集容量），
           全本模式 3–6；
        2) 逐段生成（每段限额内），段间用「前情摘要」保连贯（不塞全文，token 不
           随段数线性放大）；
        3) 拼接返回，供 docx 落地 / 审核 / 圆桌终审。

        每个阶段切换点经 on_event 上报进度（SSE 流式：前端实时看到做到哪一步）。
        """
        from deskmate.memory.llm import llm_available

        if not llm_available():
            return ""
        novel_block = (
            f"\n\n【原著参考（改编依据，含改编圣经+相关章节）】\n{novel_text}\n"
            "（请基于原著精神展开剧本，保持人物与核心情节一致。）" if novel_text else ""
        )
        req = _requirement_block(style, pacing, episode, episodes)
        prior_block = (
            f"\n\n【前情摘要（前序集数已发生的故事，续写须衔接）】\n{prior_summaries}"
            if prior_summaries.strip()
            else ""
        )
        rev = _revision_block(revision_notes)
        char_block = _character_block(character_profiles, for_outline=False)
        is_episodic = bool(episodes and episodes > 1)
        # 1) 拆场景（拆不动则整段写）
        # 中止检查（B4②）：客户端断开则不拆场景、不生成，省 token
        if abort is not None and abort.is_set():
            return ""
        _emit(on_event, type="note", message="正在拆解场景…", progress=5)
        plan = self._call_chat(
            system=(
                "你是编剧统筹（script supervisor），负责把大纲拆成可拍的场景序列。\n"
                "拆解原则：每个场景必须有独立戏剧功能——推进情节、揭示人物、或制造张力，"
                "至少占其一；纯交代/纯过渡的场景一律合并或删除。\n"
                "每个场景按「地点｜谁的目标｜障碍｜转折/结果」自检后再写出。\n"
                "每行严格格式：序号.场景名|一句话目标。只输出清单，不要其他内容。"
                + ("这是单集（约 2-4 幕），场景数控制在 1-3 个，容量要适配一集，"
                   "结尾那个场景须承担下集钩子。" if is_episodic else "")
            ),
            user=f"大纲：\n{outline}" + (f"\n\n目标集数：第 {episode}/{episodes} 集" if is_episodic else ""),
            max_tokens=800,
            on_event=on_event,
            stage="split_scenes",
        )
        scenes = _parse_scenes(plan)
        if len(scenes) < 2:
            scenes = [("全剧", "按大纲完整撰写")]
        # 2) 逐段生成 + 前情摘要续写
        segments: list[str] = []
        prev_summary = "（这是第一段，尚未有前情）"
        base_user = f"{outline}{novel_block}{req}{prior_block}{rev}{char_block}"
        n = len(scenes)
        for i, (name, goal) in enumerate(scenes, 1):
            progress = 10 + int(88 * i / n)
            scene_label = (name or f"场景{i}")[:14]
            _emit(on_event, type="note", message=f"正在写场景 {i}/{n}《{scene_label}》…", progress=progress)
            user = (
                f"{base_user}\n\n"
                f"【当前场景 {i}/{n}】{name}\n"
                f"场景目标：{goal}\n"
                f"【前情摘要】{prev_summary}\n\n"
                f"请撰写本场景剧本正文：台词节奏、可拍性、人物行为与语气连贯，"
                f"并与前情自然衔接。"
                + (f"（这是第 {episode}/{episodes} 集，结尾须给下集留钩子。）" if is_episodic else "")
            )
            seg = self._call_chat(
                system=(
                    "你是职业编剧。按场景写作工艺落笔：\n"
                    "① 开场即入冲突或张力，不写铺垫式开场；场景中途必须有转折，"
                    "结尾留下新的问题或悬念。\n"
                    "② 展示而非讲述：只写可见动作、可听对白与环境细节；禁止心理描写、"
                    "禁止旁白解释人物在想什么。\n"
                    "③ 对白：每个角色有可辨识的说话方式（用词、句长、口头禅、回避什么）；"
                    "多用潜台词，让人物说出口的话与真实意图有落差。\n"
                    "④ 可拍性：动作具体到镜头能拍（谁、在哪、做什么、看见什么）。\n"
                    "⑤ 格式：场景标题行（场景/地点/日或夜/内或外）；动作段落；"
                    "角色名: 台词；转场。\n"
                    "每段只写当前场景，不重复前情、不预告后文。制作要求必须严格遵守。"
                ),
                user=user,
                max_tokens=3800,
                on_event=on_event,
                stage=f"scene_{i}",
            )
            if not seg or not seg.strip():
                logger.warning("场景 %s 生成为空，跳过", name)
                continue
            segments.append(seg.strip())
            prev_summary = _summarize_segment(seg)
            # 中止检查（B4②）：当前场景已写完，但后续场景不再生成，省 token。
            # 注意：单个在飞的 chat HTTP 请求无法中断（chat 层不暴露连接关闭），
            # 故只能在上一个场景返回后停下；这是 best-effort 止血。
            if abort is not None and abort.is_set():
                logger.info("创作被中止：已保留前 %d 个场景，停止后续生成", len(segments))
                break
        if not segments:
            return ""
        _emit(on_event, type="note", message="全稿拼接完成", progress=100)
        return "\n\n---\n\n".join(segments)

    def review(self, text: str, criteria: str, novel_text: str = "", on_event=None,
                abort: "Optional[threading.Event]" = None) -> ReviewResult:
        from deskmate.memory.llm import llm_available
        from deskmate.style_profile_store import format_style_fingerprint

        if not llm_available():
            return ReviewResult(approved=True, opinion="（LLM 未配置，默认通过）")
        # 中止检查（B4②）：客户端断开则不启动审核
        if abort is not None and abort.is_set():
            return ReviewResult(approved=True, opinion="（审核已中止）")
        fp = format_style_fingerprint()
        novel_block = (
            f"\n\n【原著参考（对照依据）】\n{novel_text}\n"
            "（请结合原著判断改编是否忠于核心人物与情节。）" if novel_text else ""
        )
        # C：文风对齐评判——把 P1-5 量化指纹作为审稿硬标准之一，
        # 让「越写越懂你的文风」在审稿环节也被校验（偏离须指出并建议修正）。
        style_section = ""
        if fp:
            style_section = (
                f"\n\n【文风对齐评判】请对照以下萧哥写作风格量化指纹，评审文本是否贴合其声口"
                f"（句长节奏、四字格密度、文白比例、对话占比、标点节奏）；"
                f"明显偏离处须指出并给出修正建议：\n{fp}"
            )
        _emit(on_event, type="note", message="审核专家正在逐项核验…", progress=50)
        raw = self._call_chat(
            system=(
                "你是剧本审核专家，兼资深剧本医生。按六个维度逐项评：\n"
                "① 结构：幕结构是否成立、转折点是否有效、有无无效场景；\n"
                "② 人物：动机是否一致、有无 OOC、弧光是否完成；\n"
                "③ 对白：是否可辨识、有无潜台词、信息密度是否过高或过低；\n"
                "④ 节奏：冲突密度、场景功能、拖沓或跳脱之处的具体位置；\n"
                "⑤ 改编忠实度（如给原著）：核心动机、关键真相与悬念是否保留；\n"
                "⑥ 文风对齐（如给量化指纹）：句长节奏、四字格密度、文白比例、对话占比。\n"
                "每条意见必须给：定位（引用原文片段）→ 问题 → 具体改法（改写成什么样），"
                "禁止只说「可以更好」这类空话。\n"
                "输出格式硬性要求：第一行必须是 `VERDICT: APPROVE`（通过定稿）"
                "或 `VERDICT: REJECT`（打回修改），其后是分维度意见。\n"
                "判定口径：只有存在会影响成稿质量的硬伤（结构断裂、人物 OOC、史实硬错、"
                "节奏崩坏、偏离验收标准、改编偏离原著内核）才 REJECT；"
                "「建议优化三处台词」这类弱建议是 APPROVE + 意见，不要打回。"
            ),
            user=f"验收标准：{criteria}{style_section}\n\n文本：\n{text}{novel_block}",
            max_tokens=6000,
            on_event=on_event,
            stage="review",
        )
        # 统一 verdict 解析：复用 final_review.parse_verdict（含否定式清洗），
        # 与终审裁决逻辑一致，避免两套判断对同份文本给出相反结论
        from .final_review import parse_verdict

        approved = parse_verdict(raw)
        _emit(on_event, type="note", message="已给出审核意见", progress=100)
        return ReviewResult(approved=approved, opinion=raw)
