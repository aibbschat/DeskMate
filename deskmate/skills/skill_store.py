"""SkillStore — 可累积、可复用的技能库。

设计原则：
- 每个技能是一个独立 .md 文件，带 YAML frontmatter
- 技能存储在 ~/.deskmate/skills/ 下，按分类子目录组织
- 支持创建、搜索、匹配、更新（patch）、归档
- 用户可随时在文件系统中查看和编辑

文件格式（参考 Hermes SKILL.md）：
  ---
  name: novel-review
  description: 审校萧盛的历史小说章节
  tags: [写作, 审校, 小说]
  trigger: 用户让你审校小说章节时
  created: 2026-07-31
  updated: 2026-07-31
  uses: 0
  ---
  # 技能标题
  具体步骤...
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional


# 技能目录
DEFAULT_SKILLS_DIR = Path(os.path.expanduser("~/.deskmate/skills"))

# 技能分类
CATEGORIES = ["writing", "coding", "office", "general"]


def _safe_filename(name: str) -> str:
    """技能名 → 安全文件名。"""
    name = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]", "-", name)
    return name.strip("-") or "untitled"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML frontmatter，返回 (metadata, body)。"""
    if not text.startswith("---\n"):
        return {}, text
    parts = text[4:].split("\n---\n", 1)
    if len(parts) != 2:
        return {}, text
    meta = {}
    for line in parts[0].split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if key == "tags":
            if val.startswith("[") and val.endswith("]"):
                items = [
                    t.strip().strip("'\"")
                    for t in val[1:-1].split(",")
                    if t.strip()
                ]
                meta[key] = items
            else:
                meta[key] = [val] if val else []
        elif key == "uses":
            try:
                meta[key] = int(val)
            except ValueError:
                meta[key] = 0
        elif key == "success_rate":
            try:
                meta[key] = float(val)
            except ValueError:
                meta[key] = 0.0
        elif key == "last_used":
            meta[key] = val
        else:
            meta[key] = val
    return meta, parts[1].lstrip("\n")


def _build_frontmatter(meta: dict) -> str:
    """从 dict 构建 YAML frontmatter。"""
    lines = ["---"]
    for key in ("name", "description", "tags", "trigger", "created", "updated", "uses", "success_rate", "last_used"):
        if key not in meta:
            continue
        val = meta[key]
        if key == "tags" and isinstance(val, list):
            if val:
                lines.append(f"tags: [{', '.join(val)}]")
        elif key in ("uses",):
            lines.append(f"uses: {val}")
        elif key == "success_rate":
            lines.append(f"success_rate: {val:.2f}")
        elif key == "last_used":
            lines.append(f"last_used: {val}")
        elif val:
            lines.append(f"{key}: {val}")
    lines.append("---")
    return "\n".join(lines)


def _compute_hash(content: str) -> str:
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:12]


