"""SkillStore — 可累积、可复用的技能库。

设计原则：
- 两套技能共存，互不污染：
  1) 自研扁平技能：~/.deskmate/skills/<category>/<name>.md，带自研 frontmatter
     （tags/trigger/created/updated/uses/success_rate/last_used），由桌搭闭环后
     自动沉淀（extract_skill_from_session）或用户手动创建。
  2) 导入的社区技能包：~/.deskmate/skills/imported/<skill-name>/SKILL.md，
     严格遵循 **agentskills.io 开放标准**（Anthropic 发起，Hermes/OpenClaw/
     WorkBuddy/Claude Code 等 27+ agent 通用）。目录式结构，frontmatter 仅需
     name + description，可选 license/compatibility/allowed-tools/metadata。
     导入后只读，绝不改写第三方包——用量统计走全局 sidecar（use_history.jsonl）。
- 支持创建、搜索、匹配、更新（patch）、归档、导入。
- 用户可随时在文件系统中查看和编辑。

自研技能文件格式：
  ---
  name: novel-review
  description: 审校萧盛的历史小说章节
  tags: [写作, 审校, 小说]
  trigger: 用户让你审校小说章节时
  created: 2026-07-31
  updated: 2026-07-31
  uses: 0
  ---

agentskills.io 包格式（skill-name/ 目录下 SKILL.md）：
  ---
  name: pdf-processing
  description: Extract text/tables from PDFs. Use when user mentions PDFs.
  license: Apache-2.0
  metadata:
    author: example-org
    version: "1.0"
  ---
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Optional

try:
    import yaml as _YAML
except ImportError:  # 兜底：冻结包若未收集 yaml 也不应崩，仅降级解析能力
    _YAML = None

logger = logging.getLogger(__name__)


# 技能目录
DEFAULT_SKILLS_DIR = Path(os.path.expanduser("~/.deskmate/skills"))

# 技能分类（自研扁平技能的子目录）
CATEGORIES = ["writing", "coding", "office", "general"]

# 导入的社区技能包根目录（agentskills.io 标准：imported/<skill-name>/SKILL.md）
IMPORTED_DIR = "imported"

# 方案乙：小桔自研技能除落本地扁平库外，额外发布到 Coworker 规范技能池
# （~/.config/coworker/skills，与 skill_list / effective_skill_names 同池），
# 使小桔生成的技能在「设置 → 技能」里可见、可被 load_skill 加载。
# 测试可用 DESKMATE_COWORKER_SKILLS_DIR 重定向发布目标。
def _coworker_skills_dir() -> Path | None:
    """Coworker 规范全局技能目录（小桔发布目标）。

    与生产 effective_skill_names 扫描的 global_dir 完全一致：
    coworker.secrets.state_dir()/skills（macOS/Linux → ~/.config/coworker/skills）。
    """
    override = os.environ.get("DESKMATE_COWORKER_SKILLS_DIR")
    if override:
        return Path(override)
    try:
        from coworker.secrets import state_dir

        return state_dir() / "skills"
    except Exception:
        # 兜底：与 state_dir 解析顺序一致（macOS/Linux XDG 式）
        if os.name == "nt":
            base = Path(
                os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")
            )
            return base / "coworker" / "skills"
        return Path.home() / ".config" / "coworker" / "skills"


def _coworker_skill_slug(name: str) -> str:
    """技能名 → Coworker 目录安全名（ascii）。中文名退化为短 hash。"""
    slug = re.sub(r"[^a-z0-9-]", "-", (name or "").lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        slug = hashlib.md5((name or "skill").encode("utf-8")).hexdigest()[:12]
    return slug


def _safe_filename(name: str) -> str:
    """技能名 → 安全文件名。"""
    name = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]", "-", name)
    return name.strip("-") or "untitled"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML frontmatter，返回 (metadata, body)。

    兼容多种来源并归一化：
    - 自研扁平技能：name/description/tags/trigger/created/updated/uses/...
    - agentskills.io 社区包（Hermes/OpenClaw/WorkBuddy 通用）：name/description
      （必需）+ author/version/license/homepage/compatibility/allowed-tools +
      嵌套 ``metadata:`` 块（Hermes 把 tags/category 放在 metadata.hermes.* 下）。

    归一化规则：
    - tags 统一为 list：优先顶层 tags，其次 metadata.hermes.tags，再 metadata.tags
    - author/version/license 取顶层（Hermes 放在顶层，而非 metadata 下）
    - category 顶层无则取 metadata.hermes.category
    - 完整 metadata 原样保留进 meta["metadata"] 供来源溯源
    """
    if not text.startswith("---\n"):
        return {}, text
    parts = text[4:].split("\n---\n", 1)
    if len(parts) != 2:
        return {}, text
    fm_text, body = parts[0], parts[1].lstrip("\n")

    raw: dict = {}
    if _YAML is not None:
        try:
            loaded = _YAML.safe_load(fm_text)
            if isinstance(loaded, dict):
                raw = loaded
        except _YAML.YAMLError:
            raw = {}
    if not raw:
        # 兜底：极简行解析（无 yaml 依赖时也能不崩；tags 仅取顶层）
        for line in fm_text.split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, _, val = line.partition(":")
            raw[key.strip()] = val.strip()

    meta: dict = {}
    # 顶层已知字段（含带连字符的 allowed-tools）
    for k in ("name", "description", "author", "version", "license",
              "homepage", "compatibility", "allowed-tools", "trigger",
              "category", "created", "updated", "uses", "success_rate",
              "last_used", "views"):
        if k in raw and raw[k] is not None:
            val = raw[k]
            # YAML 把裸日期解析成 date/datetime 对象，需转 ISO 字符串否则 JSON 崩
            if hasattr(val, 'isoformat'):
                val = val.isoformat()
            meta[k] = val

    # tags 归一化为 list
    tags = raw.get("tags")
    if tags is None:
        md = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        hermes = md.get("hermes") if isinstance(md.get("hermes"), dict) else {}
        tags = hermes.get("tags") if "tags" in hermes else md.get("tags")
    if isinstance(tags, str):
        inner = tags.strip()
        if inner.startswith("[") and inner.endswith("]"):
            inner = inner[1:-1]
        tags = [t.strip().strip("'\"") for t in inner.split(",") if t.strip()]
    elif not isinstance(tags, list):
        tags = []
    meta["tags"] = tags

    # category 补充：顶层无则取 metadata.hermes.category
    if not meta.get("category"):
        md = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        hermes = md.get("hermes") if isinstance(md.get("hermes"), dict) else {}
        if isinstance(hermes, dict) and hermes.get("category"):
            meta["category"] = hermes["category"]

    # 保留完整 metadata 供来源溯源
    if isinstance(raw.get("metadata"), dict):
        meta["metadata"] = raw["metadata"]

    return meta, body


