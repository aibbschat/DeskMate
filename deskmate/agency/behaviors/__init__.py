"""L2 自主动作行为实现。"""

from __future__ import annotations

from .world_ingestion import run_world_ingestion
from .worldview_reflection import run_worldview_reflection

__all__ = ["run_world_ingestion", "run_worldview_reflection"]
