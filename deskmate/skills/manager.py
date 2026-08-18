"""SkillManager — 技能生命周期管理。

职责：
- 技能索引生成：build_skill_index() 输出 <available_skills> 块，注入系统 prompt
- 技能匹配：match_skills() 按用户消息搜索匹配技能
- 技能进化：evolve_skill_on_correction() 用户纠正时自动更新技能

注意：技能创建和加载由 LLM 自觉触发（非引擎强制）：
- 加载：LLM 看到匹配任务时自觉调用 skill_view
- 创建：LLM 完成复杂任务后主动提议调用 skill_create
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from ..memory.llm import llm_available, chat, chat_memory, memory_llm_available
from .skill_store import SkillStore, DEFAULT_SKILLS_DIR


class SkillManager:
    """技能管理器，负责技能的自动创建、匹配和进化。"""

    def __init__(self, skills_dir: Path | str | None = None, base_dir: Path | str | None = None):
        self.store = SkillStore(skills_dir=skills_dir)
        self.base_dir = Path(base_dir) if base_dir else Path(
            os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
        )

    # ------------------------------------------------------------------
    # 技能索引（注入系统 prompt）
    # ------------------------------------------------------------------

    def build_skill_index(self) -> str:
        """生成技能索引块，注入系统 prompt。

        格式参考 Hermes 的 <available_skills>：
        - 按分类分组
        - 每行：技能名 + 简短描述 + 触发条件
        - 无技能时返回空串

        LLM 看到匹配任务时自觉调用 skill_view(name) 加载完整技能。
        """
        skills = self.store.list_skills()
        if not skills:
            return ""

        by_cat: dict[str, list[dict]] = {}
        for s in skills:
            cat = s.get("category", "general")
            if cat not in by_cat:
                by_cat[cat] = []
            by_cat[cat].append(s)

        # 技能熟练度徽标：<熟练> / <入门> / <生疏>
        prof_map = self._proficiency_map()

        lines = ["<available_skills>"]
        # 已知自研分类优先排序，导入社区包的分类（creative/research/note-taking…）
        # 也一并纳入，否则会被排除出系统 prompt 索引导致匹配不到。
        known_order = ["writing", "coding", "office", "general"]
        cats = [c for c in known_order if c in by_cat] + sorted(
            k for k in by_cat if k not in known_order
        )
        for cat in cats:
            lines.append(f"  {cat}:")
            for s in by_cat[cat]:
                desc = s.get("description", "")[:80]
                trigger = s.get("trigger", "")
                entry = f"    - {s['name']}: {desc}"
                badge = self._proficiency_badge(prof_map.get(s["name"]))
                if badge:
                    entry += f"（{badge}）"
                if trigger:
                    entry += f"（触发：{trigger}）"
                # 导入的社区技能包：标注作者/版本，便于识别来源
                if s.get("imported"):
                    extra = []
                    if s.get("author"):
                        extra.append(f"作者 {s['author']}")
                    if s.get("version"):
                        extra.append(f"v{s['version']}")
                    if extra:
                        entry += f"（{'/'.join(extra)}）"
                # 截断到 120 字符，保持索引紧凑
                if len(entry) > 120:
                    entry = entry[:117] + "..."
                lines.append(entry)
        lines.append("</available_skills>")

        return "\n".join(lines)

    def _proficiency_map(self) -> dict[str, float]:
        """读取技能熟练度映射（name → proficiency 0-1）。失败返回空 dict。"""
        try:
            from deskmate.growth.engine import GrowthEngine

            engine = GrowthEngine(base_dir=self.base_dir)
            return {
                name: p.proficiency
                for name, p in engine.state.skill_proficiency.items()
            }
        except Exception as e:
            logger.debug("growth engine proficiency map failed: %s", e)
            return {}

    @staticmethod
    def _proficiency_badge(prof: float | None) -> str:
        """熟练度徽标：0-1 → 文本徽标。"""
        if prof is None or prof <= 0:
            return ""
        if prof >= 0.7:
            return "熟练"
        if prof >= 0.35:
            return "入门"
        return "生疏"

    # ------------------------------------------------------------------
    # 自动匹配（每轮对话前）
    # ------------------------------------------------------------------

    def match_skills(self, user_message: str, limit: int = 3) -> list[dict]:
        """根据用户消息匹配相关技能。

        策略：
        1. 先用关键词搜索（score >= 1）
        2. 如果结果不够，用 LLM 语义匹配（口语化表达也能命中）
        """
        skills = self.store.search(user_message, limit=limit)
        # 过滤掉相关度太低的（score < 1）
        results = [s for s in skills if s.get("score", 0) >= 1]

        # 如果结果不足，尝试 LLM 语义匹配
        if len(results) < limit and llm_available():
            all_skills = self.store.list_skills(limit=30)
            if all_skills:
                semantic_matches = self._semantic_match(user_message, all_skills, limit=limit)
                # 合并，去重
                existing_names = {s["name"] for s in results}
                for m in semantic_matches:
                    if m["name"] not in existing_names:
                        results.append(m)
                        existing_names.add(m["name"])
                results.sort(key=lambda s: s.get("score", 0), reverse=True)

        # 熟练度加权：熟练的技能更容易被选中（越用越顺手）
        results = self._apply_proficiency_ranking(results)

        return results[:limit]

    def _apply_proficiency_ranking(self, results: list[dict]) -> list[dict]:
        """按技能熟练度对匹配结果重新加权排序。

        熟练度 (proficiency) 由 GrowthEngine 从使用次数/成功率/时效性派生，
        加权 ±20%：熟练的技能分数上浮，生疏的技能分数下浮。
        """
        if not results:
            return results
        try:
            from deskmate.growth.engine import GrowthEngine

            engine = GrowthEngine(base_dir=self.base_dir)
            for r in results:
                r["score"] = engine.modulate_skill_ranking(
                    float(r.get("score", 0)), r["name"]
                )
                r["proficiency"] = round(engine.get_proficiency(r["name"]), 3)
            results.sort(key=lambda s: s.get("score", 0), reverse=True)
        except Exception as e:
            logger.debug("growth engine proficiency ranking failed: %s", e)
        return results

    def _semantic_match(self, user_message: str, skills: list[dict], limit: int = 3) -> list[dict]:
        """用 LLM 做口语化语义匹配。

        解决"帮我看看这一章有没有问题"无法命中"审校小说"技能的问题。
        """
        if not skills:
            return []

        # 构造技能摘要列表
        skill_summary = "\n".join([
            f"- {s['name']}: {s.get('description', '')[:60]} | 触发: {s.get('trigger', '无')}"
            for s in skills[:20]
        ])

        prompt = f"""根据用户的请求，判断应该使用哪个技能。用户的表达可能是口语化的。

