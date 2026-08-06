"""VaultStore - Obsidian vault 作为长期记忆存储层。

设计原则：
- vault 是用户的领地。系统只往里写新文件，不覆盖用户编辑过的文件。
- 每条记忆是一个独立 .md 文件，带 YAML frontmatter，可在 Obsidian 中直接浏览/编辑/链接。
- 系统写入时记录内容 hash，下次写入前对比。hash 不匹配 = 用户改过，跳过覆盖。
- 用户在 vault 里手动新建的 .md 文件，系统也会读到（双向）。

目录结构：
  vault/
  ├── preferences/    用户偏好
  ├── corrections/    用户纠正
  ├── work/           项目进展
  ├── knowledge/      结构化知识（人物圣经、技术方案等）
  └── archive/        被压缩淘汰的旧记忆（可恢复）

文件格式：
  ---
  type: preference
  tags: [写作, 反馈风格]
  created: 2026-07-29
  updated: 2026-07-29
  source: session-1785331300
  hash: a1b2c3d4...
  ---
  用户接受直接、具体、带例子的写作反馈，不要绕弯子铺垫。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import date

logger = logging.getLogger(__name__)
from pathlib import Path
from typing import Optional
from functools import lru_cache

# § 分节标题 -> vault 子目录 的映射
_CATEGORY_DIRS = {
    "preference": "preferences",
    "correction": "corrections",
    "work": "work",
    "note": "knowledge",
    "research": "knowledge",
}

# vault 搜索结果最大字符数
VAULT_SEARCH_MAX_CHARS = 2_000


def _safe_filename(text: str, max_len: int = 20) -> str:
    """把记忆文本转成安全的文件名（去掉特殊字符，截断到合理长度）。"""
    # 取第一句/第一行
    first_line = text.split("\n")[0].strip()
    # 截断到第一个逗号/句号前（取关键短语）
    for sep in ("，", "。", "；", ",", ".", ";", "：", ":"):
        idx = first_line.find(sep)
        if 4 < idx <= max_len:
            first_line = first_line[:idx]
            break
    # 去掉 markdown 符号和标点
    name = re.sub(r"[#*`\[\](){}|<>:;\"'!?.,，。、！？；：""''【】（）]+", "", first_line)
    # 空格转连字符
    name = re.sub(r"\s+", "-", name.strip())
    # 截断
    if len(name) > max_len:
        name = name[:max_len]
    return name or "untitled"


def _compute_hash(content: str) -> str:
    """计算内容的 hash（用于检测用户是否编辑过文件）。"""
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:12]


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML frontmatter，返回 (metadata, body)。

    不依赖 PyYAML，用简单的行级解析。
    """
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
        # tags: [a, b, c] -> list
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
        else:
            meta[key] = val
    return meta, parts[1].lstrip("\n")


def _build_frontmatter(meta: dict) -> str:
    """从 dict 构建 YAML frontmatter 字符串。"""
    lines = ["---"]
    for key in ("type", "tags", "created", "updated", "source", "hash"):
        if key not in meta:
            continue
        val = meta[key]
        if key == "tags" and isinstance(val, list):
            if val:
                lines.append(f"tags: [{', '.join(val)}]")
            # 空列表不写
        elif val:
            lines.append(f"{key}: {val}")
    lines.append("---")
    return "\n".join(lines)


