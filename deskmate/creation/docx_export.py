"""剧本 / 大纲定稿导出 .docx（基于本地 officecli）。

officecli 是萧哥本地装的 AI-friendly Office CLI（默认 ~/.local/bin/officecli），
schema-driven 地操作 .docx/.xlsx/.pptx。这里用它的 `markdown` 元素把结构化文本
一次性展开为规范的 Word 文档（标题 / 段落 / 列表 / 表格 / 引用自动映射），
比在 Python 里手搓 python-docx 干净得多。

设计要点：
- 探测顺序：~/.local/bin/officecli → PATH 中 which officecli。找不到则降级写 .md，
  并在返回里标记 degraded，绝不阻断定稿主流程。
- 文件名带时间戳，永不覆盖历史定稿（多次定稿各自留痕）。
- 导出失败只返回错误 dict，由调用方决定是否记日志；本模块不抛异常。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 输出根：openworker 仓库下的 output/（远离 ~/Documents/已出版 神圣稿件，可随项目版本管理）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTPUT_DIR = os.path.join(_REPO_ROOT, "output")


def find_officecli() -> Optional[str]:
    """定位 officecli 可执行文件，找不到返回 None。"""
    cand = [os.path.expanduser("~/.local/bin/officecli")]
    for c in cand:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    found = shutil.which("officecli")
    if found:
        return found
    return None


def _safe_name(s: str) -> str:
    s = (s or "未命名").strip().replace("/", "_").replace("\\", "_").replace(":", "_")
    return s[:40] or "未命名"


def _ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def export_creation_docx(
    kind: str,
    title: str,
    markdown: str,
    out_dir: Optional[str] = None,
) -> dict:
    """把定稿 markdown 导出为 .docx。

    参数：
        kind: "screenplay" | "outline"（仅用于文件名区分）
        title: 剧名 / 大纲标题（用于 docx 标题与文件名）
        markdown: 已组织好的 Markdown 文本（建议首行是 `# 标题`）
        out_dir: 输出目录，默认仓库 output/

    返回：{"ok": bool, "path": str|None, "degraded": bool, "error": str|None}
        - ok=True & degraded=False：成功生成 docx
        - degraded=True：officecli 缺失，已降级写出同名 .md
        - ok=False：导出失败（调用方应忽略，不影响定稿状态机）
    """
    out_dir = out_dir or OUTPUT_DIR
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "path": None, "degraded": False,
                "error": f"无法创建输出目录 {out_dir}: {exc}"}

    base = f"{_safe_name(title)}_{kind}_{_ts()}"
    cli = find_officecli()

    # 降级分支：无 officecli，直接写 .md
    if not cli:
        md_path = os.path.join(out_dir, base + ".md")
        try:
            Path(md_path).write_text(markdown, encoding="utf-8")
            return {"ok": True, "path": md_path, "degraded": True,
                    "error": "officecli 未安装，已降级导出 .md"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "path": None, "degraded": False, "error": str(exc)}

    out_path = os.path.join(out_dir, base + ".docx")
    tmp: Optional[str] = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".md", prefix="docx_src_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(markdown)

        # create 拒绝覆盖既存文件；我们用带时间戳的唯一名，正常不会冲突。
        r1 = subprocess.run([cli, "create", out_path], capture_output=True, text=True, timeout=60)
        if r1.returncode != 0:
            err = (r1.stderr or r1.stdout).strip()
            if "file_exists" in err:
                # 极端并发同名：再缀一记时间戳
                out_path = os.path.join(out_dir, base + "_" + str(int(time.time())) + ".docx")
                r1 = subprocess.run([cli, "create", out_path], capture_output=True, text=True, timeout=60)
            if r1.returncode != 0:
                return {"ok": False, "path": None, "degraded": False,
                        "error": f"officecli create 失败: {(r1.stderr or r1.stdout).strip()}"}

        # add markdown（src 指向临时文件，避开 shell 转义）
        r2 = subprocess.run(
            [cli, "add", out_path, "/", "--type", "markdown", "--prop", f"src={tmp}"],
            capture_output=True, text=True, timeout=120,
        )
        if r2.returncode != 0:
            return {"ok": False, "path": None, "degraded": False,
                    "error": f"officecli add markdown 失败: {(r2.stderr or r2.stdout).strip()}"}

        # close（flush 到磁盘释放驻留）
        r3 = subprocess.run([cli, "close", out_path], capture_output=True, text=True, timeout=60)
        if r3.returncode != 0:
            logger.warning("officecli close 非零退出（可能已自动 flush）: %s",
                           (r3.stderr or r3.stdout).strip())

        # validate 兜底校验（失败仅告警，不回滚）
        rv = subprocess.run([cli, "validate", out_path], capture_output=True, text=True, timeout=60)
        if rv.returncode != 0:
            logger.warning("officecli validate 警告: %s", (rv.stderr or rv.stdout).strip())

        return {"ok": True, "path": out_path, "degraded": False, "error": None}
    except subprocess.TimeoutExpired:
        return {"ok": False, "path": None, "degraded": False, "error": "officecli 执行超时"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "path": None, "degraded": False, "error": str(exc)}
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
