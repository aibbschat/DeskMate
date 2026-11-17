"""后台心跳可见性——把「她最近一次真的思考了」落成一个可直读的小 JSON。

为什么单独一个模块（2026-11-17，萧哥）：
后台主动推理默认常开、30s 一跳，但它**没有可见的出口**。模型挂了、通道熔断、
热更新把 tick 弄断了，界面照常，用户只会觉得「她最近有点安静」——这正是本仓反复
踩的「静默退化」形态（PITFALLS 里访谈断粮、情感层失效都是这一类）。所以把
「上次 tick / 上次真的调了 LLM / 今日次数 / 当前模型」摊到设置页，一眼能看见。

边界：
- 只记时间戳与计数，**不碰任何业务状态**，读的写的不影响 tick 行为；
- 原子写（tmp + replace），任何异常静默吞掉——心跳本身绝不许拖累主流程；
- 路径走调用方给的 base_dir（tick 的 workspace 口径），未给时回落
  :func:`deskmate.paths.deskmate_base_dir`，不用模块级常量（测试隔离红线）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_FILENAME = "proactive_heartbeat.json"


def _path(base_dir: str | Path | None = None) -> Path:
    if base_dir is None:
        from deskmate.paths import deskmate_base_dir

        return deskmate_base_dir() / _FILENAME
    return Path(base_dir) / _FILENAME


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — 缺文件/坏文件都当作首次
        return {}


def _merge(base_dir: str | Path | None, **fields: Any) -> None:
    path = _path(base_dir)
    data = _read(path)
    data.update(fields)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — 心跳写失败不配影响她思考
        pass


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def record_tick(
    base_dir: str | Path | None,
    *,
    skipped_reason: str = "",
    acted: bool = False,
    model: str = "",
) -> None:
    """每轮后台 tick 调一次（无论是否真的思考）——它回答「她还活着吗」。"""
    now = time.time()
    day = _today()
    data = _read(_path(base_dir))
    ticks = int(data.get("ticks_today") or 0) + 1 if data.get("ticks_date") == day else 1
    _merge(
        base_dir,
        last_tick_at=now,
        ticks_date=day,
        ticks_today=ticks,
        last_skip_reason=skipped_reason,
        last_acted=bool(acted),
        model=model or data.get("model", ""),
    )


def record_judge(
    base_dir: str | Path | None,
    *,
    model: str = "",
    fingerprint: str = "",
) -> None:
    """judge 真的调了一次 LLM 才记——它回答「她还在思考吗」（区别于 tick 空转）。

    ``fingerprint`` 一并落盘：前台 tick 与后台 scheduler 各自持有 engine 实例，
    共享这份指纹才能把「同一份输入」的去重做成全局的，而不是两个引擎各判一遍。
    """
    now = time.time()
    day = _today()
    data = _read(_path(base_dir))
    calls = (
        int(data.get("judge_calls_today") or 0) + 1
        if data.get("judge_date") == day
        else 1
    )
    fields: dict[str, Any] = dict(
        last_judge_at=now,
        judge_date=day,
        judge_calls_today=calls,
        model=model or data.get("model", ""),
    )
    if fingerprint:
        fields["last_judge_fp"] = fingerprint
    _merge(base_dir, **fields)


def read_heartbeat(base_dir: str | Path | None = None) -> dict[str, Any]:
    """读给设置页：原始时间戳 + 便于渲染的 age 秒数（None = 从未发生）。"""
    data = _read(_path(base_dir))
    now = time.time()
    for src, dst in (
        ("last_tick_at", "tick_age_seconds"),
        ("last_judge_at", "judge_age_seconds"),
    ):
        value = data.get(src)
        try:
            data[dst] = max(0, int(now - float(value))) if value else None
        except (TypeError, ValueError):
            data[dst] = None
    return data
