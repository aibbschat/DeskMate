"""GardenState —— 花园状态的持久化。

存储位置（与 growth 同一收口语义）：
- 默认目录（``None`` 或解析等于 ``~/.deskmate``）：统一库 ``deskmate.db`` 的
  ``json_blobs`` 表（``KEY_GARDEN``），与 growth / emotion 同为物理原子单元。
- 显式「非默认」目录（测试隔离 / 独立项目）：文件 ``<base>/garden.json`` 原子写。

设计要点：
- **字段级容错**（复刻 ``growth/state.py`` 的 ``_num`` 模式）：单个字段坏掉只影响
  它自己，不牵连整份花园状态——花朵是用户长期攒下的，不能因一次坏数据全丢。
- **fail-open**：读取失败返回默认空花园，绝不阻断个人中心。
- **数据本地化**：花园是设备本地私密层，不随 style/decision 派生包跨端同步。
"""

from __future__ import annotations

import datetime
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from deskmate.persistence import BlobStore


def _default_base_dir() -> Path:
    return Path(os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate")))


def _is_default_dir(base_dir: Path | str | None) -> bool:
    """默认目录（None 或解析后等于 ~/.deskmate）→ 走 BlobStore；否则走文件。"""
    if base_dir is None:
        return True
    try:
        return (
            Path(base_dir).expanduser().resolve()
            == _default_base_dir().expanduser().resolve()
        )
    except (OSError, RuntimeError):
        return False


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class FlowerRecord:
    """一朵花 = 一次「值得记的事」的可视化沉淀。

    - flower_id: 稳定标识 ``{kind}:{fingerprint}``，幂等（同事件不重复开花）。
    - quote: 事件原文摘录，**必须来自真实事件**，禁止模型编造。
    - title: 零 LLM 模板渲染的短标题。
    - echo: 小桔在花开处的「回响」——零 LLM 模板短评（P1 花园回响）。
    """

    flower_id: str
    kind: str            # 花种 id（勿忘草/木槿/勋章花/向日葵…）
    bed: str             # 花圃：memory/diary/badge/milestone
    title: str
    quote: str
    date: str            # 开花日期 YYYY-MM-DD
    source_ts: str = ""  # 原始事件时间戳
    mood_tint: str = ""  # 情绪倾向（可选，预留）
    echo: str = ""       # 小桔的回响（P1）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GardenState:
    """花园整体状态。"""

    version: int = 1
    last_visit_date: str = ""          # YYYY-MM-DD（归来计算：距此 >= 3 天）
    visit_count: int = 0               # 累计来访
    season_started_at: str = ""        # 季节起始证据（供统计，非渲染依赖）
    flowers: dict[str, dict] = field(default_factory=dict)   # flower_id -> FlowerRecord dict
    garden_log: list[dict] = field(default_factory=list)     # 花园痕迹（cap 200）
    clock_log: dict[str, str] = field(default_factory=dict)  # 幂等表：event_key -> iso
    water_log: dict[str, str] = field(default_factory=dict)  # 每日浇水幂等表：date -> iso（P2）
    year_flowers: dict[str, dict] = field(default_factory=dict)  # 年花（P2）：year -> {year, first_visit_date}
    lit_flowers: dict[str, str] = field(default_factory=dict)    # 被点亮的花（P2）：flower_id -> iso
    archive: list[dict] = field(default_factory=list)        # 花园年鉴（P2）：超限归档的旧花（cap 500）
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "last_visit_date": self.last_visit_date,
            "visit_count": self.visit_count,
            "season_started_at": self.season_started_at,
            "flowers": dict(self.flowers),
            "garden_log": self.garden_log[-200:],
            "clock_log": dict(self.clock_log),
            "water_log": dict(self.water_log),
            "year_flowers": dict(self.year_flowers),
            "lit_flowers": dict(self.lit_flowers),
            "archive": self.archive[-500:],
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# 读写（字段级容错）
# ---------------------------------------------------------------------------


def _num(data: dict, key: str, default, cast):
    try:
        v = data.get(key, default)
        return cast(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _flowers_of(data: dict) -> dict[str, dict]:
    """逐朵容错：坏花跳过，不牵连整份花园。"""
    raw = data.get("flowers")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in raw.items():
        if not k or not isinstance(v, dict):
            continue
        out[str(k)] = {
            "flower_id": str(v.get("flower_id", "") or k),
            "kind": str(v.get("kind", "") or ""),
            "bed": str(v.get("bed", "") or "memory"),
            "title": str(v.get("title", "") or ""),
            "quote": str(v.get("quote", "") or ""),
            "date": str(v.get("date", "") or ""),
            "source_ts": str(v.get("source_ts", "") or ""),
            "mood_tint": str(v.get("mood_tint", "") or ""),
            "echo": str(v.get("echo", "") or ""),
        }
    return out


def _logs_of(data: dict) -> list[dict]:
    raw = data.get("garden_log")
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)][-200:]


def _clock_of(data: dict) -> dict[str, str]:
    raw = data.get("clock_log")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if k}


def _water_of(data: dict) -> dict[str, str]:
    raw = data.get("water_log")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if k}


def _years_of(data: dict) -> dict[str, dict]:
    raw = data.get("year_flowers")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in raw.items():
        if not k or not isinstance(v, dict):
            continue
        out[str(k)] = {
            "year": str(v.get("year", "") or k),
            "first_visit_date": str(v.get("first_visit_date", "") or ""),
        }
    return out


def _lit_of(data: dict) -> dict[str, str]:
    raw = data.get("lit_flowers")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if k}


def _archive_of(data: dict) -> list[dict]:
    raw = data.get("archive")
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)][-500:]


def load_garden(base_dir: Path | str | None = None) -> GardenState:
    """加载花园状态；不存在或损坏时返回默认空花园（不抛异常）。"""
    data = _load_raw(base_dir)
    if not isinstance(data, dict):
        return GardenState()
    return _build_garden(data)


def _load_raw(base_dir: Path | str | None) -> dict | None:
    if _is_default_dir(base_dir):
        return BlobStore().get(BlobStore.KEY_GARDEN)
    path = Path(base_dir) / "garden.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _build_garden(data: dict) -> GardenState:
    st = GardenState(
        version=_num(data, "version", 1, int),
        last_visit_date=str(data.get("last_visit_date", "") or ""),
        visit_count=_num(data, "visit_count", 0, int),
        season_started_at=str(data.get("season_started_at", "") or ""),
        updated_at=str(data.get("updated_at", "") or ""),
    )
    st.flowers = _flowers_of(data)
    st.garden_log = _logs_of(data)
    st.clock_log = _clock_of(data)
    st.water_log = _water_of(data)
    st.year_flowers = _years_of(data)
    st.lit_flowers = _lit_of(data)
    st.archive = _archive_of(data)
    return st


def save_garden(state: GardenState, base_dir: Path | str | None = None) -> None:
    """保存花园状态。写盘失败不抛异常（花园不该让主流程崩），留一条 warning。"""
    if _is_default_dir(base_dir):
        state.updated_at = _now()
        BlobStore().set(BlobStore.KEY_GARDEN, state.to_dict())
        return
    path = Path(base_dir) / "garden.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        state.updated_at = _now()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        import logging

        logging.warning("garden state 写盘失败（花开记录可能未保存）")
