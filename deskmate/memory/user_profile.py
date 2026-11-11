"""USER.md 用户画像管理 — 从对话中自动提取用户偏好。"""

from __future__ import annotations

import difflib
import hashlib
import json
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


# ---------------------------------------------------------------------------
# 条目归属闸（2026-11-11）——防止机器文本安静地变成常驻硬规矩
# ---------------------------------------------------------------------------
# 为什么要这道闸
# ==============
# USER.md 分两层：
#   - ``## 硬规矩``：每轮**常驻**注入 system prompt，写错就每轮都错；
#   - ``## 偏好与事实（非常驻·待检索）``：只有检索命中时才进上下文。
#
# 抽取层（对话后事实提取）只会 ``append_to_section("user", …)``，把条目拼到文件
# **末尾**——也就是偏好层，所以它已经写不进硬规矩。真正的口子只剩「整篇覆写」：
#   ① 压缩：USER.md 超限 → ``_llm_compress_user`` → ``save_user`` 整篇覆盖；
#   ② ``write_memory(section="user")``：HTTP / MCP 两个调用方，模型可自己回写。
# 而 ``check_user_content`` 只看「标题还在不在、长度有没有突变」，不关心里面的
# 条目属于哪一层，于是 2026-11-11 那批「用户不希望…」系统文案能被搬进硬规矩。
#
# 判据只有一条，且刻意不做语义判断
# ================================
# **硬规矩节里新出现的条目，必须带人工确认标记**（``MANUAL_SOURCE_MARK``）。
# 同层内的改写 / 合并放行（相似度 ≥ ``_REWRITE_SIM``），这样既拦得住「搬运 +
# 新增」，又不会误伤「把已有规矩换个说法压缩短一点」。
LAYER_HARD = "硬规矩"
LAYER_PREF = "偏好与事实"
#: 人工晋升标记：硬规矩新增条目必须显式带上这串（写在条目正文里即可）。
MANUAL_SOURCE_MARK = "〔人工确认〕"
_HARD_HEAD_RE = re.compile(r"(?m)^##\s*硬规矩")
_PREF_HEAD_RE = re.compile(r"(?m)^##\s*偏好与事实")
#: 改写/合并的放行阈值（与旧硬规矩某条足够像 → 视为「同一句话改了个说法」）
_REWRITE_SIM = 0.6

#: 闸事件日志（拒写 / 降级 / 强制写入），每行一条 JSON
_GATE_TRACE = "traces/user_layer_gate.jsonl"


def split_layers(text: str) -> dict[str, list[str]]:
    """把 USER.md 拆成 ``{层名: [条目正文]}``。

    只认两个记忆层；其余 ``## `` 小节（基本信息 / 写作观 / 日常流程…）归入
    ``"其它"``，既不参与比对也不被丢弃——本函数只读不写。
    """
    out: dict[str, list[str]] = {LAYER_HARD: [], LAYER_PREF: [], "其它": []}
    layer = "其它"
    for line in (text or "").splitlines():
        if line.startswith("## "):
            if "硬规矩" in line:
                layer = LAYER_HARD
            elif line.startswith("## 偏好与事实"):
                layer = LAYER_PREF
            else:
                layer = "其它"
            continue
        if line.startswith("- ") and layer in out:
            out[layer].append(line[2:].strip())
    return out


def _entry_key(entry: str) -> str:
    """条目指纹：去掉人工标记与所有空白，用于「同一条」判定。"""
    s = (entry or "").replace(MANUAL_SOURCE_MARK, "")
    return re.sub(r"\s+", "", s)


def _is_rewrite_of_old(entry: str, old_keys: list[str]) -> bool:
    """新硬规矩条目是不是某条旧硬规矩的改写（而非凭空多出来的）。"""
    k = _entry_key(entry)
    if not k:
        return True
    for old in old_keys:
        if not old:
            continue
        if k in old or old in k:
            return True
        if difflib.SequenceMatcher(None, k, old).ratio() >= _REWRITE_SIM:
            return True
    return False


