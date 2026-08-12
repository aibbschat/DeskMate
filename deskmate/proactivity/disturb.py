"""防打扰机制 — 避免重复建议、用户忽略后降低优先级。"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SuggestionRecord:
    """一次建议记录。"""
    suggestion_type: str       # 建议类型（如 "plot_contradiction", "missing_foreshadow"）
    suggestion_key: str        # 建议的唯一标识（简化内容，用于去重）
    timestamp: float           # 时间戳
    accepted: bool | None      # None=未回应, True=接受, False=忽略


@dataclass
class DisturbManager:
    """防打扰管理器。

    规则（参考技术方案）：
    - 同一类型建议在 30 分钟内不重复
    - 用户连续忽略 3 次同类建议后，降低该类建议的优先级
    - 用户明确说"不要打扰我"时，进入静默模式
    """
    records: list[SuggestionRecord] = field(default_factory=list)
    silent_mode: bool = False
    persist_path: Path | None = None

    # 配置
    cooldown_seconds: int = 1800       # 30 分钟冷却
    ignore_threshold: int = 3           # 连续忽略次数阈值
    max_records: int = 200              # 最多保留记录数

    def should_allow(self, suggestion_type: str, suggestion_key: str) -> bool:
        """判断是否允许发送此建议。

        Returns:
            True = 允许，False = 应被拦截
        """
        # 静默模式：拒绝一切
        if self.silent_mode:
            return False

        now = time.time()

        # 1) 同类建议的冷却检查。
        #    只在「连续忽略 ≥ 阈值」时拦截（第 3 步处理）。
        #    单次忽略不拦截：用户可能只是不喜欢那条建议，不代表不想再看到同类建议。
        #    精确 key 匹配的冷却在第 2 步处理。

        # 2) 检查完全相同的建议（30 分钟内，额外兜底）
        for rec in reversed(self.records):
            if rec.suggestion_key == suggestion_key and (now - rec.timestamp) < self.cooldown_seconds:
                return False

        # 3) 检查同类型建议的连续忽略次数
        consecutive_ignores = 0
        for rec in reversed(self.records):
            if rec.suggestion_type != suggestion_type:
                break
            if rec.accepted is False:
                consecutive_ignores += 1
            else:
                break

        if consecutive_ignores >= self.ignore_threshold:
            return False

        return True

    def record_suggestion(
        self,
        suggestion_type: str,
        suggestion_key: str,
        accepted: bool | None = None,
    ) -> None:
        """记录一次建议。"""
        rec = SuggestionRecord(
            suggestion_type=suggestion_type,
            suggestion_key=suggestion_key,
            timestamp=time.time(),
            accepted=accepted,
        )
        self.records.append(rec)
        # 超过上限，删除最旧的
        if len(self.records) > self.max_records:
            self.records = self.records[-self.max_records:]
        self._persist()

    def record_feedback(self, suggestion_key: str, accepted: bool) -> None:
        """记录用户对建议的反馈（接受/忽略）。

        从后往前找最近的、key 完全匹配且尚未回应的记录。
        如果精确 key 没匹配到（LLM 每次生成文本不同），回退到
        更新同类型中最近的未回应记录。
        """
        # 1. 精确 key 匹配
        for rec in reversed(self.records):
            if rec.suggestion_key == suggestion_key and rec.accepted is None:
                rec.accepted = accepted
                self._persist()
                return
        # 2. 回退：同类型中最近的未回应记录
        #    （suggestion_key 可能因 LLM 生成不同文本而不匹配，
        #    但用户是对某个类型的建议做的反馈）
        for rec in reversed(self.records):
            if rec.accepted is None:
                rec.accepted = accepted
                self._persist()
                return

    def set_silent_mode(self, silent: bool) -> None:
        """设置静默模式。"""
        self.silent_mode = silent
        self._persist()

    def get_stats(self) -> dict:
        """获取防打扰统计。"""
        now = time.time()
        recent = [r for r in self.records if (now - r.timestamp) < 3600]
        return {
            "total_records": len(self.records),
            "recent_1h": len(recent),
            "silent_mode": self.silent_mode,
            "recent_accepted": sum(1 for r in recent if r.accepted is True),
            "recent_ignored": sum(1 for r in recent if r.accepted is False),
        }

    def persist(self, path: Path) -> None:
        """加载/保存到文件。"""
        self.persist_path = path
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.silent_mode = data.get("silent_mode", False)
                self.records = [
                    SuggestionRecord(**r) for r in data.get("records", [])
                ]
            except (json.JSONDecodeError, TypeError):
                pass

    def _persist(self) -> None:
        """保存到文件（原子写：临时文件 + os.replace，避免并发读到半截文件）。"""
        if not self.persist_path:
            return
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "silent_mode": self.silent_mode,
                "records": [
                    {
                        "suggestion_type": r.suggestion_type,
                        "suggestion_key": r.suggestion_key,
                        "timestamp": r.timestamp,
                        "accepted": r.accepted,
                    }
                    for r in self.records
                ],
            }
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            tmp = self.persist_path.with_suffix(
                self.persist_path.suffix + ".tmp"
            )
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.persist_path)
        except OSError:
            pass
