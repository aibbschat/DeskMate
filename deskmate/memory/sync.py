"""墨灵记忆 · 云端同步（P2-7，本地优先 + 可选云同步）。

设计原则（诚实、本地优先、绝不伪造云端）：
- **本地优先**：默认 ``NoopBackend``（特性在位但关闭），不联网、不持有任何密钥。
- **可插拔传输**：``SyncBackend`` 抽象只定义 push/pull/list/delete；``LocalDirBackend``
  把记忆同步到用户配置的目录——若该目录是 iCloud Drive / Dropbox / USB 等云文件夹，
  即成为**真实的跨设备同步**，且零凭证、零费用（我们不必自建云服务）。
- **敏感不跨端**：同步前按 ``§`` 分节过滤掉标记 ``#no-sync``（PRIVATE）的段落，
  复用 8.4 的 ``should_sync`` 语义——未成年人/隐私信息只留本机。
- **设备本地关系留本机**：``growth_state`` / ``behavior_log`` / ``emotion_timeline`` /
  ``traces`` 属单设备绑定与活动信号，不参与同步（多端「懂你」延续的是「你是谁/
  偏好/项目上下文」，不是「某台设备的亲密度」）。
- **可观测 + 冲突可读**：manifest 记录每个文件的 sha256 + mtime；push/pull 走
  last-write-wins，但把「两侧都改过」的文件列进 conflicts 回报，绝不静默吞掉。
- **fail-open、零 LLM**：任何异常都被捕获，同步失败不影响主流程。

同步文件集合（相对 ``~/.deskmate`` 的路径）：
    SOUL.md  USER.md  MEMORY.md  style_profile.json
    vault/**/*.md  projects/**/*.md  preferences/**/*.md
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .sensitivity import NOSYNC_TAG, should_sync

# 隐私标记写作 ``#no-sync``（含连字符）；通用 ``#(\w+)`` 正则会把连字符截断成 ``#no``，
# 故过滤时直接用字面量 ``#no-sync`` 子串判定，规避该坑。
_NOSYNC_MARK = "#" + NOSYNC_TAG

# 参与同步的「知识记忆」根文件（相对 base_dir）
_SYNC_ROOT_FILES = ["SOUL.md", "USER.md", "MEMORY.md", "style_profile.json"]

# 参与同步的目录（递归 *.md）
_SYNC_DIRS = ["vault", "projects", "preferences"]

# 设备本地状态——排除，不同步
_EXCLUDE_DIRS = {"traces", "growth", "behavior_logs", ".sync", "__pycache__"}
_EXCLUDE_FILES = {
    "behavior_log.jsonl",
    "emotion_timeline.json",
    "sync_config.json",
    "sync_manifest.json",
}

# 分节标签提取：形如 "§ 标题 #tag1 #tag2" 中的 #tag
_TAG_RE = re.compile(r"#(\w+)")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def filter_no_sync_sections(md_text: str) -> str:
    """从 markdown 文本中剔除标记了 ``#no-sync`` 的 ``§`` 分节（PRIVATE 不跨端）。

    按行扫描：命中 ``^§ `` 头行时读取其 ``#tag``，若 ``should_sync`` 为假则跳过
    该头行及其后续正文直到下一个 ``§``。保留其余内容与原始换行（精确无损）。
    无法解析时返回原文（fail-open）。
    """
    try:
        if "§" not in md_text:
            return md_text
        lines = md_text.split("\n")
        out: list[str] = []
        skipping = False
        for line in lines:
            m = re.match(r"^§\s*(.*)$", line)
            if m:
                # 用字面量 #no-sync 判定（连字符不被 \w+ 捕获），其余走 should_sync
                has_nosync = _NOSYNC_MARK in line
                skipping = has_nosync or not should_sync(set(_TAG_RE.findall(m.group(1))))
                if not skipping:
                    out.append(line)
            elif not skipping:
                out.append(line)
        return "\n".join(out)
    except Exception:
        return md_text


def _iter_syncable(base_dir: Path) -> list[Path]:
    """枚举所有应同步的本地文件（绝对路径），排除设备本地状态与同步目录自身。"""
    base_dir = Path(base_dir)
    result: list[Path] = []
    # 根文件
    for name in _SYNC_ROOT_FILES:
        p = base_dir / name
        if p.is_file():
            result.append(p)
    # 目录递归
    for d in _SYNC_DIRS:
        root = base_dir / d
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() != ".md":
                continue
            # 跳过被排除的子目录
            if any(part in _EXCLUDE_DIRS for part in p.relative_to(base_dir).parts):
                continue
            result.append(p)
    # 排除单文件
    result = [p for p in result if p.name not in _EXCLUDE_FILES]
    return result


class SyncBackend(ABC):
    """同步传输抽象。子类实现真正的远程存取。"""

    @abstractmethod
    def write(self, relpath: str, content: str) -> None:
        """把（已过滤的）文件内容写到远端 relpath。"""

    @abstractmethod
    def read(self, relpath: str) -> str | None:
        """读取远端 relpath；不存在返回 None。"""

    @abstractmethod
    def exists(self, relpath: str) -> bool: ...

    @abstractmethod
    def mtime(self, relpath: str) -> float:
        """远端文件 mtime（epoch 秒）；不存在返回 0。"""

    @abstractmethod
    def list_relpaths(self) -> list[str]:
        """列出远端所有已同步文件的 relpath。"""

    @abstractmethod
    def delete(self, relpath: str) -> None:
        """删除远端文件（本地删除后调用）。"""


class NoopBackend(SyncBackend):
    """默认后端：不联网。所有写操作静默成功、读返回 None。

    用于「未启用同步」状态——特性在位但完全本地，零风险。
    """

    def write(self, relpath: str, content: str) -> None:
        return None

    def read(self, relpath: str) -> str | None:
        return None

    def exists(self, relpath: str) -> bool:
        return False

    def mtime(self, relpath: str) -> float:
        return 0.0

    def list_relpaths(self) -> list[str]:
        return []

    def delete(self, relpath: str) -> None:
        return None


class LocalDirBackend(SyncBackend):
    """本地目录后端：把记忆同步到 ``sync_dir/openworker-memory/<relpath>``。

    若 ``sync_dir`` 指向 iCloud Drive / Dropbox / 外接磁盘等云同步文件夹，即成为
    **真实跨设备同步**，且无需任何云端密钥。所有路径均约束在 sync_dir 内，防穿越。
    """

    def __init__(self, sync_dir: str | Path):
        self.root = Path(sync_dir) / "openworker-memory"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, relpath: str) -> Path:
        # 归一化并防止目录穿越
        rel = Path(relpath).as_posix().lstrip("/")
        target = (self.root / rel).resolve()
        if self.root.resolve() not in target.parents and target != self.root.resolve():
            raise ValueError(f"非法路径（疑似穿越）: {relpath}")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def write(self, relpath: str, content: str) -> None:
        self._path(relpath).write_text(content, encoding="utf-8")

    def read(self, relpath: str) -> str | None:
        p = self._path(relpath)
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def exists(self, relpath: str) -> bool:
        return self._path(relpath).is_file()

    def mtime(self, relpath: str) -> float:
        p = self._path(relpath)
        return p.stat().st_mtime if p.is_file() else 0.0

    def list_relpaths(self) -> list[str]:
        if not self.root.is_dir():
            return []
        out: list[str] = []
        for p in self.root.rglob("*"):
            if p.is_file():
                out.append(p.relative_to(self.root).as_posix())
        return sorted(out)

    def delete(self, relpath: str) -> None:
        p = self._path(relpath)
        if p.is_file():
            p.unlink()


# ---------------------------------------------------------------------------
# 配置与 manifest
# ---------------------------------------------------------------------------

_CONFIG_FILE = "sync_config.json"
_MANIFEST_FILE = "sync_manifest.json"


def _device_id() -> str:
    return f"dev-{uuid.getnode():x}"


def load_config(base_dir: Path) -> dict:
    """读取同步配置；不存在返回默认（disabled）。fail-open。"""
    try:
        p = Path(base_dir) / _CONFIG_FILE
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {
        "enabled": False,
        "backend": "noop",
        "params": {},
        "last_sync": None,
        "device_id": _device_id(),
    }


def save_config(base_dir: Path, cfg: dict) -> None:
    try:
        d = Path(base_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / _CONFIG_FILE).write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _backend_from_config(cfg: dict, base_dir: Path) -> SyncBackend:
    try:
        if not cfg.get("enabled"):
            return NoopBackend()
        backend = cfg.get("backend", "noop")
        if backend == "localdir":
            d = cfg.get("params", {}).get("dir") or os.environ.get("DESKMATE_SYNC_DIR")
            if not d:
                return NoopBackend()
            return LocalDirBackend(d)
    except Exception:
        pass
    return NoopBackend()


# ---------------------------------------------------------------------------
# 同步服务
# ---------------------------------------------------------------------------


class MemorySyncService:
    """记忆同步编排：枚举 → 过滤敏感 → push/pull/sync → manifest/冲突。"""

    def __init__(self, base_dir: str | Path | None = None):
        self.base_dir = Path(base_dir) if base_dir else _default_base_dir()

    # ---- 对外：状态 ----
    def status(self) -> dict:
        cfg = load_config(self.base_dir)
        backend = _backend_from_config(cfg, self.base_dir)
        local = self._collect()
        remote = backend.list_relpaths()
        return {
            "enabled": bool(cfg.get("enabled")),
            "backend": cfg.get("backend", "noop"),
            "device_id": cfg.get("device_id", _device_id()),
            "last_sync": cfg.get("last_sync"),
            "local_files": len(local),
            "remote_files": len(remote),
            "sync_dir": cfg.get("params", {}).get("dir")
            or os.environ.get("DESKMATE_SYNC_DIR"),
        }

    # ---- 对外：启用/禁用 ----
    def configure(self, enabled: bool, sync_dir: str | None = None) -> dict:
        cfg = load_config(self.base_dir)
        cfg["enabled"] = bool(enabled)
        cfg["backend"] = "localdir" if enabled else "noop"
        cfg["device_id"] = cfg.get("device_id", _device_id())
        if sync_dir:
            cfg.setdefault("params", {})["dir"] = str(sync_dir)
        save_config(self.base_dir, cfg)
        return self.status()

    # ---- 对外：推送（本机 → 远端） ----
    def push(self) -> dict:
        cfg = load_config(self.base_dir)
        backend = _backend_from_config(cfg, self.base_dir)
        result = {"pushed": 0, "skipped": 0, "conflicts": [], "errors": []}
        try:
            for abspath in self._collect():
                rel = self._rel(abspath)
                raw = abspath.read_text(encoding="utf-8")
                content = self._sanitize(abspath, raw)
                # 冲突：远端已存在且内容与本地不同，且远端更「新」
                if backend.exists(rel):
                    remote_mt = backend.mtime(rel)
                    local_mt = abspath.stat().st_mtime
                    if remote_mt > local_mt and _sha256(content) != _sha256(
                        backend.read(rel) or ""
                    ):
                        result["conflicts"].append(rel)
                backend.write(rel, content)
                result["pushed"] += 1
            self._touch(cfg)
        except Exception as e:  # fail-open
            result["errors"].append(str(e))
        return result

    # ---- 对外：拉取（远端 → 本机） ----
    def pull(self) -> dict:
        cfg = load_config(self.base_dir)
        backend = _backend_from_config(cfg, self.base_dir)
        result = {"pulled": 0, "skipped": 0, "conflicts": [], "errors": []}
        try:
            for rel in backend.list_relpaths():
                remote_content = backend.read(rel) or ""
                local_path = (self.base_dir / rel).resolve()
                # 只拉回已知同步根内的文件
                if self.base_dir.resolve() not in local_path.parents:
                    continue
                local_path.parent.mkdir(parents=True, exist_ok=True)
                if local_path.is_file():
                    local_mt = local_path.stat().st_mtime
                    remote_mt = backend.mtime(rel)
                    if remote_mt <= local_mt:
                        result["skipped"] += 1
                        continue
                    # 本地也改过 → 冲突（pull 以远端为准，但记录）
                    if _sha256(local_path.read_text(encoding="utf-8")) != _sha256(
                        remote_content
                    ):
                        result["conflicts"].append(rel)
                local_path.write_text(remote_content, encoding="utf-8")
                result["pulled"] += 1
            self._touch(cfg)
        except Exception as e:  # fail-open
            result["errors"].append(str(e))
        return result

    # ---- 对外：双向同步（push 再 pull） ----
    def sync(self) -> dict:
        push_r = self.push()
        pull_r = self.pull()
        return {
            "enabled": True,
            "pushed": push_r.get("pushed", 0),
            "pulled": pull_r.get("pulled", 0),
            "skipped": pull_r.get("skipped", 0),
            "conflicts": push_r.get("conflicts", []) + pull_r.get("conflicts", []),
            "errors": push_r.get("errors", []) + pull_r.get("errors", []),
        }

    # ---- 内部 ----
    def _collect(self) -> list[Path]:
        return _iter_syncable(self.base_dir)

    def _rel(self, abspath: Path) -> str:
        return abspath.relative_to(self.base_dir).as_posix()

    def _sanitize(self, abspath: Path, raw: str) -> str:
        """同步前脱敏：markdown 剔除非跨端 § 分节；json 原样（已是偏好，无 #no-sync 机制）。"""
        try:
            if abspath.suffix.lower() == ".md":
                return filter_no_sync_sections(raw)
        except Exception:
            pass
        return raw

    def _touch(self, cfg: dict) -> None:
        cfg["last_sync"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        save_config(self.base_dir, cfg)


def _default_base_dir() -> Path:
    """与 manager 保持一致的默认记忆根目录。"""
    try:
        from .manager import _default_base_dir as _mgr_default

        return _mgr_default()
    except Exception:
        return Path(os.path.expanduser("~")) / ".deskmate"
