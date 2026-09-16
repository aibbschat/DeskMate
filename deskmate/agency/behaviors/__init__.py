"""L2 自主动作行为实现。"""

from __future__ import annotations

from .world_ingestion import run_world_ingestion
from .worldview_reflection import run_worldview_reflection
from .ai_circle_ingestion import run_ai_circle_ingestion, record_ai_circle_observation

__all__ = [
    "run_world_ingestion",
    "run_worldview_reflection",
    "run_ai_circle_ingestion",
    "record_ai_circle_observation",
]
