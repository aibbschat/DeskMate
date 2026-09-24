"""本地文件 / 笔记摄入管线（P2-3）。

对标 Khoj / OwnHuman：把用户**主动指定**的本地写作 / 笔记目录里的纯文本文件，
压缩成小桔的「事实记忆」（learned_history），让她更懂萧哥的项目与思路。

红线（贯穿全程）：
- **作者稿可辅助、原稿绝不动**：萧哥 2026-09-24 拍板——自己的稿子（如《国子监秘闻录》）
  是写作助理的核心工作对象，**读 / 讨论 / 灌进通用记忆** 都是红线之内、用户授权的正常行为。
  ``sacred_paths.guarded_file`` 只做通用安全校验（resolve + 扩展名白名单），不再对作者稿设防；
  目录层 ``guarded_dir_entry`` 允许发现/列出。唯一的硬红线是**原稿正文绝不直接改写/删除**，
  由 ``sacred_paths.guarded_write`` 守住（读稿**协助**——建人物圣经 / 看进度 / 查一致性——
  走 ``novel_memory`` 专用通道的 ``guarded_assistance_read``，本来就不受摄入层约束）。
- **不引入新存储策略**：直接复用 learned_history（与对话 / 观察学到的记忆同库），来源以
  ``source`` 列标记（``local_intake:<path>``），用户可在「学到的记忆」里看到并主动遗忘。
- **保守路径**：扫描只产出**预览候选**，沉淀（写入记忆）必须由用户在 UI 确认后才发生，
  后端绝不自动写入——把存储保留的决定权交还用户。

2026-09-23 巡检修复（两处漏防，均为实测可复现）：
1. ``ingest_files`` / ``build_facts`` 此前零守卫，端点又收任意 ``paths`` →
   直传任意路径即可灌进记忆库。现在读取前一律过 ``sacred_paths.guarded_file``
   （强制 resolve + 扩展名白名单）。
2. 遍历只看字面路径，配置目录里放 ``dirlink -> 某目录`` 即可绕过。
   现在每条条目过 ``sacred_paths.guarded_dir_entry``（基于解析后的真实路径判定）。

设计取向：纯规则摘要，**零 LLM**（与外部信息摄入流水线 ``memory/ingestion_pipeline.py`` 同构）；
fail-open：任何一步炸了都不向上抛，主聊天链路不受影响。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from deskmate.memory.learned_store import LearnedStore, _fingerprint
from deskmate.paths import deskmate_base_dir
from deskmate.sacred_paths import guarded_dir_entry, guarded_file

logger = logging.getLogger(__name__)

# 系统 / 依赖目录：跳过，避免扫进仓库、node_modules、构建产物、系统库等
_SYSTEM_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "target", "dist", "build", ".next", ".idea", ".vscode", ".cargo",
    ".rustup", "Library", "Caches", ".Trash",
}
# 纯文本 / 笔记类扩展名
_TEXT_EXTS = {".md", ".markdown", ".txt", ".text", ".org", ".rst"}

# 单文件读取上限（字节），防止超大文件拖垮
_MAX_READ_BYTES = 200_000
# 摘要最大长度
_DIGEST_MAX = 240
# 候选文件上限（一次扫描最多返回多少份预览）
_MAX_CANDIDATES = 200

_CONFIG_NAME = "ingest_dirs.json"


# ---------------------------------------------------------------------------
# 配置：摄入目录（用户主动添加的本地目录列表）
# ---------------------------------------------------------------------------
def _config_path() -> Path:
    base = deskmate_base_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / _CONFIG_NAME


def load_dirs() -> list[str]:
    """读取已配置的摄入目录（绝对路径列表）。文件不存在 / 损坏返回 []。"""
    try:
        p = _config_path()
        if not p.is_file():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(d) for d in data if d]
    except (OSError, ValueError):
        pass
    return []


def save_dirs(dirs: list[str]) -> list[str]:
    """去重、去空、取绝对路径后持久化；返回规范化后的列表。"""
    seen: set[str] = set()
    out: list[str] = []
    for d in dirs or []:
        d = str(d).strip()
        if not d:
            continue
        try:
            ap = str(Path(d).expanduser().resolve())
        except OSError:
            continue
        if ap in seen:
            continue
        seen.add(ap)
        out.append(ap)
    try:
        _config_path().write_text(
            json.dumps(out, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        logger.warning("save ingest dirs failed (read-only?)")
    return out


# ---------------------------------------------------------------------------
# 扫描：只产出预览候选，不写入记忆
# ---------------------------------------------------------------------------
def _walk(base: Path):
    """递归遍历 base，产出命中的纯文本笔记文件（跳过系统 / 隐藏目录）。

    作者稿（如《国子监秘闻录》）可读、可摄入，不再跳过——读稿协助是红线之内的
    正常行为。仅基础可遍历性由 ``guarded_dir_entry`` 把关，再由 ``guarded_file``
    做扩展名白名单校验。
    """
    try:
        entries = list(base.iterdir())
    except OSError:
        return
    for entry in entries:
        if not guarded_dir_entry(entry, allow_discovery=True):
            continue
        if entry.is_dir():
            name = entry.name
            if name in _SYSTEM_DIRS or name.startswith("."):
                continue
            yield from _walk(entry)
        elif entry.is_file():
            if entry.suffix.lower() not in _TEXT_EXTS:
                continue
            yield entry


def _excerpt(path: Path) -> str | None:
    """读文件前 _MAX_READ_BYTES，取首个非空段作为摘要（折叠空白，截断）。"""
    try:
        data = path.read_bytes()[:_MAX_READ_BYTES]
        text = data.decode("utf-8", errors="replace")
    except OSError:
        return None
    paras = [p.strip() for p in text.splitlines() if p.strip()]
    if not paras:
        return None
    digest = " ".join(paras[:3])  # 前 3 行 / 段
    digest = " ".join(digest.split())
    if len(digest) > _DIGEST_MAX:
        digest = digest[:_DIGEST_MAX].rstrip() + "…"
    return digest or None


def scan_directories(dirs: list[str], max_candidates: int = _MAX_CANDIDATES) -> list[dict[str, Any]]:
    """扫描给定目录，返回候选笔记文件预览（不写入记忆）。

    Returns:
        ``[{"path","name","size","mtime","excerpt"}]``，已跳过系统目录与不支持的扩展名。
        达到 ``max_candidates`` 即停止（preview 阶段不追求全量）。
    """
    candidates: list[dict[str, Any]] = []
    for raw in dirs or []:
        base = Path(raw).expanduser()
        if not base.is_dir():
            continue
        for path in _walk(base):
            if len(candidates) >= max_candidates:
                break
            # 命中后再过一次完整守卫：解析真实路径 + 扩展名白名单
            real = guarded_file(path, _TEXT_EXTS)
            if real is None:
                continue
            try:
                st = real.stat()
                excerpt = _excerpt(real)
                if excerpt is None:
                    continue
                candidates.append(
                    {
                        "path": str(real),
                        "name": real.name,
                        "size": int(st.st_size),
                        "mtime": int(st.st_mtime),
                        "excerpt": excerpt,
                    }
                )
            except OSError:
                continue
        if len(candidates) >= max_candidates:
            break
    return candidates


# ---------------------------------------------------------------------------
# 沉淀：把选中的笔记写入 learned_history（带 local_intake:<path> 来源标记）
# ---------------------------------------------------------------------------
def build_facts(path: str) -> list[str]:
    """把一份笔记压缩成小桔能记住的事实（一条）。

    用 ``[本地笔记] 文件名：摘要`` 形态——**不用《》括文件名**，避免被
    LearnedStore 的实体抽取误判成「书」，从而错误触发与真实书稿事实的矛盾取代。

    读取前必须过 ``sacred_paths.guarded_file``：本函数是外部可控路径（端点 ``paths``）
    抵达文件内容的最后一道门，此前这里没有任何守卫。
    """
    real = guarded_file(path, _TEXT_EXTS)
    if real is None:
        return []
    name = real.stem or real.name
    excerpt = _excerpt(real)
    if not excerpt:
        return []
    return [f"[本地笔记] {name}：{excerpt}"]


def ingest_files(paths: list[str], base_dir=None) -> dict[str, Any]:
    """把选中的笔记文件沉淀进 learned_history。

    更新语义（对同一文件）：
    - 内容不变（fp 集合相同）→ 幂等跳过，不重复沉淀；
    - 内容变了 → 把旧条目标 ``superseded``（不再参与回忆）后写入新条目。
    """
    store = LearnedStore(base_dir=base_dir)
    result: dict[str, Any] = {"ingested": 0, "updated": 0, "skipped": 0, "errors": []}
    for path in paths or []:
        try:
            # 先取真实路径：既做守卫，也让 source 标记对同一文件的不同软链路径稳定
            real = guarded_file(path, _TEXT_EXTS)
            if real is None:
                result["skipped"] += 1
                continue
            facts = build_facts(str(real))
            if not facts:
                result["skipped"] += 1
                continue
            src = f"local_intake:{real}"
            existing = {r["fp"] for r in store.find_active_by_source(src)}
            new_fps = {_fingerprint(f) for f in facts}
            if existing and existing == new_fps:
                # 内容未变，幂等跳过
                result["skipped"] += 1
                continue
            if existing:
                store.retire_by_source(src)
                result["updated"] += 1
            result["ingested"] += store.append(facts, source=src)
        except Exception as exc:  # fail-open：单文件出错不影响其余
            logger.exception("ingest file failed: %s", path)
            result["errors"].append({"path": path, "error": str(exc)})
    return result