class VaultStore:
    """Obsidian vault 存储层。

    管理 vault 目录下的 .md 记忆文件，支持：
    - 写入（带用户编辑保护）
    - 搜索（关键词 + 标签）
    - 归档（压缩淘汰的条目移到 archive/）
    - 列举（供前端展示）
    """

    def __init__(self, vault_dir: Path | str | None = None):
        if vault_dir:
            self.vault_dir = Path(vault_dir)
        else:
            # 默认 ~/.deskmate/vault/
            self.vault_dir = Path(
                os.environ.get(
                    "DESKMATE_VAULT_DIR",
                    os.path.expanduser("~/.deskmate/vault"),
                )
            )
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        # 确保子目录存在
        for subdir in _CATEGORY_DIRS.values():
            (self.vault_dir / subdir).mkdir(exist_ok=True)
        (self.vault_dir / "archive").mkdir(exist_ok=True)

        # 文件缓存：避免重复读取磁盘
        self._file_cache: dict[str, tuple[float, str]] = {}  # {path: (mtime, content)}
        self._last_scan_time: float = 0
        self._scan_interval: float = 5.0  # 5秒内不重复扫描

    def _read_file_cached(self, filepath: Path) -> str | None:
        """带缓存的文件读取，避免重复读取磁盘。"""
        try:
            stat = filepath.stat()
            mtime = stat.st_mtime
            cache_key = str(filepath)

            # 检查缓存
            if cache_key in self._file_cache:
                cached_mtime, cached_content = self._file_cache[cache_key]
                if cached_mtime == mtime:
                    return cached_content

            # 读取并缓存
            content = filepath.read_text(encoding="utf-8")
            self._file_cache[cache_key] = (mtime, content)
            return content
        except OSError:
            return None

    def _invalidate_cache(self, filepath: Path | None = None):
        """清除缓存，当文件被修改或删除时调用。"""
        if filepath:
            self._file_cache.pop(str(filepath), None)
        else:
            self._file_cache.clear()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def write_entry(
        self,
        content: str,
        category: str = "note",
        tags: list[str] | None = None,
        source: str = "",
        filename: str = "",
    ) -> str:
        """写入一条记忆到 vault。

        如果文件已存在且用户编辑过（hash 不匹配），不覆盖，
        而是创建 -v2, -v3 等版本文件。

        Returns:
            写入的文件路径（相对 vault 根）
        """
        category = category.lower().strip()
        if category not in _CATEGORY_DIRS:
            category = "note"
        subdir = _CATEGORY_DIRS[category]
        dirname = self.vault_dir / subdir

        # 确定文件名
        if not filename:
            filename = _safe_filename(content)
        # 确保文件名安全
        filename = re.sub(r"[^\w\u4e00-\u9fff\-]", "", filename).strip("-")
        if not filename:
            filename = "untitled"
        filepath = dirname / f"{filename}.md"

        # 检查文件是否已存在
        version = 2
        while filepath.exists():
            existing = filepath.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(existing)
            old_hash = meta.get("hash", "")

            if not old_hash:
                # 没有 hash（用户手动创建的文件），不覆盖
                filepath = dirname / f"{filename}-v{version}.md"
                version += 1
                continue

            if old_hash == _compute_hash(content):
                # 要写入的内容和原 hash 一致
                # 但检查 body 是否被用户改过
                if old_hash != _compute_hash(body.strip()):
                    # 用户编辑过 body，不覆盖原文件，创建新版本
                    filepath = dirname / f"{filename}-v{version}.md"
                    version += 1
                    continue
                # 内容完全相同且未被编辑，跳过
                return f"{subdir}/{filename}.md (已存在，跳过)"

            # hash 不同：检查是用户编辑过还是系统写入的不同内容
            # 如果 body 的 hash 等于 old_hash，说明 body 没被用户改过（系统写的 body 和 hash 一致）
            # 如果 body 的 hash 不等于 old_hash，说明用户编辑了 body 但 hash 没更新
            if old_hash == _compute_hash(body.strip()):
                # body 和 hash 一致 = 系统写的，但内容不同 = 旧版本，可以覆盖
                break
            else:
                # 用户编辑过（body 变了），不覆盖，创建新版本
                filepath = dirname / f"{filename}-v{version}.md"
                version += 1
                continue

        # 构建 frontmatter
        today = date.today().isoformat()
        content_hash = _compute_hash(content)
        meta = {
            "type": category,
            "tags": tags or [],
            "created": today,
            "updated": today,
            "source": source,
            "hash": content_hash,
        }
        fm = _build_frontmatter(meta)
        file_content = f"{fm}\n{content}\n"

        filepath.write_text(file_content, encoding="utf-8")
        # 写入后清除缓存，确保下次读取时获取最新内容
        self._invalidate_cache(filepath)
        return f"{subdir}/{filename}.md"

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        max_results: int = 5,
    ) -> str:
        """在 vault 中搜索相关记忆，返回拼接的 markdown。

        搜索逻辑：
        1. 提取关键词（空格/标点分词 + CJK 2-gram 补充）
        2. 遍历所有 .md 文件（排除 archive/）
        3. 按关键词命中数排序
        4. 返回前 N 条
        """
        # 基础关键词：按空格/标点分词
        base_keywords = [
            w for w in re.split(r"[\s，。、？！,.?!\-]+", query) if len(w) >= 2
        ][:6]
        # CJK 2-gram 补充：对长度 > 2 的关键词拆成 2 字片段
        # 这样 "简洁回复" -> ["简洁", "回复"]，即使原文是 "简洁的回复风格" 也能匹配
        keywords = list(base_keywords)
        for kw in base_keywords:
            if len(kw) > 2 and re.search(r"[\u4e00-\u9fff]", kw):
                for i in range(len(kw) - 1):
                    bigram = kw[i : i + 2]
                    if bigram not in keywords:
                        keywords.append(bigram)
        keywords = keywords[:10]  # 限制总数
        if not keywords:
            return ""

        results: list[tuple[int, str, str]] = []  # (score, rel_path, content)

        for subdir in set(_CATEGORY_DIRS.values()):
            dirpath = self.vault_dir / subdir
            if not dirpath.is_dir():
                continue
            for md_file in dirpath.glob("*.md"):
                text = self._read_file_cached(md_file)
                if text is None:
                    continue
                meta, body = _parse_frontmatter(text)
                # 搜索范围：body + tags + 文件名
                searchable = (
                    body.lower()
                    + " "
                    + " ".join(meta.get("tags", []))
                    + " "
                    + md_file.stem.lower()
                )
                score = sum(
                    1 for kw in keywords if kw.lower() in searchable
                )
                if score > 0:
                    rel_path = f"{subdir}/{md_file.name}"
                    results.append((score, rel_path, body.strip()))

        if not results:
            return ""

        # 按分数降序
        results.sort(key=lambda x: -x[0])
        results = results[:max_results]

        parts = []
        total_chars = 0
        for score, rel_path, body in results:
            entry = f"**[{rel_path}]**\n{body}"
            if total_chars + len(entry) > VAULT_SEARCH_MAX_CHARS:
                break
            parts.append(entry)
            total_chars += len(entry)

        return "\n\n---\n\n".join(parts)

    # ------------------------------------------------------------------
    # 归档
    # ------------------------------------------------------------------

    def archive_entry(
        self,
        subdir: str,
        filename: str,
        reason: str = "compressed",
    ) -> str:
        """把一个文件移到 archive/ 目录（压缩淘汰时不丢，可恢复）。

        Args:
            subdir: 子目录名（如 "preferences"）
            filename: 文件名（如 "喜欢简洁回复.md"）
            reason: 归档原因
        """
        src = self.vault_dir / subdir / filename
        if not src.exists():
            return f"文件不存在: {subdir}/{filename}"

        archive_dir = self.vault_dir / "archive"
        dst = archive_dir / filename
        # 避免重名
        if dst.exists():
            stem = src.stem
            suffix = src.suffix
            counter = 2
            while dst.exists():
                dst = archive_dir / f"{stem}-{counter}{suffix}"
                counter += 1

        src.rename(dst)

        # 在归档文件末尾追加归档原因
        try:
            content = dst.read_text(encoding="utf-8")
            content += f"\n\n<!-- archived: {reason} -->\n"
            dst.write_text(content, encoding="utf-8")
        except Exception as e:
            logger.debug("vault archive annotation failed: %s", e)

        # 归档后清除缓存
        self._invalidate_cache(src)
        self._invalidate_cache(dst)

        return f"已归档: {subdir}/{filename} -> archive/{dst.name}"

    # ------------------------------------------------------------------
    # 删除（永久）
    # ------------------------------------------------------------------

    def delete_entry(
        self,
        path: str,
    ) -> str:
        """永久删除 vault 中的一个记忆文件。

        Args:
            path: 文件路径（相对 vault 根，如 "preferences/喜欢简洁回复.md"），
                  或仅文件名（如 "喜欢简洁回复.md"，自动在各子目录中搜索）

        Returns:
            操作结果描述
        """
        target = self.vault_dir / path
        if not target.suffix:
            target = target.with_suffix(".md")

        # 直接路径不存在时，按文件名搜索
        if not target.exists():
            stem = Path(path).stem
            found = None
            for subdir in set(_CATEGORY_DIRS.values()) | {"archive"}:
                candidate = self.vault_dir / subdir / f"{stem}.md"
                if candidate.exists():
                    found = candidate
                    break
            if found is None:
                return f"文件不存在: {path}"
            target = found

        # 安全检查：确保目标在 vault 目录内
        try:
            target.resolve().relative_to(self.vault_dir.resolve())
        except ValueError:
            return f"安全拒绝：路径不在 vault 范围内"

        rel_path = target.relative_to(self.vault_dir)
        target.unlink()
        self._invalidate_cache(target)
        return f"已删除: {rel_path}"

    # ------------------------------------------------------------------
    # 列举（供前端展示）
    # ------------------------------------------------------------------

    def list_entries(
        self, subdir: str | None = None
    ) -> list[dict]:
        """列出 vault 中的记忆文件。

        Args:
            subdir: 指定子目录，None 则列举所有（排除 archive/）

        Returns:
            [{"path", "category", "tags", "created", "title", "body"}]
        """
        entries = []
        dirs = [subdir] if subdir else list(set(_CATEGORY_DIRS.values()))
        for d in dirs:
            dirpath = self.vault_dir / d
            if not dirpath.is_dir():
                continue
            for md_file in sorted(
                dirpath.glob("*.md"), key=lambda f: f.name, reverse=True
            ):
                text = self._read_file_cached(md_file)
                if text is None:
                    continue
                meta, body = _parse_frontmatter(text)
                entries.append(
                    {
                        "path": f"{d}/{md_file.name}",
                        "category": meta.get("type", d.rstrip("s")),
                        "tags": meta.get("tags", []),
                        "created": meta.get("created", ""),
                        "title": md_file.stem,
                        "body": body.strip()[:200],
                    }
                )
        return entries

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """返回 vault 统计信息。"""
        counts = {}
        total = 0
        for subdir in set(_CATEGORY_DIRS.values()) | {"archive"}:
            dirpath = self.vault_dir / subdir
            if dirpath.is_dir():
                n = len(list(dirpath.glob("*.md")))
                counts[subdir] = n
                total += n
        return {"total": total, "by_category": counts}

    # ------------------------------------------------------------------
    # 从 MEMORY.md § 分节导入
    # ------------------------------------------------------------------

    def import_from_memory_md(self, memory_content: str) -> list[str]:
        """把 MEMORY.md 的 § 分节导入为 vault 文件。

        每个分节里的每个条目（以空行分隔）变成一个 .md 文件。
        跳过已存在的文件。

        Returns:
            导入的文件路径列表
        """
        # 复用 manager 的解析逻辑
        parts = re.split(r"(?m)^§\s*", memory_content)
        imported = []
        for part in parts[1:]:
            if not part.strip():
                continue
            lines = part.split("\n", 1)
            header_rest = lines[0].strip()
            body = lines[1].strip() if len(lines) > 1 else ""
            if not body:
                continue

            # 解析 category
            tags = set(re.findall(r"#(\w+)", header_rest))
            title = re.sub(r"#\w+", "", header_rest).strip(" :·-")
            # 从标签推断 category
            category = "note"
            if "preference" in tags:
                category = "preference"
            elif "correction" in tags:
                category = "correction"
            elif "work" in tags:
                category = "work"
            elif title == "写作项目" or title == "研究":
                category = "knowledge"

            # 每个条目（空行分隔）单独导入
            entries = [e.strip() for e in re.split(r"\n\s*\n", body) if e.strip()]
            for entry in entries:
                # 去掉条目开头的 #标签
                clean = re.sub(r"^\s*#\w+\s*", "", entry).strip()
                if not clean:
                    continue
                result = self.write_entry(
                    content=clean,
                    category=category,
                    tags=list(tags) if tags else None,
                    source="MEMORY.md import",
                )
                if "跳过" not in result:
                    imported.append(result)

        return imported
