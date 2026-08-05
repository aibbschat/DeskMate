"""创作圆桌（有界多轮群聊）模块。

用法：
    from deskmate.roundtable import Roundtable, PeerSpec, run_roundtable_sync

    result = run_roundtable_sync(
        "你的创作话题……",
        peers=[PeerSpec("Hermes", "http://localhost:9900")],
        max_rounds=2,
    )
    print(result.to_markdown())
"""
from __future__ import annotations

from deskmate.roundtable.coordinator import (
    PeerSpec,
    Roundtable,
    RoundtableResult,
    Turn,
    run_roundtable,
    run_roundtable_sync,
)
from deskmate.roundtable.personas import PERSONAS, Persona

__all__ = [
    "PeerSpec",
    "Roundtable",
    "RoundtableResult",
    "Turn",
    "run_roundtable",
    "run_roundtable_sync",
    "PERSONAS",
    "Persona",
]
