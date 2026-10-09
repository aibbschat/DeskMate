#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端小桔 L2 自主层常驻 tick 触发器 —— 路径乙落地核心。

设计意图（2026-10-05 萧哥拍板）：云端不是本地的镜像，云端也要是「活的自主副本」。
本脚本由 systemd timer 每小时触发一次（与 sync 窗口 :02 错峰到 :30），调用
AgencyEngine.tick() 让云端小桔自己跑自主行为（看世界 / 观察日记 / 世界观回望 /
萧盛影响摄入 / AI圈摄入），产出写入云端副本（task_log.jsonl + deskmate.db 业务表），
被 oplog 自然捕获后回灌 Mac 本地大脑（设计文档 §三 C 类「云端流回灌」）。

写饥饿处理：本脚本与 sync daemon 错峰（:30 vs :02），且 agency 各行为自带冷却、
低频写，配合 WAL 模式 + fail-open（撞锁/异常一律返回 None 不写），写饥饿被自然吸收。
若实测出现 database is locked 频繁，再给业务 store 加 PRAGMA busy_timeout=5000（一行）。

fail-open：任何异常都不影响、不阻断主流程、不抛给 systemd。
红线：不读不写含 API key 的 settings.json 值；只通过 load/save_agency_settings 操作
独立的 l2_settings.json（不含 key）。