用户请求：{user_message[:200]}

可用技能：
{skill_summary}

规则：
- 如果用户的请求与某个技能的功能相关（即使措辞完全不同），返回那个技能的 name
- 如果没有匹配的技能，返回空字符串
- 只返回技能名或空字符串，不要解释"""

        try:
            resp = chat(
                "你是技能匹配器。只返回技能名或空字符串，不要解释。",
                prompt,
                temperature=0.0,
                max_tokens=30,
            )
            name = resp.strip()
            if name:
                # 找到对应技能
                for s in skills:
                    if s["name"] == name:
                        return [{**s, "score": 5}]  # LLM 匹配给高分
        except Exception as e:
            logger.debug("LLM semantic skill match failed: %s", e)
        return []

    def format_skills_context(self, user_message: str, max_chars: int = 2000) -> str:
        """匹配技能并格式化为上下文注入块。

        Returns:
            <skills-context> 包裹的技能内容，无匹配技能时返回空串。
        """
        skills = self.match_skills(user_message)
        if not skills:
            return ""

        parts = []
        for s in skills:
            # 读取完整技能内容
            full = self.store.read_skill(s["path"])
            if not full:
                continue
            # 只取技能的核心步骤（去掉 frontmatter 已经在 meta 里）
            body = full.get("body", "").strip()
            # 截断过长的技能
            if len(body) > 600:
                # 保留标题 + 前 500 字
                lines = body.split("\n")
                header = ""
                content_lines = []
                for line in lines:
                    if line.startswith("#") and not header:
                        header = line
                    else:
                        content_lines.append(line)
                body = (header + "\n" if header else "") + "\n".join(content_lines)[:500] + "\n…"

            # 记录技能被使用
            self.store.record_use(s["name"], context=user_message[:100], success=True)
            self.store.increment_uses(s["path"])
            # 成长系统：技能使用事件（熟练度 + XP + 亲密度）
            self._record_growth_skill_use(s["name"], s["path"])

            parts.append(
                f"### 技能：{s['name']}\n"
                f"**触发条件**：{s.get('trigger', '无')}\n"
                f"**使用方法**：\n{body}"
            )

        combined = "\n\n---\n\n".join(parts)
        if len(combined) > max_chars:
            combined = combined[:max_chars] + "\n…"

        return f"<skills-context>\n{combined}\n</skills-context>"

    def _record_growth_skill_use(self, name: str, path: str) -> None:
        """向成长系统上报技能使用事件（熟练度 + XP + 亲密度）。"""
        try:
            from deskmate.growth.engine import GrowthEngine

            full = self.store.read_skill(path)
            uses = full["meta"].get("uses", 0) if full else 1
            rate = full["meta"].get("success_rate", 0.0) if full else 0.0
            engine = GrowthEngine(base_dir=self.base_dir)
            engine.record_skill_use(name, success=True, skill_store_uses=uses, skill_store_rate=rate)
        except Exception as e:
            logger.debug("growth engine skill_use record failed: %s", e)

    def _record_growth_skill_evolved(self, name: str, path: str) -> None:
        """向成长系统上报技能进化事件。"""
        try:
            from deskmate.growth.engine import GrowthEngine

            full = self.store.read_skill(path)
            uses = full["meta"].get("uses", 0) if full else 1
            rate = full["meta"].get("success_rate", 0.0) if full else 0.0
            engine = GrowthEngine(base_dir=self.base_dir)
            engine.record_skill_evolved(name, skill_store_uses=uses, skill_store_rate=rate)
        except Exception as e:
            logger.debug("growth engine skill_evolved record failed: %s", e)

    # ------------------------------------------------------------------
    # 自动抽取（对话结束后）
    # ------------------------------------------------------------------

    def extract_skill_from_session(
        self, user_msg: str, assistant_msg: str, conversation_history: str = ""
    ) -> dict | None:
        """从对话中判断是否产生了可复用的技能。

        使用 LLM 分析对话，判断：
        1. 是否完成了一个复杂任务（多步骤、有工具调用）
        2. 是否有可复用的流程或方法
        3. 是否值得保存为技能

        Returns:
            新创建的技能信息，或 None（无需创建）
        """
        if not memory_llm_available():
            return None

        # 获取已有技能摘要，供 LLM 判断去重
        existing_skills = self.store.list_skills(limit=50)
        existing_summary = ""
        if existing_skills:
            existing_summary = "\n已有技能（请勿创建重复技能，可考虑更新已有技能）：\n"
            for s in existing_skills[:30]:
                existing_summary += f"- {s['name']}: {s.get('description', '')[:60]}\n"

        prompt = (
            "你是技能抽取器。分析以下对话，判断是否产生了可复用的工作流程，"
            "如果是，提取为技能。\n\n"
            "判断标准：\n"
            "1. 对话中完成了一个复杂任务（多步骤、有工具调用、有明确产出）\n"
            "2. 过程中有值得记录的方法、步骤或踩坑经验\n"
            "3. 这个任务未来可能重复出现\n"
            "4. 与已有技能不重复（如果已有技能可以覆盖，应输出 should_create: false）\n\n"
            f"{existing_summary}\n"
            "如果符合条件，输出 JSON：\n"
            '{"should_create": true, "name": "技能名", "description": "简短描述", '
            '"category": "writing/coding/office/general", '
            '"tags": ["标签1", "标签2"], '
            '"trigger": "触发条件（用户说什么时该加载此技能）", '
            '"body": "技能正文（Markdown格式，包含步骤和踩坑记录）"}\n\n'
            "如果不符合条件（包括与已有技能重复），输出：\n"
            '{"should_create": false}\n\n'
            "只输出 JSON，不要解释。\n\n---\n\n"
            "对话记录：\n"
        )

        if conversation_history:
            prompt += conversation_history[-3000:]
        else:
            prompt += f"用户：{user_msg[:1500]}\n\n助手：{assistant_msg[:1500]}"

        try:
            resp = chat_memory(
                "你是技能抽取器。只输出JSON，不要解释。",
                prompt,
                temperature=0.2,
                max_tokens=1500,
            )
            m = re.search(r"\{.*\}", resp, re.DOTALL)
            if not m:
                return None
            result = json.loads(m.group(0))
        except Exception as e:
            logger.debug("LLM skill extraction from session failed: %s", e)
            return None

        if not result.get("should_create"):
            return None

        # 创建技能
        name = result.get("name", "").strip()
        if not name:
            return None

        # 二次去重：用关键词搜索已有技能，检查是否功能重叠
        dedup_query = f"{name} {result.get('description', '')} {result.get('trigger', '')}"
        existing = self.store.search(dedup_query, limit=3)
        for ex in existing:
            if ex.get("score", 0) >= 3 and ex.get("name") != name:
                # 高分匹配到已有技能，跳过创建，返回已有技能信息
                return {"path": ex["path"], "name": ex["name"], "status": f"已存在相似技能「{ex['name']}」，跳过创建"}

        created = self.store.create_skill(
            name=name,
            description=result.get("description", ""),
            body=result.get("body", ""),
            tags=result.get("tags"),
            trigger=result.get("trigger", ""),
            category=result.get("category", "general"),
        )
        # 成长系统：技能创建事件
        if created.get("status") == "已创建":
            try:
                from deskmate.growth.engine import GrowthEngine

                GrowthEngine(base_dir=self.base_dir).record_skill_created(name)
            except Exception as e:
                logger.debug("growth engine skill_created record failed: %s", e)
        return created

    # ------------------------------------------------------------------
    # 自动进化（用户纠正时 + 成功执行后）
    # ------------------------------------------------------------------

    def evolve_skill_on_correction(
        self,
        correction_text: str,
        user_message: str = "",
        assistant_output: str = "",
    ) -> list[dict]:
        """当用户纠正某事时，检查是否涉及已有技能，自动更新。

        Args:
            correction_text: 用户的纠正内容
            user_message: 原始用户消息（用于匹配相关技能）
            assistant_output: 上一轮助手输出（被纠正的对象）。提供后 LLM 能基于
                「助手错了什么 + 用户怎么纠」的完整证据链判断是否更新技能，
                而不是只看纠正文本凭空猜。

        Returns:
            被更新的技能列表
        """
        if not llm_available():
            return []

        # 找到可能相关的技能（提高 limit 到 5）
        skills = self.match_skills(user_message or correction_text, limit=5)
        if not skills:
            return []

        updated = []
        for s in skills:
            full = self.store.read_skill(s["path"])
            if not full:
                continue

            body = full.get("body", "")
            prompt = (
                f"用户纠正了一个问题，可能与技能「{s['name']}」相关。\n\n"
                f"技能内容：\n{body[:1000]}\n\n"
                f"用户纠正：{correction_text}\n\n"
            )
            if assistant_output:
                prompt += (
                    "上一轮助手输出（被纠正的对象，看这里才能知道助手错在哪）：\n"
                    f"{assistant_output[:800]}\n\n"
                )
            prompt += (
                "判断：这个纠正是否应该更新到这个技能里？\n"
                "如果是，输出 JSON：\n"
                '{"should_update": true, "old_string": "技能中需要修改的原文（精确匹配）", '
                '"new_string": "修改后的文本（补充踩坑记录或更新步骤）"}\n'
                "如果不需要，输出：\n"
                '{"should_update": false}\n\n'
                "只输出 JSON，不要解释。"
            )

            try:
                resp = chat(
                    "你是技能进化器。只输出JSON，不要解释。",
                    prompt,
                    temperature=0.2,
                    max_tokens=800,
                )
                m = re.search(r"\{.*\}", resp, re.DOTALL)
                if not m:
                    continue
                result = json.loads(m.group(0))
            except Exception as e:
                logger.debug("LLM skill evolution failed: %s", e)
                continue

            if not result.get("should_update"):
                continue

            old_str = result.get("old_string", "")
            new_str = result.get("new_string", "")
            if old_str and new_str and old_str in body:
                patch_result = self.store.patch_skill(
                    s["path"], old_str, new_str
                )
                if patch_result.get("status") == "ok":
                    updated.append({"name": s["name"], "path": s["path"]})
                    # 成长系统：技能进化事件（用户纠正后打磨 → 熟练度提升）
                    self._record_growth_skill_evolved(s["name"], s["path"])

        return updated

    def evolve_skill_on_success(
        self, user_message: str, assistant_message: str, skill_name: str = ""
    ) -> dict | None:
        """任务成功执行后，检查是否可以优化相关技能。

        进化闭环的关键一环：成功执行 = 一次训练，训练产生的新经验应回流到技能中。

        Args:
            user_message: 用户消息
            assistant_message: 助手回复
            skill_name: 如果本次使用了特定技能，传入名称

        Returns:
            被更新的技能信息，或 None
        """
        if not llm_available():
            return None

        # 确定要检查的技能
        skills_to_check = []
        if skill_name:
            path = self.store._resolve_path(skill_name)
            if path:
                full = self.store.read_skill(str(path))
                if full:
                    skills_to_check.append({"name": skill_name, "path": str(path), **full})
        else:
            # 自动匹配相关技能
            matched = self.match_skills(user_message, limit=3)
            for m in matched:
                full = self.store.read_skill(m["path"])
                if full:
                    skills_to_check.append({**m, **full})

        if not skills_to_check:
            return None

        for s in skills_to_check:
            body = s.get("body", "")
            prompt = (
                f"助手刚成功完成了一个任务。检查技能「{s['name']}」是否可以从这次执行中学到新东西。\n\n"
                f"技能当前内容：\n{body[:1200]}\n\n"
                f"用户请求：{user_message[:500]}\n\n"
                f"助手执行结果（摘要）：{assistant_message[:500]}\n\n"
                "判断：这次执行是否产生了技能中没有记录的新经验？\n"
                "新经验可能是：更好的步骤、新的注意事项、更高效的路径、踩坑记录等。\n\n"
                "如果有新经验，输出 JSON：\n"
                '{"should_update": true, "old_string": "技能中需要修改的原文（精确匹配）", '
                '"new_string": "更新后的文本（融入新经验）"}\n'
                "如果没有新经验，输出：\n"
                '{"should_update": false}\n\n'
                "只输出 JSON，不要解释。"
            )

            try:
                resp = chat(
                    "你是技能进化器。只输出JSON，不要解释。",
                    prompt,
                    temperature=0.2,
                    max_tokens=800,
                )
                m = re.search(r"\{.*\}", resp, re.DOTALL)
                if not m:
                    continue
                result = json.loads(m.group(0))
            except Exception as e:
                logger.debug("LLM skill evolution failed: %s", e)
                continue

            if not result.get("should_update"):
                continue

            old_str = result.get("old_string", "")
            new_str = result.get("new_string", "")
            if old_str and new_str and old_str in body:
                patch_result = self.store.patch_skill(s["path"], old_str, new_str)
                if patch_result.get("status") == "ok":
                    # 递增使用计数
                    self.store.increment_uses(s["path"])
                    # 成长系统：技能进化事件（成功执行后积累经验）
                    self._record_growth_skill_evolved(s["name"], s["path"])
                    return {"name": s["name"], "path": s["path"], "status": "已从成功执行中进化"}

        return None

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        return self.store.stats()