def _build_frontmatter(meta: dict) -> str:
    """从 dict 构建 YAML frontmatter。"""
    lines = ["---"]
    for key in ("name", "description", "tags", "trigger", "created", "updated", "uses", "success_rate", "last_used", "views"):
        if key not in meta:
            continue
        val = meta[key]
        if key == "tags" and isinstance(val, list):
            if val:
                lines.append(f"tags: [{', '.join(val)}]")
        elif key in ("uses", "views"):
            lines.append(f"{key}: {val}")
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
        # 导入的社区技能包根目录（只读，绝不改写第三方包）
        (self.skills_dir / IMPORTED_DIR).mkdir(parents=True, exist_ok=True)

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
        # 方案乙：同步发布到 Coworker 规范技能池（skill_list 可见），失败不阻断主流程
        self.publish_to_coworker(
            name=name,
            description=description,
            body=body,
            tags=tags,
            trigger=trigger,
            category=category,
        )
        return {"path": str(filepath), "name": name, "status": "已创建"}

    # ------------------------------------------------------------------
    # 方案乙：发布到 Coworker 规范技能池（与 skill_list 同池）
    # ------------------------------------------------------------------

    def publish_to_coworker(
        self,
        name: str,
        description: str,
        body: str,
        tags: list[str] | None = None,
        trigger: str = "",
        category: str = "general",
    ) -> bool:
        """把技能以 Coworker SKILL.md 格式发布到规范全局技能目录。

        目录式 ``<slug>/SKILL.md``（与 coworker 的 SkillLoader / effective_skill_names
        完全一致），frontmatter 保留真实中文名。幂等覆盖。任何异常（权限/路径）都
        fail-open，绝不阻断小桔的技能创建主流程。

        Returns:
            True 表示成功落盘到规范池；目标不可写时返回 False。
        """
        cw = _coworker_skills_dir()
        if cw is None:
            return False
        try:
            cw.mkdir(parents=True, exist_ok=True)
            folder = cw / _coworker_skill_slug(name)
            folder.mkdir(parents=True, exist_ok=True)
            today = date.today().isoformat()
            lines = [
                "---",
                f"name: {name}",
                f"description: {description}",
                "source: deskmate",
                f"created: {today}",
                f"updated: {today}",
                "---",
                "",
                (body or "").strip(),
                "",
            ]
            (folder / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
            return True
        except OSError as e:
            logger.debug("publish_to_coworker failed (non-fatal): %s", e)
            return False

    # ------------------------------------------------------------------
    # 导入 agentskills.io 社区技能包（目录式，只读）
    # ------------------------------------------------------------------

    def import_skill_dir(self, pkg_dir: str | Path, *, overwrite: bool = False) -> dict:
        """导入一个 agentskills.io 标准技能包（目录，含 SKILL.md）。

        把整个包（含 scripts/references/assets）复制到
        ``skills/imported/<safe-name>/`` 下，供桌搭直接消费 Hermes/OpenClaw/
        WorkBuddy 等社区技能，无需自己造轮子。

        关键约束：导入后包为只读——绝不改写第三方 SKILL.md（用量统计走
        全局 use_history.jsonl sidecar），保证可幂等重导、可 diff 上游更新。

        Args:
            pkg_dir: 源技能包目录（必须含 SKILL.md）
            overwrite: 已存在时是否覆盖重新导入

        Returns:
            {"status": str, "name": str, "path": str}
        """
        src = Path(pkg_dir)
        skill_md = src / "SKILL.md"
        if not skill_md.is_file():
            return {"status": "error", "message": f"不是有效的技能包（缺少 SKILL.md）: {pkg_dir}"}
        try:
            raw = skill_md.read_text(encoding="utf-8")
            meta, _ = _parse_frontmatter(raw)
        except OSError:
            return {"status": "error", "message": "读取 SKILL.md 失败"}

        # agentskills.io 允许 name 与目录名不一致；落盘目录名用合规小写连字符
        name = (meta.get("name") or src.name).strip()
        safe = re.sub(r"[^a-z0-9-]", "-", name.lower())
        safe = re.sub(r"-+", "-", safe).strip("-") or "skill"
        dest = self.skills_dir / IMPORTED_DIR / safe
        if dest.exists():
            if not overwrite:
                return {"status": "已存在，跳过导入", "name": name, "path": str(dest)}
            shutil.rmtree(dest)

        try:
            shutil.copytree(src, dest)
        except OSError as e:
            return {"status": "error", "message": f"复制技能包失败: {e}"}
        return {"status": "已导入", "name": name, "path": str(dest)}

    def list_imported(self) -> list[dict]:
        """发现并解析 imported/ 下的 agentskills.io 技能包。

        Returns:
            技能 dict 列表（category 取包自带的分类，缺省 general，imported=True）。
        """
        result: list[dict] = []
        imported_root = self.skills_dir / IMPORTED_DIR
        if not imported_root.is_dir():
            return result
        for pkg in sorted(imported_root.iterdir()):
            if not pkg.is_dir():
                continue
            skill_md = pkg / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                text = skill_md.read_text(encoding="utf-8")
                meta, body = _parse_frontmatter(text)
            except OSError:
                continue
            result.append({
                "name": meta.get("name") or pkg.name,
                "description": meta.get("description", ""),
                "trigger": meta.get("trigger", ""),
                "tags": meta.get("tags", []),
                "category": meta.get("category") or "general",
                "path": str(skill_md),
                "pkg_dir": str(pkg),
                "imported": True,
                "license": meta.get("license", ""),
                "compatibility": meta.get("compatibility", ""),
                "allowed_tools": meta.get("allowed-tools", ""),
                "author": meta.get("author", ""),
                "version": meta.get("version", ""),
                "created": meta.get("created", ""),
                "uses": 0,
                "success_rate": 0.0,
                "last_used": "",
                "updated": meta.get("updated", ""),
                "body": body[:2000],
            })
        return result

    def _is_imported(self, path: str | Path) -> bool:
        """判断路径是否属于 imported/ 只读包（用于 guard 改写操作）。"""
        try:
            rel = Path(path).resolve().relative_to(self.skills_dir.resolve())
            return IMPORTED_DIR in rel.parts
        except (ValueError, OSError):
            return False

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

        用于踩坑后自动更新技能步骤。导入的社区技能包为只读，不支持自动修补。
        """
        filepath = self._resolve_path(name_or_path)
        if not filepath:
            return {"status": "error", "message": f"技能不存在: {name_or_path}"}
        if self._is_imported(filepath):
            return {"status": "error", "message": f"导入的技能包为只读，不支持自动修补: {name_or_path}"}

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
        # 方案乙：同步到 Coworker 规范池，避免两池副本漂移
        try:
            m, b = _parse_frontmatter(updated)
            self.publish_to_coworker(
                name=m.get("name", name_or_path),
                description=m.get("description", ""),
                body=b,
                tags=m.get("tags"),
                trigger=m.get("trigger", ""),
                category=filepath.parent.name,
            )
        except Exception as e:  # noqa: BLE001 - fail-open，绝不阻断主流程
            logger.debug("patch_skill 同步发布到 coworker 失败 (non-fatal): %s", e)
        return {"status": "ok", "message": "技能已更新"}

    def increment_uses(self, name_or_path: str, success: bool = True) -> None:
        """技能被使用后，递增 uses 计数，更新 success_rate 和 last_used。

        导入的社区技能包（agentskills.io）为只读，绝不改写其 SKILL.md；
        其用量追踪走全局 use_history.jsonl sidecar（见 record_use）。
        """
        filepath = self._resolve_path(name_or_path)
        if not filepath:
            return
        if self._is_imported(filepath):
            return  # 只读包：不回写用量统计
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

    def bump_view(self, name_or_path: str) -> None:
        """技能被查看时递增 views 计数（仅 engagement，不计入成功/失败）。

        与 increment_uses 解耦：查看 ≠ 「成功使用」，避免把浏览量算进
        success_rate 与成长系统 XP（A2 修复：原 skill_view 把浏览当成功使用，
        导致 success_rate 恒为 1.0、成长 XP 被浏览量灌满）。导入的只读社区包
        同样跳过回写。
        """
        filepath = self._resolve_path(name_or_path)
        if not filepath:
            return
        if self._is_imported(filepath):
            return
        try:
            text = filepath.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(text)
            meta["views"] = int(meta.get("views", 0)) + 1
            meta["updated"] = date.today().isoformat()
            new_text = _build_frontmatter(meta) + "\n\n" + body.strip() + "\n"
            filepath.write_text(new_text, encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------

    def list_skills(
        self, category: str | None = None, limit: int = 20, include_coworker: bool = False
    ) -> list[dict]:
        """列出所有技能。

        include_coworker=True 时，额外合并 Coworker 规范技能池
        （~/.config/coworker/skills）里小桔扁平库没有的技能，使小桔的技能列表
        与「设置 → 技能」（skill_list）看到的池子一致——方案乙「同一个池子」。
        """
        skills = self._all_skills(include_coworker=include_coworker)
        if category and category in CATEGORIES:
            skills = [s for s in skills if s.get("category") == category]
        return skills[:limit]

    def _all_skills(self, include_coworker: bool = False) -> list[dict]:
        """扫描所有技能文件（自研扁平 .md + 导入的 agentskills.io 包）。

        include_coworker=True 时合并 Coworker 规范池（见 list_skills）。
        扁平库与 Coworker 池同名技能去重时扁平库优先（元数据更丰富）。
        """
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
                            "created": meta.get("created", ""),
                            "updated": meta.get("updated", ""),
                            "body": body[:2000],  # 截断避免过长，供搜索用
                        }
                    )
                except OSError:
                    continue
        # 导入的社区技能包（agentskills.io）
        skills.extend(self.list_imported())

        if include_coworker:
            skills.extend(self._coworker_pool_skills(skills))
        return skills

    def _coworker_pool_skills(self, existing: list[dict]) -> list[dict]:
        """扫描 Coworker 规范技能池，返回扁平库里没有的技能（去重）。"""
        cw = _coworker_skills_dir()
        if not cw or not cw.is_dir():
            return []
        seen = {s.get("name") for s in existing}
        out: list[dict] = []
        for sub in sorted(cw.iterdir()):
            md = sub / "SKILL.md"
            if not md.is_file():
                continue
            try:
                text = md.read_text(encoding="utf-8")
                meta, body = _parse_frontmatter(text)
            except OSError:
                continue
            name = meta.get("name") or sub.name
            if name in seen:
                continue  # 扁平库优先（元数据更丰富）
            seen.add(name)
            out.append(
                {
                    "name": name,
                    "description": meta.get("description", ""),
                    "tags": meta.get("tags", []),
                    "trigger": meta.get("trigger", ""),
                    "category": meta.get("category") or "general",
                    "path": str(md),
                    "uses": meta.get("uses", 0),
                    "success_rate": meta.get("success_rate", 0.0),
                    "last_used": meta.get("last_used", ""),
                    "created": meta.get("created", ""),
                    "updated": meta.get("updated", ""),
                    "body": body[:2000],
                }
            )
        return out

    # ------------------------------------------------------------------
    # 归档
    # ------------------------------------------------------------------

    def archive_skill(self, name_or_path: str) -> dict:
        """归档技能（移到 archive/ 子目录）。导入的社区技能包不支持归档。"""
        filepath = self._resolve_path(name_or_path)
        if not filepath:
            return {"status": "error", "message": f"技能不存在: {name_or_path}"}
        if self._is_imported(filepath):
            return {"status": "error", "message": f"导入的技能包不支持归档: {name_or_path}"}

        archive_dir = self.skills_dir / "archive"
        archive_dir.mkdir(exist_ok=True)
        try:
            target = archive_dir / filepath.name
            filepath.rename(target)
            return {"status": "ok", "message": f"已归档: {filepath.name}"}
        except OSError as e:
            return {"status": "error", "message": f"归档失败: {e}"}

    def restore_skill(self, name_or_path: str) -> dict:
        """从 archive/ 还原技能（一键还原，归档不会删除）。

        归位到技能 frontmatter 记录的 category 目录（缺省 general）。
        若目标已存在同名活跃技能，则不覆盖、返回 error。
        """
        archive_dir = self.skills_dir / "archive"
        if not archive_dir.is_dir():
            return {"status": "error", "message": "没有可还原的归档技能"}

        filename = _safe_filename(name_or_path) + ".md"
        src: Path | None = None
        # 1) 文件名精确匹配（自研扁平技能归档后文件名不变）
        cand = archive_dir / filename
        if cand.is_file():
            src = cand
        else:
            # 2) frontmatter name 模糊匹配
            for f in archive_dir.glob("*.md"):
                try:
                    meta, _ = _parse_frontmatter(f.read_text(encoding="utf-8"))
                except OSError:
                    continue
                if (meta.get("name") or f.stem) == name_or_path:
                    src = f
                    break
        if src is None:
            return {"status": "error", "message": f"归档中找不到技能: {name_or_path}"}

        # 读取原 category 归位
        try:
            meta, _ = _parse_frontmatter(src.read_text(encoding="utf-8"))
            category = meta.get("category") or "general"
        except OSError:
            category = "general"
        if category not in CATEGORIES:
            category = "general"
        target_dir = self.skills_dir / category
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / src.name
        if target.exists():
            return {"status": "error", "message": f"已存在同名活跃技能，未覆盖: {target.name}"}
        try:
            src.rename(target)
            return {"status": "ok", "message": f"已还原: {target.name}（{category}）"}
        except OSError as e:
            return {"status": "error", "message": f"还原失败: {e}"}

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
        """根据技能名或路径解析文件（支持自研扁平技能与导入的 agentskills.io 包）。"""
        p = Path(name_or_path)
        if p.is_absolute() and p.exists():
            return p

        imported_root = self.skills_dir / IMPORTED_DIR

        # 精确匹配：imported/<name>/SKILL.md（按目录名或 frontmatter name）
        if imported_root.is_dir():
            cand = imported_root / name_or_path / "SKILL.md"
            if cand.exists():
                return cand
            for pkg in imported_root.iterdir():
                if pkg.is_dir() and (pkg / "SKILL.md").is_file():
                    try:
                        meta, _ = _parse_frontmatter((pkg / "SKILL.md").read_text(encoding="utf-8"))
                    except OSError:
                        continue
                    if (meta.get("name") or pkg.name) == name_or_path:
                        return pkg / "SKILL.md"

        # 精确匹配（自研扁平文件名）
        filename = _safe_filename(name_or_path) + ".md"
        for cat in CATEGORIES:
            candidate = self.skills_dir / cat / filename
            if candidate.exists():
                return candidate

        # 评分模糊搜索（自研扁平 + 导入包目录名/name）
        best_path: Path | None = None
        best_score = 0
        query = name_or_path.lower()
        search_dirs: list[tuple[Path, str]] = [(self.skills_dir / c, "flat") for c in CATEGORIES]
        if imported_root.is_dir():
            for pkg in imported_root.iterdir():
                if pkg.is_dir() and (pkg / "SKILL.md").is_file():
                    search_dirs.append((pkg, "imported"))
        for d, kind in search_dirs:
            if not d.is_dir():
                continue
            files = [d / "SKILL.md"] if kind == "imported" else list(d.glob("*.md"))
            for f in files:
                if not f.is_file():
                    continue
                stem = (d.name if kind == "imported" else f.stem).lower()
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
                    # 字符重叠度（对短名称有效）
                    elif len(query) >= 2 and len(stem) >= 2:
                        common = sum(1 for c in query if c in stem)
                        if common >= len(query) * 0.6:
                            score = int(common / max(len(query), len(stem)) * 5)
                if score > best_score:
                    best_score = score
                    best_path = f
        return best_path if best_score >= 6 else None


# ---------------------------------------------------------------------------
# 方案乙：存量迁移 —— 把扁平技能库里的技能一键发布到 Coworker 规范池（幂等）
# ---------------------------------------------------------------------------

def migrate_legacy_flat_skills(store: "SkillStore") -> int:
    """把扁平技能库里的存量技能发布到 Coworker 规范技能池（幂等覆盖）。

    用于「方案乙」上线后，把历史上落在 ~/.deskmate/skills 的技能对齐到
    ~/.config/coworker/skills，使 skill_list 看到完整池子。返回发布数量。
    任何单条失败都不影响其余（fail-open）。
    """
    count = 0
    for s in store.list_skills(limit=2000):
        try:
            full = store.read_skill(s["name"])
            if not full:
                continue
            store.publish_to_coworker(
                name=full["meta"].get("name", s["name"]),
                description=full["meta"].get("description", ""),
                body=full["body"],
                tags=full["meta"].get("tags"),
                trigger=full["meta"].get("trigger", ""),
                category=s.get("category", "general"),
            )
            count += 1
        except Exception as e:  # noqa: BLE001 - 单条失败不阻断
            logger.debug("migrate_legacy_flat_skills 跳过 %s: %s", s.get("name"), e)
    return count