def find_unmarked_hard_additions(old: str, new: str) -> list[str]:
    """返回新文本「硬规矩」节里新增、且既无人工标记也不是旧条改写的条目。"""
    old_hard = [_entry_key(e) for e in split_layers(old)[LAYER_HARD]]
    old_set = set(old_hard)
    bad: list[str] = []
    for entry in split_layers(new)[LAYER_HARD]:
        if _entry_key(entry) in old_set:
            continue
        if MANUAL_SOURCE_MARK in entry:
            continue
        if _is_rewrite_of_old(entry, old_hard):
            continue
        bad.append(entry)
    return bad


def check_layer_integrity(content: str, old: str = "") -> tuple[bool, str, list[str]]:
    """条目归属闸：整篇覆写不得把机器文本升格成硬规矩。

    返回 ``(ok, reason, 违规条目)``。只在旧文件已有这两层时才校验——历史遗留的
    单层 USER.md 做一次性迁移时不会被误拦。

    与 ``check_user_content`` 的关系：**并行、不替代**。那条护栏管「文件本身坏没坏」
    （重复 / 结构 / 长度），这条管「条目有没有越层」。
    """
    if not (old or "").strip():
        return True, "", []
    if _HARD_HEAD_RE.search(old) and not _HARD_HEAD_RE.search(content or ""):
        return False, "硬规矩节被删除", []
    if _PREF_HEAD_RE.search(old) and not _PREF_HEAD_RE.search(content or ""):
        return False, "偏好层被删除", []
    bad = find_unmarked_hard_additions(old, content or "")
    if bad:
        return False, f"硬规矩新增 {len(bad)} 条无人工确认标记", bad
    return True, "", []


def _blocks(text: str) -> list[tuple[str, list[str]]]:
    """按 ``## `` 切成 [(标题行(含换行), 主体行)]，首块标题为空串（前言）。"""
    blocks: list[tuple[str, list[str]]] = [("", [])]
    for line in (text or "").splitlines(keepends=True):
        if line.startswith("## "):
            blocks.append((line, []))
        else:
            blocks[-1][1].append(line)
    return blocks


def demote_unmarked_hard_rules(old: str, new: str) -> tuple[str, list[str]]:
    """把压缩产物里「无标记的新硬规矩」降级回偏好层末尾（不丢内容）。

    为什么是降级而不是整篇拒绝：压缩是**自动触发的**（文件超限），整篇拒绝会让
    文件一直超限、每次写入都重试压缩，形成死循环；而降级的方向是安全的——从
    常驻层退回需检索层，最坏结果是「该被记住的没被常驻」，而不是「机器文案每轮
    都在注入」。整篇写入（路径 C）则用拒绝，见 ``check_layer_integrity``。
    """
    bad = find_unmarked_hard_additions(old, new)
    if not bad:
        return new, []
    bad_keys = {_entry_key(b) for b in bad}
    blocks = _blocks(new)
    hard_idx = pref_idx = None
    for i, (head, _) in enumerate(blocks):
        if not head:
            continue
        if hard_idx is None and "硬规矩" in head:
            hard_idx = i
        if pref_idx is None and head.startswith("## 偏好与事实"):
            pref_idx = i
    if hard_idx is None:
        return new, []
    head, body = blocks[hard_idx]
    kept = [
        ln for ln in body
        if not (ln.startswith("- ") and _entry_key(ln[2:]) in bad_keys)
    ]
    blocks[hard_idx] = (head, kept)
    moved = [f"- {b}\n" for b in bad]
    if pref_idx is None:
        rendered = "".join(h + "".join(b) for h, b in blocks)
        tail = "" if (rendered.endswith("\n") or not rendered) else "\n"
        return (
            rendered + tail
            + "\n## 偏好与事实（非常驻·待检索）\n" + "".join(moved),
            bad,
        )
    phead, pbody = blocks[pref_idx]
    at = len(pbody)
    for j in range(len(pbody) - 1, -1, -1):
        if pbody[j].strip():
            at = j + 1
            break
    blocks[pref_idx] = (phead, pbody[:at] + moved + pbody[at:])
    return "".join(h + "".join(b) for h, b in blocks), bad


