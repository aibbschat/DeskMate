"""P1-3 桌宠日记产品化——小桔视角第一人称「今日小结」+ 每周回忆。

让「伙伴感」可感知：把每天 growth 信号（对话/创作/徽章/默契/偏好）转成
小桔第一人称的日记（``~/.deskmate/diary/YYYY-MM-DD.md``），并支持每周
「我们的回忆」汇总（对话数/创作进度/解锁徽章/学到偏好/默契阶段）。

- 独立于 memory 层（P0-6/P0-7 范围外）：写入 ``<base>/diary/``，不碰 memory manager。
- fail-open：有真实信号则如实记录；纯空转只写一句安静的陪伴，绝不编造。
- **一天可以写多则**（2026-09-02 萧哥定调：不要限死每天一条）。素材攒够了就
  ``append_entry`` 追加一则（``## HH:MM`` 小节），绝不覆盖当天已写的内容——
  覆盖会让「今天」永远停在第一则落笔的那一刻，后面的事永远进不来。

## 关于「零 LLM」（2026-09-03 修订）

本模块**渲染事实的部分仍然是零 LLM**：技能名、次数、时间戳、默契数值、
徽章，全部由规则生成，模型一个字都不碰——这是「客观档案不许编」的底线。

但**把这些事实讲成人话**是另一回事，交给 ``diary_narrator.py``（L3 可选层）。
原因是萧哥点破的一点：

> 「技能又精进了」这类话，技能日志里全都有，日记再说一遍是废话。
> 日记该写的是**因为什么事**精进的。

只有规则模板时，日记只能是台账（``- 「novel-review」这项技能又精进了``）。
润色层拿到的是同样的事实，只是把它组织成有因果、有温度的叙述；
**拿不到润色就照旧列清单**，降级是常态而非事故。润色层自带三道闸门
（无 provider 不调 / 素材没内容不调 / 结果异常丢弃），且情绪表达必须有
可观测的信号锚点，不许凭空抒情。

关闭润色（退回纯规则）：环境变量 ``DESKMATE_DIARY_NARRATE=0``。
"""

from __future__ import annotations

import datetime
import hashlib
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Optional

from deskmate.growth.engine import GrowthEngine

# 事件 kind → 第一人称描述（无匹配时 fallback 到 kind 本身）
_EVENT_LABELS = {
    "chat": "你和我聊了天",
    "creation_approved": "你拍板定稿了一份创作",
    "creation_rework": "有一稿被你打回重做了",
    "suggestion_accepted": "你采纳了我的一条提醒",
    "suggestion_ignored": "有一条提醒你没有理会",
    "user_corrected": "你纠正了我一次（我会记着校准）",
    "memory_learned": "我学到了一些关于你的新事",
    "writing": "我们一起整理了文稿",
    "roundtable": "我们开了一次圆桌讨论",
    "report": "你让我出了一份报告",
    "skill_use": "你调用了一项技能",
    "skill_used": "你调用了一项技能",
    "skill_created": "我们一起建了个新技能",
    "skill_evolved": "一项技能又精进了",
}

# 默认事件描述（兜底）
_FALLBACK_LABEL = "今天发生了一点小事"

# 同类事件多于等于这个数就合并成一句，不再逐条排开（详见 _render_entry）
_MERGE_AT = 3


def _evt_date(ts: str) -> str:
    """UTC ISO 时间 → 本地日期 YYYY-MM-DD。"""
    try:
        dt = datetime.datetime.fromisoformat(ts)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.date().isoformat()
    except (ValueError, TypeError):
        return ""


def _today_local() -> str:
    return datetime.date.today().isoformat()


def _weekday_cn(date_iso: str) -> str:
    try:
        return "一二三四五六日"[
            datetime.date.fromisoformat(date_iso).weekday()
        ]
    except ValueError:
        return ""


def _now_hhmm() -> str:
    """当前本地时间 HH:MM（一则日记的小标题）。"""
    return datetime.datetime.now().strftime("%H:%M")


# 一则以 ``## HH:MM`` 开头——据此数当天写了几则
_ENTRY_HEAD_RE = re.compile(r"^##\s", re.MULTILINE)

