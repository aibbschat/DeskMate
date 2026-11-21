"""技能库自整理（curator）—— 对标 Hermes agent/curator.py。

周期性在后台运行：扫描技能库，发现重复/重叠候选 + 长期未用技能，
主动把合并候选与归档候选写入报告（默认非破坏性，绝不自动合并/删除以免丢信息）；
设 auto_archive=True 时仅归档「从未使用、陈旧、且与别的技能高重叠（Jaccard≥阈值）」的严格低价值技能；其余仅出报告。开启方式见 auto_archive_enabled()。

设计取舍（对齐 Hermes 的「受保护技能」原则）：
- 合并是信息有损操作，默认只提案、不自动执行，留给人确认。
- 归档只针对「用户从未用过 / 陈旧 / 无关联」的低价值技能；
  导入的社区技能包与手写低频技能默认不动。
- 任意异常都不影响主流程（fail-open）。

触发节奏由调用方控制：after_action 后台线程按 7 天间隔调用（见 _should_run，
基于报告 mtime，避免每轮都扫）。间隔可用环境变量覆盖。
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_CURATOR_INTERVAL_DAYS = int(os.environ.get("DESKMATE_CURATOR_INTERVAL_DAYS", "7"))
_CURATOR_STALE_DAYS = int(os.environ.get("DESKMATE_CURATOR_STALE_DAYS", "30"))
_OVERLAP_THRESHOLD = float(os.environ.get("DESKMATE_CURATOR_OVERLAP", "0.5"))
# 2026-11-20：curator_report.md 停写（零内容读者，前端 SkillStore 走 HTTP 现场现算）。
# opt-in 归档仍要节流，改看轻量时间戳文件，不再依赖报告 mtime。
_RUN_MARKER = ".curator_last_run"


def auto_archive_enabled() -> bool:
    """curator 是否开启自动归档（可控开启，默认关）。

    通过环境变量 DESKMATE_CURATOR_AUTO_ARCHIVE 控制（值 1/true/yes/on 开启）。
    默认关闭，保持「只报告不清理」的非破坏性姿态；开启后仅归档严格低价值技能，
    且支持 skill_restore 一键还原，不丢信息。
    """
    val = os.environ.get("DESKMATE_CURATOR_AUTO_ARCHIVE", "false")
    return val.strip().lower() in ("1", "true", "yes", "on")


def _is_imported(path: str) -> bool:
    """导入的社区技能包（agentskills.io）路径标记：只读，不归档/不合并。"""
    return ".deskmate/skills/imported" in (path or "").replace("\\", "/")


def _skills_dir(base_dir: Path) -> Path:
    """解析技能库实际目录（与 SkillManager 一致，受 DESKMATE_SKILLS_DIR 影响）。"""
    try:
        from deskmate.skills.manager import SkillManager

        return Path(SkillManager(base_dir=base_dir).store.skills_dir)
    except Exception:
        return Path(base_dir) / "skills"


def _tokenize(text: str) -> set[str]:
    """中英文混合分词（与 SkillStore._tokenize 同思路）：英文 >=2 字符，中文 2-gram。"""
    text = (text or "").lower()
    toks: set[str] = set()
    for seg in re.split(r"[\s，。、？！,.?!\-()（）\[\]【】\"'':：；;]+", text):
        seg = seg.strip()
        if not seg:
            continue
        for w in re.findall(r"[a-z0-9]+", seg):
            if len(w) >= 2:
                toks.add(w)
        cjk = re.findall(r"[一-鿿]", seg)
        for i in range(len(cjk) - 1):
            toks.add(cjk[i] + cjk[i + 1])
    return toks


def _overlap(a: set[str], b: set[str]) -> float:
    """Jaccard 相似度。空集合返回 0。"""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _parse_date(s: str | None) -> float | None:
    """ISO 日期/时间字符串 → epoch；失败返回 None。"""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _should_run(base_dir: Path) -> bool:
    """距上次巡检超过间隔（默认 7 天）才运行，避免每轮都扫。

    2026-11-20 起不再依赖 curator_report.md 的 mtime（报告已停写）；
    改看独立时间戳文件，只在 opt-in 归档路径上生效。
    """
    marker = _skills_dir(base_dir) / _RUN_MARKER
    if not marker.is_file():
        return True
    try:
        age_days = (time.time() - marker.stat().st_mtime) / 86400.0
        return age_days >= _CURATOR_INTERVAL_DAYS
    except OSError:
        return True


def _mark_run(base_dir: Path) -> None:
    """记一次巡检时间（仅节流用，不承载任何内容）。"""
    try:
        marker = _skills_dir(base_dir) / _RUN_MARKER
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        logger.debug("curator: 写巡检时间戳失败", exc_info=True)


def curate_skills(base_dir: Path, *, auto_archive: bool = False) -> dict:
    """巡检技能库，返回候选数据（默认非破坏性；不再写报告文件）。

    Args:
        base_dir: 记忆根目录（默认 ~/.deskmate）。
        auto_archive: True 时仅归档「从未使用 + 陈旧 + 高重叠」的严格低价值技能；默认 False 只出报告。

    Returns:
        {"ok", "total", "merge_candidates", "archive_candidates", "auto_archive_candidates", "archived"}
    """
    try:
        from deskmate.skills.manager import SkillManager

        store = SkillManager(base_dir=base_dir).store
    except Exception as e:
        logger.warning("curator: 无法初始化技能库: %s", e)
        return {"ok": False, "reason": str(e)}

    skills = store.list_skills(limit=200)
    # 跳过导入的社区技能包（只读，不归档/不合并）
    skills = [s for s in skills if not _is_imported(s.get("path"))]

    for s in skills:
        blob = " ".join([
            s.get("name", ""), s.get("description", ""),
            s.get("trigger", ""), " ".join(s.get("tags") or []),
            (s.get("body") or "")[:1500],
        ])
        s["_tokens"] = _tokenize(blob)

    # ---- 重叠 / 合并候选（两两 Jaccard）----
    merge_candidates: list[dict] = []
    n = len(skills)
    for i in range(n):
        for j in range(i + 1, n):
            ov = _overlap(skills[i]["_tokens"], skills[j]["_tokens"])
            if ov >= _OVERLAP_THRESHOLD:
                merge_candidates.append({
                    "a": skills[i]["name"], "b": skills[j]["name"],
                    "similarity": round(ov, 3),
                    "a_path": skills[i]["path"], "b_path": skills[j]["path"],
                })

    # 出现在任意高重叠对中的技能名（即与别的技能功能高度重叠 → 冗余可归档）
    overlapping_names: set[str] = set()
    for m in merge_candidates:
        overlapping_names.add(m["a"])
        overlapping_names.add(m["b"])

    # ---- 陈旧 / 归档候选 ----
    archive_candidates: list[dict] = []
    now = time.time()
    for s in skills:
        if _is_imported(s.get("path")):
            continue
        uses = int(s.get("uses") or 0)
        last_used = _parse_date(str(s.get("last_used") or ""))
        mtime = now
        try:
            mtime = Path(s["path"]).stat().st_mtime
        except OSError:
            pass
        last_active = max(last_used or 0, mtime)
        stale_days = (now - last_active) / 86400.0 if last_active else 9999.0
        if uses == 0 and stale_days >= _CURATOR_STALE_DAYS:
            archive_candidates.append({
                "name": s["name"], "uses": uses,
                "stale_days": int(stale_days), "path": s["path"],
            })

    # ---- 执行（可控开启时仅归档「严格低价值」技能：从未使用 + 陈旧 + 高重叠）----
    # archive_candidates 是「从未使用且陈旧」的宽候选（仅供报告/人工确认）；
    # 自动归档只针对其中还与别的技能高重叠的——才是真正冗余、可安全归档的。
    auto_archive_candidates = [c for c in archive_candidates if c["name"] in overlapping_names]
    archived: list[str] = []
    if auto_archive:
        for c in auto_archive_candidates:
            try:
                res = store.archive_skill(c["path"])
                if res.get("status") == "ok":
                    archived.append(c["name"])
            except Exception as e:
                logger.debug("curator: 归档 %s 失败: %s", c["name"], e)

    # 2026-11-20：不再写 curator_report.md；候选数据由 HTTP 端点现场现算返回，
    # 前端 SkillStore 直接消费。这里只留时间戳供 opt-in 归档节流。
    _mark_run(base_dir)
    return {
        "ok": True,
        "total": n,
        "merge_candidates": merge_candidates,
        "archive_candidates": archive_candidates,
        "auto_archive_candidates": auto_archive_candidates,
        "archived": archived,
    }


__all__ = ["curate_skills", "_should_run", "_mark_run"]