def _record_gate_event(base_dir: Path, kind: str, payload: dict) -> None:
    """写一行闸事件（拒写 / 降级 / 强制写入），供事后追查「谁在什么时候放的」。"""
    try:
        d = base_dir / "traces"
        d.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "kind": kind}
        rec.update(payload or {})
        with (d / "user_layer_gate.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        logger.debug("写 USER.md 闸事件失败: %s", e)


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


def save_user(
    base_dir: Path,
    content: str,
    *,
    force: bool = False,
    actor: str = "",
    reason: str = "",
) -> bool:
    """保存 USER.md（内容护栏 + 条目归属闸）。等价于 ``save_user_ex(...)[0]``。"""
    ok, _ = save_user_ex(base_dir, content, force=force, actor=actor, reason=reason)
    return ok


def save_user_ex(
    base_dir: Path,
    content: str,
    *,
    force: bool = False,
    actor: str = "",
    reason: str = "",
) -> tuple[bool, str]:
    """保存 USER.md，返回 ``(ok, 说明)``。

    ⚠ 加锁原子写（2026-09-23 补）：USER.md 被对话轮管理器方法、后台守护线程
    ``reflect_and_persist``、定时 ``run_dream`` 三入口并发写，历史上无互斥保护，
    是「同一件事被重复写成两条」（并发重入）的根因之一。改用项目约定的
    fcntl 锁 + 原子写（见 ``_fs_lock``）。

    ⚠ 内容护栏（2026-11-11 补）：2026-11-11 17:41 一次「整篇覆写」把 USER.md
    写成了退化文本。原子写只能防半截文件，防不住「内容本身就是坏的」。这里在
    落盘前做重复度 / 分节结构 / 长度突变检查，异常则**拒绝写入**并留存现场快照。

    ⚠ 条目归属闸（2026-11-11 补，在 save_user 内部而不是各调用方）：压缩、
    HTTP ``write_memory``、MCP ``memory_write`` 三条整篇覆写路径都从这里落盘，
    闸放在这一层才没有旁路。判据见 ``check_layer_integrity``。

    ``force=True`` 是逃生口：跳过两道检查，但**必须留痕**（``actor`` 是谁、
    ``reason`` 为什么），写入 ``traces/user_layer_gate.jsonl``，事后可追查。

    Returns:
        ``(True, "")`` 已写入；``(False, 原因)`` 被护栏或闸拒绝（原文件不变）。
    """
    p = get_user_path(base_dir)
    try:
        old = p.read_text(encoding="utf-8") if p.is_file() else ""
    except Exception:
        old = ""

    if not force:
        ok, reason_ = check_user_content(content, old)
        if not ok:
            _record_alert(base_dir, content, reason_)
            _record_gate_event(base_dir, "reject", {
                "gate": "content", "reason": reason_,
                "new_chars": len(content or ""), "old_chars": len(old),
            })
            logger.warning(
                "拒绝写入 USER.md：%s（新 %d 字符 / 旧 %d 字符）",
                reason_,
                len(content or ""),
                len(old),
            )
            return False, reason_

        ok, reason_, bad = check_layer_integrity(content, old)
        if not ok:
            _record_alert(base_dir, content, reason_)
            _record_gate_event(base_dir, "reject", {
                "gate": "layer", "reason": reason_,
                "entries": [b[:120] for b in bad[:5]],
            })
            logger.warning("拒绝写入 USER.md（条目归属闸）：%s", reason_)
            detail = "；".join(b[:60] for b in bad[:2])
            return False, f"{reason_}：{detail}" if detail else reason_
    else:
        # 逃生口：留痕（谁 / 何时 / 为什么 / 搬了哪些条目）
        _record_gate_event(base_dir, "force", {
            "actor": actor or "unknown",
            "reason": reason or "未说明",
            "hard_added": [e[:120] for e in find_unmarked_hard_additions(old, content)][:5],
            "new_chars": len(content or ""), "old_chars": len(old),
        })

    _rolling_backup(base_dir, p)
    atomic_write_locked(p, content)
    return True, ""


def reset_user(base_dir: Path) -> str:
    """重置为默认模板。"""
    save_user(
        base_dir, _DEFAULT_USER, force=True,
        actor="reset_user", reason="重置为默认模板（显式人工操作）",
    )
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
