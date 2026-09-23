"""P1-3 测试：auto-task 用户可配置（enabled 开关 + 接通短路 + 列表）。

hermetic：全部用临时目录，不触碰 ~/.deskmate。
"""

import os
import shutil
import tempfile
from pathlib import Path

import deskmate.auto_tasks.registry as _reg_mod
import deskmate.auto_tasks.recorder as _rec_mod
from deskmate.auto_tasks.models import Insight, TaskRegistryEntry
from deskmate.auto_tasks.recorder import TaskRecorder
from deskmate.auto_tasks.registry import TaskRegistry


class TestP1AutoTasks:
    def setup_method(self):
        # 重置全局单例，避免跨测试串数据（get_registry/get_recorder 按 base_dir 缓存）
        _reg_mod._default_registry = None
        _rec_mod._recorders = {}
        self.tmp = Path(tempfile.mkdtemp())
        self.registry = TaskRegistry(self.tmp)
        self.recorder = TaskRecorder(self.tmp)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- set_enabled 持久化 ----
    def test_set_enabled_persists_and_reloads(self):
        self.registry.register(
            TaskRegistryEntry(task_type="daily_reading", task_name="每日读书", category="writing", enabled=True)
        )
        assert self.registry.set_enabled("daily_reading", False) is True
        reloaded = TaskRegistry(self.tmp)
        assert reloaded.get("daily_reading").enabled is False
        # 未知任务返回 False
        assert self.registry.set_enabled("does_not_exist", True) is False

    # ---- 接通短路：已禁用任务跳过沉淀，默认任务行为不变 ----
    def test_record_skipped_when_disabled(self):
        self.registry.register(
            TaskRegistryEntry(task_type="daily_reading", task_name="每日读书", category="writing", enabled=True)
        )
        self.registry.set_enabled("daily_reading", False)
        before = len(self.recorder.get_timeline())
        rec = self.recorder.record("daily_reading", target="某书", summary="应被跳过")
        assert rec is None
        after = len(self.recorder.get_timeline())
        assert after == before
        # enabled 不被自动登记重置
        assert self.registry.get("daily_reading").enabled is False

    def test_record_normal_when_enabled(self):
        self.registry.register(
            TaskRegistryEntry(task_type="daily_reading", task_name="每日读书", category="writing", enabled=True)
        )
        rec = self.recorder.record(
            "daily_reading",
            target="某书",
            summary="正常沉淀",
            insights=[Insight(text="风格 A", tags=["风格"], push_to_vault=False)],
        )
        assert rec is not None
        assert len(self.recorder.get_timeline()) >= 1
        # 默认任务 enabled 始终 True，不被触碰
        assert self.registry.get("daily_reading").enabled is True

    # ---- 列表接口：任务 + 最近执行 ----
    def test_list_tasks_with_recent(self):
        self.registry.register(
            TaskRegistryEntry(
                task_type="daily_reading", task_name="每日读书", category="writing",
                enabled=True, description="每天读书沉淀",
            )
        )
        self.recorder.record("daily_reading", target="书1", summary="摘要一")
        self.recorder.record("daily_reading", target="书2", summary="摘要二")
        tasks = self.recorder.list_tasks_with_recent(recent_limit=3)
        assert len(tasks) == 1
        t = tasks[0]
        assert t["task_type"] == "daily_reading"
        assert t["enabled"] is True
        assert t["description"] == "每天读书沉淀"
        assert len(t["recent"]) == 2
        assert t["recent"][0]["summary"] == "摘要二"  # 倒序：最近在前