class SkillStore:
    """技能存储层。

    管理 ~/.deskmate/skills/ 下的 .md 技能文件。
    """

    def __init__(self, skills_dir: Path | str | None = None):
        if skills_dir:
            self.skills_dir = Path(skills_dir)
        else:
            self.skills_dir = Path(
                os.environ.get("DESKMATE_SKILLS_DIR", str(DEFAULT_SKILLS_DIR))
            )
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        for cat in CATEGORIES:
            (self.skills_dir / cat).mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    def create_skill(
        self,
        name: str,
        description: str,
        body: str,
        tags: list[str] | None = None,
        trigger: str = "",
        category: str = "general",
    ) -> dict:
        """创建新技能文件。

        Args:
            name: 技能名称（用作文件名）
            description: 简短描述
            body: 技能正文（Markdown）
            tags: 标签列表
            trigger: 触发条件描述
            category: 分类目录（writing/coding/office/general）

        Returns:
            {"path": str, "name": str, "status": str}
        """
        if category not in CATEGORIES:
            category = "general"
        filename = _safe_filename(name) + ".md"
        filepath = self.skills_dir / category / filename
        if filepath.exists():
            return {"path": str(filepath), "name": name, "status": "已存在，跳过创建"}

        today = date.today().isoformat()
        meta = {
            "name": name,
            "description": description,
            "tags": tags or [],
            "trigger": trigger,
            "created": today,
            "updated": today,
            "uses": 0,
        }
        content = _build_frontmatter(meta) + "\n\n" + body.strip() + "\n"
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")
        return {"path": str(filepath), "name": name, "status": "已创建"}

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def read_skill(self, name_or_path: str) -> dict | None:
        """读取技能文件，返回 {meta, body, path}。"""
        filepath = self._resolve_path(name_or_path)
        if not filepath:
            return None
        try:
            text = filepath.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(text)
            return {"meta": meta, "body": body, "path": str(filepath)}
        except OSError:
            return None

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """搜索匹配的技能。

        搜索范围：name, description, trigger, tags, body 关键词。
        支持中文 CJK 分词（二元组切分）。
        """
        keywords = self._tokenize(query)
        if not keywords:
            return self.list_skills(limit=limit)

        results = []
        for skill in self._all_skills():
            score = self._match_score(skill, keywords)
            if score > 0:
                results.append({**skill, "score": score})

        results.sort(key=lambda s: s["score"], reverse=True)
        return results[:limit]

    def _tokenize(self, text: str) -> list[str]:
        """中英文混合分词。

        - 英文按空格/标点分词，保留 >= 2 字符的词
        - 中文提取所有 2-gram（连续两字），作为搜索关键词
        - 不保留完整中文片段（避免跨词误匹配）
        """
        tokens: list[str] = []

        # 按标点和空格分割
        segments = re.split(r"[\s，。、？！,.?!\-()（）\[\]【】\"\"''「」]+", text)

        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue

            # 分离中文和非中文部分
            cjk_chars = re.findall(r"[\u4e00-\u9fff]", seg)
            non_cjk = re.findall(r"[a-zA-Z0-9]+", seg)

            # 英文词：保留 >= 2 字符
            for w in non_cjk:
                if len(w) >= 2:
                    tokens.append(w.lower())

            # 中文：只提取 2-gram（不保留完整片段，避免跨词误匹配）
            if cjk_chars:
                for i in range(len(cjk_chars) - 1):
                    bigram = cjk_chars[i] + cjk_chars[i + 1]
                    if bigram not in tokens:
                        tokens.append(bigram)

        return tokens

    def _match_score(self, skill: dict, keywords: list[str]) -> int:
        """计算技能与关键词的匹配分数。

        搜索范围包括 name, description, trigger, tags, body。
        使用子串匹配 + CJK 字符重叠度双重检测。
        """
        # 搜索所有可搜索字段（元数据）
        meta_text = (
            (skill.get("name") or "")
            + " "
            + (skill.get("description") or "")
            + " "
            + (skill.get("trigger") or "")
            + " "
            + " ".join(skill.get("tags") or [])
        ).lower()

        # 也搜索 body（技能正文），但权重减半
        body_text = (skill.get("body") or "").lower()[:2000]

        score = 0
        for kw in keywords:
            kw_lower = kw.lower()
            if not kw_lower:
                continue

            # 1. 完整子串匹配（最可靠）
            if kw_lower in meta_text:
                score += 3
            elif kw_lower in body_text:
                score += 2
            else:
                # 2. CJK 字符重叠度检测（对2-gram特别有效）
                kw_chars = set(re.findall(r"[\u4e00-\u9fff]", kw_lower))
                if kw_chars:
                    meta_chars = set(re.findall(r"[\u4e00-\u9fff]", meta_text))
                    body_chars = set(re.findall(r"[\u4e00-\u9fff]", body_text))
                    overlap_meta = len(kw_chars & meta_chars)
                    overlap_body = len(kw_chars & body_chars)
                    # 至少 50% 字符重叠才算匹配
                    if overlap_meta >= len(kw_chars) * 0.5 and overlap_meta >= 1:
                        score += 2
                    elif overlap_body >= len(kw_chars) * 0.5 and overlap_body >= 1:
                        score += 1

        return score

    # ------------------------------------------------------------------
    # 更新
    # ------------------------------------------------------------------

    def patch_skill(self, name_or_path: str, old_string: str, new_string: str) -> dict:
        """在技能文件中精确替换一段文本。

        用于踩坑后自动更新技能步骤。
        """
        filepath = self._resolve_path(name_or_path)
        if not filepath:
            return {"status": "error", "message": f"技能不存在: {name_or_path}"}

        try:
            text = filepath.read_text(encoding="utf-8")
        except OSError:
            return {"status": "error", "message": "读取技能文件失败"}

        if old_string not in text:
            return {"status": "error", "message": "未找到要替换的文本"}

        updated = text.replace(old_string, new_string, 1)
        # 更新 updated 字段
        today = date.today().isoformat()
        updated = re.sub(
            r"^updated:.*$", f"updated: {today}", updated, flags=re.MULTILINE
        )
        filepath.write_text(updated, encoding="utf-8")
        return {"status": "ok", "message": "技能已更新"}

    def increment_uses(self, name_or_path: str, success: bool = True) -> None:
        """技能被使用后，递增 uses 计数，更新 success_rate 和 last_used。"""
        filepath = self._resolve_path(name_or_path)
        if not filepath:
            return
        try:
            text = filepath.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(text)
            uses = meta.get("uses", 0)
            old_rate = meta.get("success_rate", 0.0)
            # 滑动平均：新 success_rate = (old_rate * old_uses + new_outcome) / new_uses
            new_uses = uses + 1
            new_rate = (old_rate * uses + (1.0 if success else 0.0)) / new_uses
            meta["uses"] = new_uses
            meta["success_rate"] = round(new_rate, 2)
            meta["last_used"] = date.today().isoformat()
            meta["updated"] = date.today().isoformat()
            new_text = _build_frontmatter(meta) + "\n\n" + body.strip() + "\n"
            filepath.write_text(new_text, encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------

    def list_skills(self, category: str | None = None, limit: int = 20) -> list[dict]:
        """列出所有技能。"""
        skills = self._all_skills()
        if category and category in CATEGORIES:
            skills = [s for s in skills if s.get("category") == category]
        return skills[:limit]

    def _all_skills(self) -> list[dict]:
        """扫描所有技能文件。"""
        skills = []
        for cat in CATEGORIES:
            cat_dir = self.skills_dir / cat
            if not cat_dir.is_dir():
                continue
            for f in sorted(cat_dir.glob("*.md")):
                try:
                    text = f.read_text(encoding="utf-8")
                    meta, body = _parse_frontmatter(text)
                    skills.append(
                        {
                            "name": meta.get("name", f.stem),
                            "description": meta.get("description", ""),
                            "tags": meta.get("tags", []),
                            "trigger": meta.get("trigger", ""),
                            "category": cat,
                            "path": str(f),
                            "uses": meta.get("uses", 0),
                            "success_rate": meta.get("success_rate", 0.0),
                            "last_used": meta.get("last_used", ""),
                            "updated": meta.get("updated", ""),
                            "body": body[:2000],  # 截断避免过长，供搜索用
                        }
                    )
                except OSError:
                    continue
        return skills

    # ------------------------------------------------------------------
    # 归档
    # ------------------------------------------------------------------

    def archive_skill(self, name_or_path: str) -> dict:
        """归档技能（移到 archive/ 子目录）。"""
        filepath = self._resolve_path(name_or_path)
        if not filepath:
            return {"status": "error", "message": f"技能不存在: {name_or_path}"}

        archive_dir = self.skills_dir / "archive"
        archive_dir.mkdir(exist_ok=True)
        try:
            target = archive_dir / filepath.name
            filepath.rename(target)
            return {"status": "ok", "message": f"已归档: {filepath.name}"}
        except OSError as e:
            return {"status": "error", "message": f"归档失败: {e}"}

    def stats(self) -> dict:
        """返回技能统计信息。"""
        skills = self._all_skills()
        by_cat = {}
        for s in skills:
            cat = s.get("category", "general")
            by_cat[cat] = by_cat.get(cat, 0) + 1
        return {
            "total": len(skills),
            "by_category": by_cat,
            "total_uses": sum(s.get("uses", 0) for s in skills),
        }

    # ------------------------------------------------------------------
    # 使用历史
    # ------------------------------------------------------------------

    def record_use(self, skill_name: str, context: str = "", success: bool = True) -> None:
        """记录技能使用历史。"""
        history_file = self.skills_dir / "use_history.jsonl"
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "skill": skill_name,
            "context": context[:200],
            "success": success,
        }
        try:
            with open(history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def get_use_history(self, skill_name: str | None = None, limit: int = 50) -> list[dict]:
        """获取技能使用历史。"""
        history_file = self.skills_dir / "use_history.jsonl"
        if not history_file.exists():
            return []
        results = []
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if skill_name and entry.get("skill") != skill_name:
                            continue
                        results.append(entry)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        # 返回最近的
        return list(reversed(results[-limit:]))

    def get_skill_stats(self, skill_name: str) -> dict:
        """获取单个技能的使用统计。"""
        history = self.get_use_history(skill_name)
        total = len(history)
        successes = sum(1 for h in history if h.get("success", True))
        return {
            "total_uses": total,
            "successes": successes,
            "success_rate": successes / total if total > 0 else 0,
            "last_used": history[0]["ts"] if history else None,
        }

    def get_low_success_skills(self, threshold: float = 0.5, min_uses: int = 3) -> list[dict]:
        """获取成功率低于阈值的技能（需要优化或归档）。

        Args:
            threshold: 成功率阈值（默认 0.5 = 50%）
            min_uses: 最少使用次数才纳入统计（避免小样本误判）

        Returns:
            低成功率技能列表，按成功率升序
        """
        skills = self._all_skills()
        low = []
        for s in skills:
            uses = s.get("uses", 0)
            rate = s.get("success_rate", 0.0)
            if uses >= min_uses and rate < threshold:
                low.append({
                    "name": s["name"],
                    "uses": uses,
                    "success_rate": rate,
                    "last_used": s.get("last_used", ""),
                    "path": s["path"],
                })
        low.sort(key=lambda x: x["success_rate"])
        return low

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _resolve_path(self, name_or_path: str) -> Path | None:
        """根据技能名或路径解析文件。"""
        p = Path(name_or_path)
        if p.is_absolute() and p.exists():
            return p
        # 精确匹配（文件名）
        filename = _safe_filename(name_or_path) + ".md"
        for cat in CATEGORIES:
            candidate = self.skills_dir / cat / filename
            if candidate.exists():
                return candidate
        # 评分模糊搜索
        best_path: Path | None = None
        best_score = 0
        query = name_or_path.lower()
        for cat in CATEGORIES:
            cat_dir = self.skills_dir / cat
            if not cat_dir.is_dir():
                continue
            for f in cat_dir.glob("*.md"):
                stem = f.stem.lower()
                score = 0
                # 完全包含
                if query in stem:
                    score = 10
                # 前缀匹配
                elif stem.startswith(query):
                    score = 8
                # 子串匹配（去除连字符后）
                else:
                    clean_stem = stem.replace("-", "").replace("_", "")
                    clean_query = query.replace("-", "").replace("_", "")
                    if clean_query in clean_stem:
                        score = 6
                    # 编辑距离（对短名称有效）
                    elif len(query) >= 2 and len(stem) >= 2:
                        # 简单的字符重叠度
                        common = sum(1 for c in query if c in stem)
                        if common >= len(query) * 0.6:
                            score = int(common / max(len(query), len(stem)) * 5)
                if score > best_score:
                    best_score = score
                    best_path = f
        return best_path if best_score >= 6 else None