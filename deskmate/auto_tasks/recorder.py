"""统一沉淀 API：任务执行完调用一次，自动落地+分类+推记忆。"""

from __future__ import annotations

import json
import os
from datetime import date as _date
from pathlib import Path
from typing import Optional

from .models import TaskRecord, Insight, TaskRegistryEntry
from .registry import get_registry


class TaskRecorder:
    """统一沉淀器。所有自动任务的结果都通过这里落地。"""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.timeline_file = self.base_dir / "timeline.jsonl"
        self.insights_dir = self.base_dir / "insights"
        self.details_dir = self.base_dir / "details"
        self._ensure_dirs()

    def _ensure_dirs(self):
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.insights_dir.mkdir(exist_ok=True)
        self.details_dir.mkdir(exist_ok=True)

    def record(
        self,
        task_type: str,
        target: str,
        action: str = "finish",
        summary: str = "",
        insights: Optional[list[Insight | dict]] = None,
        tags: Optional[list[str]] = None,
        detail_content: Optional[str] = None,
        detail_filename: Optional[str] = None,
        extra: Optional[dict] = None,
        task_name: Optional[str] = None,
        extract_cognitive: bool = False,
    ) -> TaskRecord:
        """
        记录一次任务执行的沉淀。自动做：
          1. 写 timeline.jsonl
          2. 写 insights/<type>/<date>.jsonl
          3. 写 details/<type>/<filename>.md（如果有 detail_content）
          4. 更新注册表 run_count / last_run_at
          5. 标记 push_to_vault / push_to_style 的洞察（由后续记忆流程处理）
          6. 如果 extract_cognitive=True 且有 detail_content，自动触发认知提取（从文本中提取立场/论证/价值观/思维框架，增量更新认知画像）

        Args:
            extract_cognitive: 是否自动触发认知提取。适用于每日读书、深度对话等包含用户思想的文本。
        """
        registry = get_registry(self.base_dir)
        entry = registry.get(task_type)
        if entry is None:
            # 未登记的任务，自动登记到 other 分类
            entry = registry.register(TaskRegistryEntry(
                task_type=task_type,
                task_name=task_name or task_type,
                category="other",
                description="自动登记（未显式注册）",
                created_at=_date.today().isoformat(),
            ))

        today = _date.today().isoformat()
        insight_objs = [
            i if isinstance(i, Insight) else Insight(**i)
            for i in (insights or [])
        ]

        # 3. 写详情文件
        detail_file = None
        if detail_content:
            type_details_dir = self.details_dir / task_type
            type_details_dir.mkdir(exist_ok=True)
            fname = detail_filename or f"{today}_{target[:30]}.md"
            fpath = type_details_dir / fname
            fpath.write_text(detail_content, encoding="utf-8")
            detail_file = f"{task_type}/{fname}"

        record = TaskRecord(
            task_type=task_type,
            task_name=entry.task_name,
            target=target,
            action=action,
            date=today,
            summary=summary,
            insights=insight_objs,
            tags=tags or [],
            detail_file=detail_file,
            extra=extra or {},
        )

        # 1. 写 timeline
        with open(self.timeline_file, "a", encoding="utf-8") as f:
            f.write(record.to_jsonl() + "\n")

        # 2. 写洞察文件
        if insight_objs:
            type_insights_dir = self.insights_dir / task_type
            type_insights_dir.mkdir(exist_ok=True)
            insights_file = type_insights_dir / f"{today}.jsonl"
            with open(insights_file, "a", encoding="utf-8") as f:
                for ins in insight_objs:
                    f.write(json.dumps(ins.__dict__, ensure_ascii=False) + "\n")

        # 4. 更新注册表
        registry.touch_run(task_type, today)

        # 5. 推入 vault / style_profile（标记，由记忆整理流程异步处理）
        self._mark_for_memory_push(insight_objs, task_type, target, today)

        # 6. 自动触发认知提取（如果指定且有 detail_content）
        if extract_cognitive and detail_content:
            try:
                from deskmate.cognitive import get_extractor
                get_extractor().extract_from_text(
                    text=detail_content,
                    source_title=target,
                    source_type=task_type,
                    source_date=today,
                )
            except Exception as e:
                logger.warning(f"Auto cognitive extraction failed: {e}")

        return record

    def _mark_for_memory_push(self, insights: list[Insight], task_type: str, target: str, date: str):
        """标记需要推入长期记忆的洞察，写入 pending 队列。"""
        pushable = [i for i in insights if i.push_to_vault or i.push_to_style]
        if not pushable:
            return
        pending_file = self.base_dir / "pending_memory_push.jsonl"
        with open(pending_file, "a", encoding="utf-8") as f:
            for ins in pushable:
                f.write(json.dumps({
                    "task_type": task_type,
                    "target": target,
                    "date": date,
                    "insight": ins.__dict__,
                }, ensure_ascii=False) + "\n")

    def get_timeline(self, limit: int = 50, task_type: Optional[str] = None) -> list[TaskRecord]:
        """读取时间线，倒序返回。"""
        if not self.timeline_file.exists():
            return []
        records = []
        with open(self.timeline_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = TaskRecord.from_jsonl(line)
                    if task_type is None or rec.task_type == task_type:
                        records.append(rec)
                except Exception:
                    continue
        records.sort(key=lambda r: r.ts, reverse=True)
        return records[:limit]

    def get_stats(self) -> dict:
        """汇总统计。"""
        records = self.get_timeline(limit=10000)
        registry = get_registry(self.base_dir)
        by_type: dict[str, int] = {}
        by_category: dict[str, int] = {}
        targets = set()
        for r in records:
            by_type[r.task_type] = by_type.get(r.task_type, 0) + 1
            entry = registry.get(r.task_type)
            cat = entry.category if entry else "other"
            by_category[cat] = by_category.get(cat, 0) + 1
            targets.add(r.target)
        return {
            "total_records": len(records),
            "by_type": by_type,
            "by_category": by_category,
            "unique_targets": len(targets),
            "tasks": [e.__dict__ for e in registry.all()],
            "categories": registry.categories(),
        }

    def get_insights(self, task_type: Optional[str] = None, limit: int = 50) -> list[dict]:
        """读取洞察。"""
        result = []
        search_dirs = [self.insights_dir / task_type] if task_type else list(self.insights_dir.iterdir())
        for d in search_dirs:
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.jsonl"), reverse=True):
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            try:
                                result.append(json.loads(line))
                            except Exception:
                                continue
                if len(result) >= limit:
                    return result[:limit]
        return result[:limit]


# 全局单例
_default_recorder: Optional[TaskRecorder] = None


def get_recorder(base_dir: Optional[str | Path] = None) -> TaskRecorder:
    global _default_recorder
    if _default_recorder is None:
        base_dir = base_dir or os.path.join(os.path.expanduser("~"), ".deskmate", "auto_tasks")
        _default_recorder = TaskRecorder(base_dir)
    return _default_recorder


def record_task_result(
    task_type: str,
    target: str,
    action: str = "finish",
    summary: str = "",
    insights: Optional[list[Insight | dict]] = None,
    tags: Optional[list[str]] = None,
    detail_content: Optional[str] = None,
    detail_filename: Optional[str] = None,
    extra: Optional[dict] = None,
    task_name: Optional[str] = None,
    extract_cognitive: bool = False,
) -> TaskRecord:
    """便捷函数：记录一次任务执行的沉淀。"""
    return get_recorder().record(
        task_type=task_type,
        target=target,
        action=action,
        summary=summary,
        insights=insights,
        tags=tags,
        detail_content=detail_content,
        detail_filename=detail_filename,
        extra=extra,
        task_name=task_name,
        extract_cognitive=extract_cognitive,
    )