# 每则日记的稳定身份（entry id）。写入时 embed 进 ``## HH:MM`` 标题行，
# 形如 ``<!--xj:2026-09-03-163012-a1b2c3-->``。留言/🩷 都按这个 id 走，
# 而不是按"当天第几则"的位置——否则删掉中间一则，后面补位后留言就串到
# 别人头上（这是早期设计的结构性缺陷，2026-09-03 起根治）。
_EID_RE = re.compile(r"<!--xj:([0-9A-Za-z-]+)-->")


def _gen_eid(date: str, at: str) -> str:
    """为新建的一则生成全局稳定 id（UUID，写入即固定，不随位置变）。"""
    return f"{date}-{at.replace(':', '')}-{uuid.uuid4().hex[:6]}"


def backfill_diary_eids(diary_dir: Path) -> int:
    """给历史日记每则补上稳定 eid，幂等。新写入的则已由 append_entry 自带，
    本函数只补改造前写的老文件。eid 用 ``日期-HHMM-同文件序号``，确定性、
    可重复，跑多次结果一致。

    为何要在后端落 eid 而非前端现造：日记唯一存储是单篇 markdown，没有
    每则身份。把身份写进文件、跟着这则走，才是"文章 ID 跟着内容"的模型。
    """
    if not diary_dir.is_dir():
        return 0
    changed = 0
    for md in sorted(diary_dir.glob("*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.split("\n")
        out: list[str] = []
        idx = 0
        modified = False
        for line in lines:
            m = re.match(r"^##\s+(.*)$", line)
            if m and "<!--xj:" not in line:
                idx += 1
                head = m.group(1).strip()
                tmatch = re.match(r"(\d{1,2}):(\d{2})", head)
                hhmm = tmatch.group(0).replace(":", "") if tmatch else "0000"
                eid = f"{md.stem}-{hhmm}-{idx:02d}"
                out.append(f"## {head} <!--xj:{eid}-->")
                modified = True
                changed += 1
            else:
                out.append(line)
        if modified:
            tmp = md.with_suffix(md.suffix + ".tmp")
            try:
                tmp.write_text("\n".join(out), encoding="utf-8")
                os.replace(tmp, md)
            except OSError:
                pass
    return changed


class DiaryWriter:
    """小桔日记：第一人称今日小结 + 每周回忆。"""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        base = Path(base_dir) if base_dir else Path.home() / ".deskmate"
        self.base = base
        self.diary_dir = base / "diary"
        # 给历史日记每则补上稳定 eid（幂等，只补没有的）。新写入的则自带。
        try:
            backfill_diary_eids(self.diary_dir)
        except Exception:
            pass
        self.engine = GrowthEngine(base_dir=base)

    # ------------------------------------------------------------------
    # 信号采集
    # ------------------------------------------------------------------
    def _events_on(self, date: str) -> list[dict]:
        """当天的事件列表（按时间正序）。"""
        out = [
            e for e in self.engine.state.recent_events
            if isinstance(e, dict) and _evt_date(str(e.get("ts", ""))) == date
        ]
        return sorted(out, key=lambda x: str(x.get("ts", "")))

    def _badges_on(self, date: str) -> list[dict]:
        """当天解锁的徽章（badges_unlocked 时间戳转本地日期匹配）。"""
        out = []
        for badge in self.engine.badge_items():
            ts = badge.get("unlockedAt") or badge.get("unlocked_at")
            if ts and _evt_date(str(ts)) == date:
                out.append({"id": badge.get("id", ""), "name": badge.get("name", "")})
        return out

    def _top_domain(self) -> tuple[str, float] | None:
        d = self.engine.current_empathy_domains()
        top, val = d.get("top") or "", d.get("top_value") or 0.0
        return (top, val) if top and val > 0 else None

    def _state_summary(self) -> dict:
        st = self.engine.state
        return {
            "intimacy": round(float(getattr(st, "intimacy", 0.0) or 0.0), 1),
            "trust": round(float(getattr(st, "trust", 0.5) or 0.5), 2),
            "mood": round(float(getattr(st, "mood", 80.0) or 80.0), 1),
            "level": int(getattr(st, "level", 1) or 1),
            "total_chats": int(getattr(st, "total_chats", 0) or 0),
        }

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def _describe_event(self, e: dict) -> str | None:
        """把单个事件转成「有实事」的第一人称描述；没有可说的具体信息返回 None。

        关键是挖出事件里携带的具体信息（skill_name / note / count），
        而不是只输出 kind 的固定标签——那正是早期「流水账」的病根。
        """
        kind = str(e.get("kind", ""))
        if kind in ("skill_used", "skill_evolved"):
            name = str(e.get("skill_name", "") or "").strip()
            # reason = 这次进化「因为什么」（调用方手里的用户原话）。
            # 光有技能名只能写出台账——技能日志里早记过了，日记要讲因果。
            reason = str(e.get("reason", "") or "").strip()
            if kind == "skill_used":
                if name and reason:
                    return f"你调用了「{name}」技能（{reason}）。"
                if name:
                    return f"你调用了「{name}」技能，我们配合了一把。"
                return "你调用了一项技能，我们配合了一把。"
            if name and reason:
                return f"「{name}」这项技能因为「{reason}」又长进了一点。"
            if name:
                return f"「{name}」这项技能又精进了。"
            return "一项技能又精进了。"
        if kind == "memory_learned":
            return None  # 这类按天合并计数，由 _render_diary 统一处理
        if kind == "user_corrected":
            note = str(e.get("note", "") or "").strip()
            # note 现在是用户的纠正原话（engine.record_correction(note=last_user)）。
            # 写原话才叫日记；写「你纠正了我一次」只是计数。
            if note and note != "用户明确纠正":
                return f"你纠正了我：「{note}」"
            return "你纠正了我一次——我会记着校准。"
        if kind == "creation_approved":
            note = str(e.get("note", "") or "").strip()
            return f"你拍板定稿了一份创作{f'（{note}）' if note else ''}。"
        if kind == "creation_rework":
            note = str(e.get("note", "") or "").strip()
            return f"有一稿被你打回重做了{f'（{note}）' if note else ''}。"
        if kind == "suggestion_accepted":
            return "你采纳了我的一条提醒——我也越来越懂你该听什么。"
        if kind == "writing":
            return "我们一起整理了文稿。"
        if kind == "roundtable":
            return "我们开了一次圆桌讨论。"
        if kind == "report":
            return "你让我出了一份报告。"
        label = _EVENT_LABELS.get(kind)
        return f"{label}。" if label else None

    def _entry_time(self, events: list[dict]) -> str:
        """一则的时间戳：取素材里**最晚那条事件**的时间。

        用事件时间而非落笔时间——这一则讲的是那一刻发生的事，
        而不是"系统什么时候想起来写的"。拿不到事件时间才退回当前时间。
        """
        latest = ""
        for e in events:
            ts = str(e.get("ts", "") or "")
            if ts > latest:
                latest = ts
        if latest:
            try:
                dt = datetime.datetime.fromisoformat(latest)
                if dt.tzinfo is not None:
                    dt = dt.astimezone()
                return dt.strftime("%H:%M")
            except ValueError:
                pass
        return _now_hhmm()

    def _opening(self, seq: int, at: str = "") -> str:
        """一则的开场白：第一则与后续补记语气不同（后记不该又来一遍"今天"）。"""
        if seq <= 1:
            return "今天我们一起做了些事，我都记在心里。"
        try:
            hour = int(str(at).split(":", 1)[0])
        except (ValueError, IndexError):
            hour = datetime.datetime.now().hour
        if hour < 12:
            return "上午过后又有了些新动静，我接着记。"
        if hour < 18:
            return "下午又有了些新动静，我接着记。"
        return "入夜了又添了几笔，我接着记。"

    @staticmethod
    def _narrate(events: list[dict], seq: int, at: str) -> Optional[str]:
        """叙事润色（L3 可选层），失败返回 None 由调用方回退规则渲染。

        三道闸门与「情绪必须有锚点」的设计见 ``diary_narrator.py`` 模块文档。
        这里只做一件事：给一个总开关，出问题能一键退回纯规则。
        """
        import os

        if os.environ.get("DESKMATE_DIARY_NARRATE", "1").strip() in (
            "0", "false", "no", "off",
        ):
            return None
        try:
            from deskmate.growth.diary_narrator import narrate

            return narrate(events, seq=seq, at=at)
        except Exception:
            return None

    def _render_entry(self, events: list[dict], badges: list[dict],
                      top: tuple[str, float] | None, st: dict,
                      seq: int, at: str, eid: str = "") -> str:
        """渲染**一则**日记（不含文件级标题，由 _append_text 负责）。

        ``eid`` 是这则的稳定身份，写入标题行（``<!--xj:...-->``），跟着这则走。
        """
        lines: list[str] = [f"## {at} <!--xj:{eid}-->" if eid else f"## {at}"]

        if events:
            # 收集实事：具体事件逐条，重复类合并计数
            facts: list[str] = []
            memory_cnt = sum(
                int(e.get("count", 1) or 1)
                for e in events if e.get("kind") == "memory_learned"
            )
            corrected = sum(1 for e in events if e.get("kind") == "user_corrected")

            # 技能类事件一天里常常成串（进化/调用好几项）。逐条排开就成了
            # 「「A」这项技能又精进了」×5 的流水账——素材是真的，读着却像报表。
            # 3 条以上合并成一句列举；少于 3 条保留逐条，那时候细节更生动。
            def _skill_names(kind: str) -> list[str]:
                out: list[str] = []
                for e in sorted(events, key=lambda x: str(x.get("ts", ""))):
                    if e.get("kind") == kind:
                        n = str(e.get("skill_name", "") or "").strip()
                        if n and n not in out:
                            out.append(n)
                return out

            evolved = _skill_names("skill_evolved")
            used = _skill_names("skill_used")
            merge_evolved = len(evolved) >= _MERGE_AT
            merge_used = len(used) >= _MERGE_AT

            for e in sorted(events, key=lambda x: str(x.get("ts", ""))):
                k = e.get("kind")
                if (k == "skill_evolved" and merge_evolved) or (
                    k == "skill_used" and merge_used
                ):
                    continue  # 交给下面的合并句，不重复列
                desc = self._describe_event(e)
                if desc and desc not in facts:
                    facts.append(desc)

            if merge_evolved:
                head = "、".join(f"「{n}」" for n in evolved[:3])
                facts.append(f"{head}……今天一口气精进了 {len(evolved)} 项技能。")
            if merge_used:
                head = "、".join(f"「{n}」" for n in used[:3])
                facts.append(
                    f"你调用了 {head} 等 {len(used)} 项技能，我都跟上了。"
                )
            if memory_cnt:
                # texts = 这几次记住的事实原文（engine.record_memory_learned(texts=)）。
                # 有原文就照实列出来；「又记住了你 4 件小事」这种只在没原文时兜底。
                learned_texts: list[str] = []
                for e in sorted(events, key=lambda x: str(x.get("ts", ""))):
                    if e.get("kind") != "memory_learned":
                        continue
                    for t in (e.get("texts") or []):
                        t = str(t or "").strip()
                        if t and t not in learned_texts:
                            learned_texts.append(t)
                if learned_texts:
                    shown = "；".join(learned_texts[:3])
                    more = (
                        f"（还有 {len(learned_texts) - 3} 条）"
                        if len(learned_texts) > 3
                        else ""
                    )
                    facts.append(f"我又记住了你的一些事：{shown}{more}。")
                else:
                    facts.append(f"我又记住了你 {memory_cnt} 件小事。")
            if corrected > 1:
                facts = [f for f in facts if not f.startswith("你纠正了我")]
                facts.append(f"你纠正了我 {corrected} 次——我会记着校准。")

            # 叙事润色（L3，可选增强）：把上面这些实事讲成一段有人味的话。
            # 拿到就用它，拿不到（无 provider / 素材没内容 / 模型认输 / 异常）
            # 就照旧列清单——降级是常态，不是事故。
            narrated = self._narrate(events, seq=seq, at=at)
            if narrated:
                lines.append(narrated)
            else:
                # 补记（seq>1）不再重复"今天"，语气改为接续
                lines.append(self._opening(seq, at))
                for f in facts[:6]:
                    lines.append(f"- {f}")
        else:
            lines.append("这一阵没什么动静。我安静地待着，等你忙完。")

        # 默契状态（阶段名暂不展示：bond_depth 由封顶溢出驱动，数值未触顶时
        # 会显示"初识"，与高默契矛盾，待 8.2 长跑完善后再接）
        lines.append(
            f"- 默契 {st['intimacy']}/100 · 信任 {st['trust']} · 心情 {st['mood']} · Lv.{st['level']}"
        )
        if badges:
            names = "、".join(b["name"] for b in badges)
            lines.append(f"- 今天解锁徽章：「{names}」。")
        if top and seq <= 1:
            # 「更懂你了」说一次就够，补记里再来一遍就成了自我表扬
            lines.append(f"- 我更懂你了：你在「{top[0]}」上最投入。")
        if seq <= 1:
            # 落款只在第一则——每则都挂一遍会很啰嗦
            lines.append("")
            lines.append(
                f"> 累计对话 {st['total_chats']} 轮。这份日记由今日真实信号生成，"
                f"没有的我不编。"
            )
        return "\n".join(lines)

    def _render_recap(self, days: int) -> str:
        today = _today_local()
        start = (
            datetime.date.today() - datetime.timedelta(days=days - 1)
        ).isoformat()
        dates = []
        for d in self.diary_dir.glob("*.md"):
            if start <= d.stem <= today:
                dates.append(d.stem)
        dates.sort()
        if not dates:
            return "（近几天还没有日记——多和我聊两句，回忆就会长出来。）"

        st = self.engine.state
        counts_all: dict[str, int] = {}
        for e in self.engine.state.recent_events:
            if isinstance(e, dict):
                d = _evt_date(str(e.get("ts", "")))
                if start <= d <= today:
                    k = str(e.get("kind", ""))
                    counts_all[k] = counts_all.get(k, 0) + 1
        badges = self.engine.badge_items()
        unlocked = [b.get("name", "") for b in badges if b.get("unlocked_at")]
        top = self._top_domain()

        lines = [f"# 我们的回忆 · 最近 {len(dates)} 天\n"]
        lines.append(f"这 {len(dates)} 天，我们一共聊了 {st.total_chats} 轮（累计），")
        bits = []
        if counts_all.get("creation_approved"):
            bits.append(f"定稿创作 {counts_all['creation_approved']} 份")
        if counts_all.get("suggestion_accepted"):
            bits.append(f"你采纳提醒 {counts_all['suggestion_accepted']} 次")
        if counts_all.get("writing"):
            bits.append(f"整理文稿 {counts_all['writing']} 次")
        lines.append("，".join(bits) + "。" if bits else "日子平平常常。")
        lines.append(
            f"- 默契 {round(st.intimacy, 1)}/100 · 信任 {round(st.trust, 2)} · Lv.{st.level}"
        )
        if unlocked:
            lines.append(f"- 已解锁徽章：{'、'.join(unlocked)}")
        if top:
            lines.append(f"- 我越来越懂你：你最常投入的是「{top[0]}」")
        lines.append("\n> 记忆是把我们一起走过的日子，慢慢攒成默契。")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def _document_header(self, date: str) -> str:
        return f"# {date} 周{_weekday_cn(date)} · 小桔的日记"

    def entry_count(self, date: str) -> int:
        """当天已写了几则（数文件里 ``## `` 小节）。

        兼容旧格式：改造前的日记是整篇一段、没有小节标题。判定办法是看
        **第一个 ``##`` 之前有没有正文**——有就说明那是一则旧格式的日记，
        要算进去，否则追加第二则时序号会被算成第一则。
        """
        p = self.diary_dir / f"{date}.md"
        if not p.is_file():
            return 0
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            return 0
        if not text.strip():
            return 0
        n = len(_ENTRY_HEAD_RE.findall(text))
        m = _ENTRY_HEAD_RE.search(text)
        head = text[: m.start()] if m else text
        legacy_blob = bool(
            "\n".join(
                ln for ln in head.splitlines()
                if ln.strip() and not ln.lstrip().startswith("# ")
            ).strip()
        )
        return n + (1 if legacy_blob else 0)

    def append_entry(self, date: str, events: list[dict],
                     badges: list[dict], at: str | None = None) -> dict:
        """在当天日记里**追加**一则，返回 ``{date, path, seq, at}``。

        一天可以写多则：素材攒够了就再记一笔（``diary_inspiration`` 判定），
        而不是把早上那一篇覆盖掉——覆盖会让后续的真实事件永远进不了日记。

        Args:
            events: 这一则要写的素材（调用方负责只传**没写过**的新素材，
                否则第二则会把第一则的内容复述一遍）
            badges: 当天徽章，只在第一则记（后续补记再列一遍没意义）
        """
        top = self._top_domain()
        st = self._state_summary()
        seq = self.entry_count(date) + 1
        at = at or self._entry_time(events)
        eid = _gen_eid(date, at)
        body = self._render_entry(events, badges, top, st, seq=seq, at=at, eid=eid)
        path = self._append_text(date, body)
        return {"date": date, "path": path, "seq": seq, "at": at}

    def _append_text(self, date: str, body: str) -> str:
        """把一则正文追加到当天文件；文件不存在时先写文档标题。

        原子落盘：临时文件 + os.replace，避免读到写了一半的文件。
        """
        self.diary_dir.mkdir(parents=True, exist_ok=True)
        path = self.diary_dir / f"{date}.md"
        header = self._document_header(date)
        try:
            old = path.read_text(encoding="utf-8").rstrip() if path.is_file() else ""
        except OSError:
            old = ""
        if old:
            content = f"{old}\n\n{body}\n"
        else:
            content = f"{header}\n\n{body}\n"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
        return str(path)

    def write(self, date: str, events: list[dict], badges: list[dict]) -> str:
        """写入（追加）一则日记，返回路径。兼容入口，逻辑见 ``append_entry``。"""
        return self.append_entry(date, events, badges)["path"]

    def ensure_today(self) -> str:
        """用户主动点「生成今日日记」时的入口。返回日记路径。

        与自动触发的区别：用户点了就是明确意愿，素材不多也照写第一则。
        当天已经有日记时，若还有**没写进去**的新素材，就再补一则——
        一天写几则由素材决定，不设上限。

        ⚠ 手动写完必须把素材标记成「已写进第 N 则」（consume + mark_entry）。
        否则自动路径（``maybe_write_diary``）看这些素材还是未消费，会把同一批
        素材再写一则，日记里出现两遍一模一样的话。

        自动触发一律走 ``diary_inspiration.maybe_write_diary``（素材攒够才动笔）。
        """
        today = _today_local()
        try:
            from deskmate.growth.diary_inspiration import (
                DiaryInspiration,
                score_items,
            )

            insp = DiaryInspiration(base_dir=self.base)
            fresh = insp.pending(today)
            if fresh:
                # 徽章只在第一则记，补记不再重复
                badges = self._badges_on(today) if self.entry_count(today) == 0 else []
                res = self.append_entry(today, fresh, badges)
                insp.ledger.consume(today, fresh, res["seq"])
                insp.ledger.mark_entry(today, res["seq"], score_items(fresh)[0])
                return res["path"]
        except Exception:
            pass  # 拿不到素材账本就走兜底，绝不因为补记失败而不写日记

        path = self.diary_dir / f"{today}.md"
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            return str(path)  # 已经写过、又没有新素材可补——不重复写
        events = self._events_on(today)
        badges = self._badges_on(today)
        return self.write(today, events, badges)

    def weekly_recap(self, days: int = 7) -> str:
        return self._render_recap(max(1, days))

    def list_diaries(self) -> list[str]:
        return sorted(p.stem for p in self.diary_dir.glob("*.md")) if self.diary_dir.is_dir() else []

    def read(self, date: str) -> str | None:
        p = self.diary_dir / f"{date}.md"
        return p.read_text(encoding="utf-8") if p.is_file() else None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    w = DiaryWriter()
    cmd = argv[0] if argv else "today"
    if cmd == "today":
        print(w.ensure_today())
    elif cmd == "weekly":
        days = int(argv[1]) if len(argv) > 1 else 7
        print(w.weekly_recap(days))
    elif cmd == "list":
        for d in w.list_diaries():
            print(d)
    elif cmd == "read" and len(argv) > 1:
        print(w.read(argv[1]) or f"（{argv[1]} 无日记）")
    else:
        print("用法: python -m deskmate.growth.diary [today|weekly [天]|list|read YYYY-MM-DD]")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