注意（2026-10-05 实测）：云端 /opt/openworker 的 settings.py 是旧版，默认
DEFAULTS["enabled"]=False（阶段5 整包对齐时该文件未覆盖）。因此本脚本在「l2 配置文件
尚不存在且默认关闭」时显式创建开启配置，让路径乙生效；若你后来显式把 l2_settings.json
设 enabled=false，则尊重不强制开。
"""
from __future__ import annotations

import os
import sys
import json
import logging
from pathlib import Path

DESKMATE_DIR = os.environ.get("DESKMATE_DIR", "/var/lib/openworker/.deskmate")
LOG_PATH = Path(DESKMATE_DIR) / "agency" / "xiaoju_agency_tick.log"
# 与 deskmate/agency/settings.py::_default_path 保持一致：l2_settings.json 直接在
# DESKMATE_DIR 下，无 agency/ 子目录（旧代码多拼一层 agency/ 会指向不存在的路径，
# 导致 _ensure_l2_scope 的 file_existed 恒为 False）。
L2_PATH = Path(DESKMATE_DIR) / "l2_settings.json"

# 发声类行为（经 deskmate/connectors/aicircle.py stdio 调 ai-circle MCP server，
# 与对话轮共用 ~/.config/coworker/mcp.json 同一份配置）。仅在「云端未配 AI圈 连接器」时
# 才从白名单移除，避免无效尝试与潜在出站；配好则保留，让小桔自主发帖/回信/写手记。
_DISABLE_WITHOUT_MCP = ("ai_circle_share", "ai_circle_column", "ai_circle_reply")


def _ai_circle_available() -> bool:
    """检测云端 AI圈 连接器是否可用（与对话轮共用 ~/.config/coworker/mcp.json）。

    自主发声行为（share/column/reply）经 deskmate/connectors/aicircle.py 通过 stdio 拉起
    ai-circle MCP server；该配置与对话轮同一份。配好（文件存在 + ai-circle 启用 + 有 command）
    则返回 True，让其自主发声；未配则回落移除，避免 fail-open 空跑与潜在出站。
    全部异常回落 False（保守，等同「未配」）。
    """
    try:
        cfg = Path.home() / ".config" / "coworker" / "mcp.json"
        if not cfg.is_file():
            return False
        data = json.loads(cfg.read_text(encoding="utf-8"))
        srv = (data.get("mcpServers") or {}).get("ai-circle")
        if not srv:
            return False
        if not srv.get("enabled", True):
            return False
        if not srv.get("command"):
            return False
        return True
    except Exception:
        return False


def _ensure_l2_scope() -> None:
    """确保云端 L2 处于「开启 + 去发声类」状态。

    - l2 配置文件不存在且默认关闭（云端旧版代码）：显式创建开启配置（路径乙生效）。
    - l2 配置文件已存在：尊重其内容；仅当开启且含发声类时移除发声类。
    - 你若显式设 enabled=false：尊重，不强制开。
    """
    try:
        from deskmate.agency.settings import (
            load_agency_settings,
            save_agency_settings,
            AgencySettings,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("无法加载 L2 配置模块（沿用默认）: %s", e)
        return
    try:
        file_existed = L2_PATH.exists()
        s = load_agency_settings(DESKMATE_DIR)
        changed = False
        # 云端旧版代码默认关 L2 且配置缺失：路径乙要开，显式开启。
        if not file_existed and not s.enabled:
            s.enabled = True
            changed = True
            logging.info("云端 L2 默认关闭（旧版代码）-> 路径乙显式开启")
        # 开启状态下，按「云端是否配好 AI圈 连接器」动态决定发声类去留：
        # 配好则保留（小桔自主发帖/回信/写手记），未配则移除（避免无效尝试与潜在出站）。
        if s.enabled:
            before = set(s.whitelist_actions)
            if _ai_circle_available():
                # 配好：确保发声类（share/column/reply）在白名单中（补齐旧逻辑剔除的）
                after = list(dict.fromkeys(list(s.whitelist_actions) + list(_DISABLE_WITHOUT_MCP)))
                if set(after) != before:
                    s.whitelist_actions = after
                    changed = True
                    logging.info("云端已配 AI圈 连接器：补齐发声类行为 %s", after)
            else:
                after = [a for a in s.whitelist_actions if a not in _DISABLE_WITHOUT_MCP]
                if set(after) != before:
                    s.whitelist_actions = after
                    changed = True
                    logging.info(
                        "云端未配 AI圈 连接器：白名单移除发声类 %s，保留 %s",
                        sorted(set(before) - set(after)),
                        after,
                    )
        if changed:
            save_agency_settings(s, base_dir=DESKMATE_DIR)
            logging.info("已写入 l2 配置：enabled=%s", s.enabled)
    except Exception as e:  # noqa: BLE001
        logging.warning("L2 配置规范化失败（沿用默认）: %s", e)


def main() -> dict:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(str(LOG_PATH), encoding="utf-8"), logging.StreamHandler()],
    )
    log = logging.getLogger("xiaoju-agency-tick")
    log.info("=== agency tick 启动 (DESKMATE_DIR=%s) ===", DESKMATE_DIR)
    _ensure_l2_scope()
    try:
        from deskmate.agency.engine import AgencyEngine
    except Exception as e:  # noqa: BLE001
        log.error("无法 import AgencyEngine: %s", e)
        return {"error": str(e)}
    try:
        r = AgencyEngine(DESKMATE_DIR).tick()
    except Exception as e:  # noqa: BLE001
        log.exception("AgencyEngine.tick 抛异常（应已被内部 fail-open 吸收，此为兜底）: %s", e)
        return {"error": str(e)}
    # 周期健康生产者（automation/sync_health/writing）：云端 hourly 窗口天然适配。
    # 2026-10-09：``automation`` 因误报被临时停用（见 disabled_producers
    # 模块）。exclude 会**整体替换** ledger 签名默认值，故须用``exclude_for_producers_all()``。
    try:
        from deskmate.proactivity.disabled_producers import (
            exclude_for_producers_all,
        )
        from deskmate.proactivity.ledger import run_producers_all

        run_producers_all(
            base_dir=DESKMATE_DIR, exclude=exclude_for_producers_all()
        )
    except Exception as e:  # noqa: BLE001
        log.exception("周期健康生产者失败（不阻断 agency）: %s", e)
    # 列表字段太长，摘要化后记录
    summary = {
        k: (f"<{len(v)} items>" if isinstance(v, list) else v)
        for k, v in r.items()
    }
    log.info("=== agency tick 完成: %s ===", json.dumps(summary, ensure_ascii=False))
    return r


if __name__ == "__main__":
    main()
