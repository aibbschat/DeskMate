"""写作状态上下文 —— 给后台主动 tick 喂眼睛（2026-10-24 主动性改造阶段一）。

背景：后台 tick 创建 ProactiveEngine 时不传 workspace，memory / project
上下文也是空，导致 judge 看不到手稿、圣经与最近改动，只能凭情绪与行为
模式判断，于是主动产出几乎全是 emotional_care / self_expression。

本模块只读文件名与修改时间，不读正文、不写任何文件、不抛异常：
任何一步失败都返回空 / None，绝不阻断 tick（fail-open）。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

_MANUSCRIPT_EXTS = (".docx", ".md")
_BOOK_DIR_PREFIX = re.compile(r"^\d+\s*[.、]\s*")
_DEFAULT_ROOT = "~/Documents/已出版"


def _book_name(dir_name: str) -> str:
    return _BOOK_DIR_PREFIX.sub("", dir_name).strip() or dir_name


def resolve_writing_workspace(explicit=None, base_dir=None):
    """解析写作工作区目录，优先级：显式参数 > 环境变量 > ingest_dirs.json 首项 > 默认根。

    返回 Path 或 None（都不存在时）。调用方不得假设返回值非空。
    """
    try:
        candidates: list[str] = []
        if explicit:
            candidates.append(str(explicit))
        env = (os.environ.get("DESKMATE_WRITING_WORKSPACE") or "").strip()
        if env:
            candidates.append(env)
        if base_dir is not None:
            try:
                ingest = Path(base_dir).expanduser() / "ingest_dirs.json"
                if ingest.is_file():
                    data = json.loads(ingest.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        candidates.extend(str(x) for x in data if x)
            except Exception:
                pass
        candidates.append(_DEFAULT_ROOT)
        for raw in candidates:
            p = Path(raw).expanduser()
            if p.is_dir():
                return p
    except Exception:
        pass
    return None


def _newest_activity(book_dir: Path):
    """书名目录下最新一个手稿类文件的 (mtime, 文件名)；无则 None。

    ~$ 锁文件（Word 打开时的临时文件）与隐藏文件不算活动——开着 ≠ 在写。
    """
    newest = None
    try:
        for p in book_dir.iterdir():
            if not p.is_file():
                continue
            name = p.name
            if name.startswith("~$") or name.startswith("."):
                continue
            if p.suffix.lower() not in _MANUSCRIPT_EXTS:
                continue
            m = p.stat().st_mtime
            if newest is None or m > newest[0]:
                newest = (m, name)
    except OSError:
        return None
    return newest


def build_writing_status(workspace, *, limit: int = 6) -> str:
    """把写作工作区扫成一句 judge 能用的短状态。

    只含书名、最近动笔天数与停笔提示，不含正文内容。工作区为空或扫不到
    书时返回空串。输出总长截断到 800 字，避免撑大 judge 上下文。
    """
    try:
        if workspace is None:
            return ""
        root = Path(workspace).expanduser()
        if not root.is_dir():
            return ""
        now = time.time()
        books: list[tuple[str, float, str]] = []
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            act = _newest_activity(d)
            if act is None:
                continue
            books.append((_book_name(d.name), act[0], act[1]))
        if not books:
            return ""
        books.sort(key=lambda x: x[1], reverse=True)
        lines = ["写作状态（手稿文件修改时间，只读统计）："]
        for name, mtime, fname in books[:limit]:
            days = (now - mtime) / 86400.0
            if days < 1:
                when = "今天动过笔"
            elif days < 2:
                when = "昨天动过笔"
            else:
                when = f"{int(days)} 天没动笔"
            lines.append(f"- 《{name}》：{when}（{fname}）")
        return "\n".join(lines)[:800]
    except Exception:
        return ""
