"""技能库自整理（curator）—— 对标 Hermes agent/curator.py。

周期性在后台运行：扫描技能库，发现重复/重叠候选 + 长期未用技能，
主动把合并候选与归档候选写入报告（默认非破坏性，绝不自动合并/删除以免丢信息）；
设 auto_archive=True 时仅归档「从未使用、陈旧、无关联」的低价值技能。

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
_REPORT_FILE = "curator_report.md"


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
    """距上次巡检超过间隔（默认 7 天）或报告缺失才运行，避免每轮都扫。"""
    report = _skills_dir(base_dir) / _REPORT_FILE
    if not report.is_file():
        return True
    try:
        age_days = (time.time() - report.stat().st_mtime) / 86400.0
        return age_days >= _CURATOR_INTERVAL_DAYS
    except OSError:
        return True


def curate_skills(base_dir: Path, *, auto_archive: bool = False) -> dict:
    """巡检技能库，产出整理报告（默认非破坏性）。

    Args:
        base_dir: 记忆根目录（默认 ~/.deskmate）。
        auto_archive: True 时仅归档严格低价值技能（见模块说明）；默认 False 只出报告。

    Returns:
        {"ok", "total", "merge_candidates", "archive_candidates", "archived", "report_path"}
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

    # ---- 执行（默认非破坏性：仅归档严格低价值技能）----
    archived: list[str] = []
    if auto_archive:
        for c in archive_candidates:
            try:
                res = store.archive_skill(c["path"])
                if res.get("status") == "ok":
                    archived.append(c["name"])
            except Exception as e:
                logger.debug("curator: 归档 %s 失败: %s", c["name"], e)

    report_path = _write_report(
        store.skills_dir, skills, merge_candidates, archive_candidates, archived, auto_archive
    )
    return {
        "ok": True,
        "total": n,
        "merge_candidates": merge_candidates,
        "archive_candidates": archive_candidates,
        "archived": archived,
        "report_path": str(report_path),
    }


def _write_report(
    skills_dir: Path,
    skills: list[dict],
    merge_candidates: list[dict],
    archive_candidates: list[dict],
    archived: list[str],
    auto_archive: bool,
) -> Path:
    """写出巡检报告（原子写）；返回报告路径。"""
    report = Path(skills_dir) / _REPORT_FILE
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    mode = "报告 + 自动归档" if auto_archive else "报告（非破坏性，待你确认）"
    lines = [
        "# 技能库巡检报告 · %s" % stamp,
        "",
        "共扫描 %d 个技能（导入包已跳过）。" % len(skills),
        "模式：%s。" % mode,
        "",
    ]
    if merge_candidates:
        lines.append("## 建议合并（功能重叠，需你确认后执行）")
        for c in merge_candidates:
            lines.append("- `%s` ↔ `%s`（相似度 %.2f）" % (c["a"], c["b"], c["similarity"]))
        lines.append("")
    if archive_candidates:
        lines.append("## 建议归档（从未使用且陈旧）")
        for c in archive_candidates:
            mark = "✅ 已归档" if c["name"] in archived else "⏳ 待确认"
            lines.append("- `%s`（%d 天未用）%s" % (c["name"], c["stale_days"], mark))
        lines.append("")
    if not merge_candidates and not archive_candidates:
        lines.append("## 结论")
        lines.append("技能库整洁，暂无需整理。")
        lines.append("")
    lines.append("---")
    lines.append(
        "合并需你确认后再执行（自动合并可能丢信息）；归档候选若为手写的低频技能，请勿自动归档。"
    )
    content = "\n".join(lines)
    try:
        report.parent.mkdir(parents=True, exist_ok=True)
        tmp = report.with_name(report.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, report)
    except OSError:
        logger.exception("curator: 写报告失败")
    return report


__all__ = ["curate_skills", "_should_run"]
