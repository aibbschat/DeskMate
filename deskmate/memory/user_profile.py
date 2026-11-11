"""USER.md 用户画像管理 — 从对话中自动提取用户偏好。"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from ._fs_lock import atomic_write_locked

logger = logging.getLogger(__name__)

_DEFAULT_USER = """\
# 用户画像

## 基本信息
- （待填充）

## 工作习惯
- （待观察）

## 偏好
- （待发现）

## 关注领域
- （待了解）

## 需要 AI 避免的
- （待记录）
"""

#: 同一字符连续 ≥200 次（如「画」×4000）
_REPEAT_1_RE = re.compile(r"(.)\1{199,}")
#: 同一 2 字单元连续 ≥50 次（如「画像」×4000）——正常中文行文不会出现
_REPEAT_2_RE = re.compile(r"(..)\1{49,}")
#: `## 小节` 标题
_HEADING_RE = re.compile(r"(?m)^##\s+\S")

#: 轮转备份保留份数 / 拒写快照保留份数
_ROLLING_KEEP = 5
_REJECTED_KEEP = 20

#: 健康检查节流：同一进程每 5 分钟最多查一次（build_full_context_for_turn 每轮调用）
_HEALTH_INTERVAL = 300.0
_last_health_check = 0.0


def get_user_path(base_dir: Path) -> Path:
    return base_dir / "USER.md"


def load_user(base_dir: Path) -> str:
    """加载 USER.md，不存在则返回默认模板。"""
    p = get_user_path(base_dir)
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return _DEFAULT_USER


def _headings(text: str) -> int:
    return len(_HEADING_RE.findall(text or ""))


def check_user_content(content: str, old: str = "") -> tuple[bool, str]:
    """写入前的合理性检查：拒绝退化覆写，放行正常写入。

    返回 ``(ok, reason)``。只拦「明显异常」的整篇覆写，阈值定得很保守，
    正常追加 / 压缩（约 −30%）/ 局部替换 / 手动精简都不会触发：

    1. 退化重复：同一字符连 200+ 或同一 2 字单元连 50+（与旧内容无关，始终检查）。
    2. 分节结构丢失：旧文件有 ≥3 个 ``##`` 而新内容一个都没有。
    3. 长度突变：旧文件 ≥300 字符时，新内容 ≤25% 旧长（骤降）或 ≥4 倍旧长（骤增）。
    """
    new = content or ""
    if _REPEAT_1_RE.search(new):
        return False, "重复字符过长"
    if _REPEAT_2_RE.search(new):
        return False, "重复片段过长"

    old = old or ""
    if len(old) < 300:
        return True, ""

    if _headings(old) >= 3 and _headings(new) == 0:
        return False, "分节结构丢失"
    if len(new) <= max(200, int(len(old) * 0.25)):
        return False, f"长度骤降 {len(old)}->{len(new)}"
    if len(new) >= len(old) * 4 and len(new) > 4000:
        return False, f"长度骤增 {len(old)}->{len(new)}"
    return True, ""


def _record_alert(base_dir: Path, content: str, reason: str) -> None:
    """把被拒内容留存现场快照，并写一行告警（不注入正文）。"""
    try:
        payload = content or ""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        rej_dir = base_dir / "backups" / "user-rejected"
        rej_dir.mkdir(parents=True, exist_ok=True)
        (rej_dir / f"USER.md.rejected-{stamp}.bak").write_text(payload, encoding="utf-8")
        # 只保留最近 N 份
        olds = sorted(rej_dir.glob("USER.md.rejected-*.bak"))
        for f in olds[:-_REJECTED_KEEP]:
            f.unlink(missing_ok=True)

        alerts = base_dir / "alerts.log"
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with alerts.open("a", encoding="utf-8") as fh:
            fh.write(
                f"{ts}\tUSER.md\t拒绝写入\t{reason}\tsize={len(payload)}\tsha256={digest}\n"
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("留存 USER.md 拒写现场失败: %s", e)


def _rolling_backup(base_dir: Path, p: Path) -> None:
    """写入前轮转备份当前文件，保留最近 N 份，供事后比对。"""
    if not p.is_file():
        return
    try:
        roll = base_dir / "backups" / "user-rolling"
        roll.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(p, roll / f"USER.md.{stamp}.bak")
        olds = sorted(roll.glob("USER.md.*.bak"))
        for f in olds[:-_ROLLING_KEEP]:
            f.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        logger.debug("USER.md 轮转备份失败: %s", e)


def save_user(base_dir: Path, content: str, *, force: bool = False) -> bool:
    """保存 USER.md（带内容合理性护栏）。

    ⚠ 加锁原子写（2026-09-23 补）：USER.md 被对话轮管理器方法、后台守护线程
    ``reflect_and_persist``、定时 ``run_dream`` 三入口并发写，历史上无互斥保护，
    是「同一件事被重复写成两条」（并发重入）的根因之一。改用项目约定的
    fcntl 锁 + 原子写（见 ``_fs_lock``）。

    ⚠ 内容护栏（2026-11-11 补）：2026-11-11 17:41 一次「整篇覆写」把 USER.md
    写成了退化文本。原子写只能防半截文件，防不住「内容本身就是坏的」。这里在
    落盘前做重复度 / 分节结构 / 长度突变检查，异常则**拒绝写入**并留存现场快照。

    Returns:
        True 已写入；False 被护栏拒绝（原文件保持不变）。
    """
    p = get_user_path(base_dir)
    try:
        old = p.read_text(encoding="utf-8") if p.is_file() else ""
    except Exception:
        old = ""

    if not force:
        ok, reason = check_user_content(content, old)
        if not ok:
            _record_alert(base_dir, content, reason)
            logger.warning(
                "拒绝写入 USER.md：%s（新 %d 字符 / 旧 %d 字符）",
                reason,
                len(content or ""),
                len(old),
            )
            return False

    _rolling_backup(base_dir, p)
    atomic_write_locked(p, content)
    return True


def reset_user(base_dir: Path) -> str:
    """重置为默认模板。"""
    save_user(base_dir, _DEFAULT_USER, force=True)
    return _DEFAULT_USER


def _latest_rolling(base_dir: Path) -> str:
    """最近一份正常轮转快照的文本；没有则空串。"""
    roll = base_dir / "backups" / "user-rolling"
    if not roll.is_dir():
        return ""
    files = sorted(roll.glob("USER.md.*.bak"))
    if not files:
        return ""
    try:
        return files[-1].read_text(encoding="utf-8")
    except Exception:
        return ""


def check_user_file_health(base_dir: Path, *, interval: float | None = None) -> dict:
    """旁路监测：直接读 USER.md 判断是否被异常覆写（不经过 save_user 的写入也能发现）。

    - 退化重复 / 分节结构缺失：与内容本身比；
    - 长度突变：与最近一份正常轮转快照比。
    异常时留存现场快照 + 写 ``alerts.log``，并返回 ``ok=False``。

    默认每进程每 5 分钟最多执行一次（``interval`` 可覆盖，测试传 0 强制）。
    """
    global _last_health_check
    now = time.time()
    iv = _HEALTH_INTERVAL if interval is None else interval
    if iv > 0 and now - _last_health_check < iv:
        return {"checked": False}
    _last_health_check = now

    p = get_user_path(base_dir)
    if not p.is_file():
        return {"checked": True, "ok": True}
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return {"checked": True, "ok": False, "reason": f"读取失败:{e}"}

    reason = ""
    if _REPEAT_1_RE.search(text) or _REPEAT_2_RE.search(text):
        reason = "重复片段过长"
    elif len(text) > 800 and _headings(text) == 0:
        reason = "分节结构缺失"
    else:
        good = _latest_rolling(base_dir)
        if len(good) >= 300:
            if len(text) <= max(200, int(len(good) * 0.25)):
                reason = f"长度骤降 {len(good)}->{len(text)}"
            elif len(text) >= len(good) * 4:
                reason = f"长度骤增 {len(good)}->{len(text)}"

    if reason:
        _record_alert(base_dir, text, "监测:" + reason)
        logger.warning("USER.md 健康检查异常：%s", reason)
        return {"checked": True, "ok": False, "reason": reason, "size": len(text)}
    return {"checked": True, "ok": True, "size": len(text)}
