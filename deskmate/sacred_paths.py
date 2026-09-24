"""作者稿件路径守卫 —— **单一事实源**。

萧哥 2026-09-24 明确拍板：自己的稿子（如《国子监秘闻录》）是桌搭写作助理的
核心工作对象，**读 / 讨论 / 灌进通用记忆 / 自动刷新人物圣经** 都是红线之内、
用户授权的正常行为，不该被拦。一个读不了你稿子的写作助理等于摆设。

真正要死守的红线只有一条：**小桔绝不直接改写 / 删除你的原稿文件**
（.docx / 人物小传.md / 大纲.docx 等创作正文）。她可以读、可以抽取知识、
可以把知识沉淀进该书专属的 ``.deskmate`` 圣经库或通用记忆，但**原稿本身只读不改**。

**方案 A（2026-09-24 萧哥拍板）：保护/识别信号与书名解耦。**
「受保护」=「该路径落在含 ``.deskmate`` 圣经的目录下」，与具体书名无关。
这样每开一本新书，只要跑一次 ``analyze_novel`` 建好 ``.deskmate``，它就自动：

  (a) 被识别为『作者在写的书』（读侧放行、可讨论）；
  (b) 原稿受 :func:`guarded_write` 保护（绝不直接改写）；
  (c) 圣经被 ``after_action`` 自动增量刷新。

三者共用同一信号，不必为每本书改代码或往名单里加书名——这是系统性任务
（萧哥会持续写多本书），硬红线不能随换书而悄悄失效。

两档语义：
1. **读侧（全部放行）**：作者稿可被读、被讨论、被辅助。
   - ``is_author_manuscript``：标记「这是萧哥在跟的某本书稿」（含 ``.deskmate`` 即算）。
   - ``guarded_file`` / ``guarded_dir_entry``：摄入/发现层，对作者稿不拦截。
   - ``guarded_assistance_read``：读稿协助通道（建圣经/进度/一致性）。
2. **写侧（唯一硬红线）**：``guarded_write`` 拦截对作者原稿正文的任何写入/删除；
   其 ``.deskmate`` 圣经子目录允许（那是小桔自己维护的记忆库）。

任何要**写**用户文件的代码都必须过 ``guarded_write``；读侧通道不调用它。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 圣经目录名：与 deskmate/novel_memory/core.py 和 deskmate/memory/after_action.py 的
# _BIBLE_DIR 保持一致。『目录里含这个子目录』即『小桔在跟的一本书』——保护信号。
_BIBLE_DIR = ".deskmate"


def is_author_manuscript(path: str | Path) -> bool:
    """该路径是否属于萧哥正在创作、且已建圣经的某本书稿（可读、可讨论、可辅助）。

    **方案 A**：识别信号 = 路径所在层级的某个祖先目录含 ``.deskmate`` 圣经库，
    与书名无关。返回 True 仅表示「这是作者的稿子」，**不表示禁区**——读侧一律放行；
    只有 :func:`guarded_write` 据此拒绝改写原稿正文。
    """
    try:
        p = Path(str(path)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    for ancestor in p.parents:
        if (ancestor / _BIBLE_DIR).is_dir():
            return True
    return False


# ── 读稿协助通道（读侧，放行）────────────────────────────────────────────────
# 小桔读萧哥的稿子只为「知道写到哪、人物飘没飘」：建人物圣经、看进度、查一致性。
# 读出的内容沉淀进该书专属的 `.deskmate` 圣经库与（经用户授权）通用记忆。
ASSISTANCE_READ_OK = True

# 读稿协助通道允许的扩展名（与 novel_memory / writing 对齐：纯文本 + docx）。
_ASSISTANCE_EXTS = {".txt", ".md", ".markdown", ".text", ".novel", ".docx"}


def guarded_assistance_read(
    path: str | Path, *, max_chars: int = 10_000_000
) -> str | None:
    """**读稿协助通道**：读取作者稿正文（建人物圣经 / 进度回顾 / 一致性校验）。

    安全约束（fail-closed）：
    - 强制 ``resolve()`` 解析真实路径，且只允许本地普通文件；
    - 扩展名白名单（仅纯文本 / docx），不读任何其它类型（防误读二进制 / 凭据）；
    - 读取 / 解析失败一律返回 ``None``，绝不抛、绝不退回「读任意文件」。
    """
    try:
        p = Path(str(path)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not p.is_file():
        return None
    if p.suffix.lower() not in _ASSISTANCE_EXTS:
        return None
    try:
        if p.suffix.lower() == ".docx":
            from deskmate.writing import read_text_from_path

            text = read_text_from_path(str(p), max_chars=max_chars)
        else:
            text = p.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:  # noqa: BLE001
        logger.warning("读稿协助读取失败（返回空）: %s -> %s", path, exc)
        return None
    return text or None


def guarded_file(
    path: str | Path,
    allowed_exts: Iterable[str] | None = None,
) -> Path | None:
    """外部传入的路径 → 可安全**摄入进通用记忆库 / 读取**的解析后路径；不可则 ``None``。

    2026-09-24 起：作者稿（如《国子监秘闻录》）**授权可进通用记忆**，不再一票否决。
    本函数只做通用安全校验：

    1. ``resolve()`` 失败 / 解析后不是普通文件 → 拒；
    2. ``allowed_exts`` 给了则校验扩展名白名单 → 不在名单拒。

    Returns:
        解析后的 :class:`Path`；被拒时 ``None``（调用方按 skip 处理）。
    """
    if path is None:
        return None
    try:
        base = Path(str(path)).expanduser()
    except (TypeError, ValueError):
        return None
    try:
        real = base.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        if not real.is_file():
            return None
    except OSError:
        return None

    if allowed_exts is not None:
        wanted = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in allowed_exts}
        if real.suffix.lower() not in wanted:
            return None
    return real


def guarded_dir_entry(path: str | Path, *, allow_discovery: bool = False) -> bool:
    """遍历（scan/discovery）时判断某条目是否可继续深入（对目录递归）。

    2026-09-24 起：作者稿目录可列出、可深入（读侧全放行）。本函数仅做基础
    可遍历性校验——任何真实路径都允许（fail-open 返回 True），``allow_discovery``
    仅保留作兼容参数，语义上不再区分。
    """
    try:
        Path(str(path))
    except (TypeError, ValueError):
        return False
    return True


def guarded_write(path: str | Path) -> bool:
    """小桔是否允许写入 / 删除该路径——**唯一的硬红线**。

    **方案 A（2026-09-24）**：保护信号 = 『该路径落在含 ``.deskmate`` 圣经的目录下』，
    与书名无关。每开一本新书，只要跑一次 ``analyze_novel`` 建好 ``.deskmate``，
    原稿就自动受保护；与 ``after_action`` 的圣经自动刷新共用同一信号。

    判定：
    - 路径落在 ``.deskmate`` 圣经子目录内 → 允许（小桔自己维护的记忆库，可写）；
    - 路径落在某『含 ``.deskmate`` 的目录』之下（即某本在跟的书的原稿）→ **拒绝**；
    - 其它一切（未跟的书、普通文件）→ 允许（按常规权限处理）。

    解析失败按「危险」拒绝（fail-closed）。
    """
    try:
        p = Path(str(path)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    # 1) 圣经子目录本身：永远允许（小桔自己维护的记忆库）
    if _BIBLE_DIR in p.parts:
        return True
    # 2) 落在某本『含 .deskmate 圣经』的目录下 → 这是原稿正文，绝不动
    for ancestor in p.parents:
        if (ancestor / _BIBLE_DIR).is_dir():
            return False
    # 3) 其它：允许
    return True
