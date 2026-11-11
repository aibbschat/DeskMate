"""长文档工位 —— 1v1 写作专家把「分段生成」收成「一份完整产物」。

## 为什么需要（2026-11-11 只读核查结论）

写侧**没有字数闸**：`aisuite` 的 `write_file` 无上限地 `write_text(content)`，
模型单轮也还有 32000 token。真正的缺口是它 `overwrite=True`、**没有 append**：
模型为了保住上一段只能另起一个文件名，一份长大纲于是散成 N 个文件；
段与段之间还会自己加「（上接）」「未完待续」，产物被工程痕迹污染。

所以这里不放宽任何上限、不改模型参数、不改人设指令，只补一个「同一份文档的
分段工位」：

- `document_append` 把每段的**成品原文**逐字存进草稿（`.part`，不进 Artifacts 面板）；
- `document_finish` 把各段原文按序纯拼接，**一次写一个文件**；
- 生成依然可以分段（绕开单轮 tool-call 参数过长的流式截断风险），
  但产物是一份、不摘要、不截断、不改写、不带衔接语。

## 安全边界

- 草稿路径由本模块从 session 的 writable roots 自行推导，工具**不接收任意路径**，
  `doc_id` 只允许 `\\w-`，杜绝穿越；
- 成稿写用户可见文件，必须落在 writable root 内，并过 `sacred_paths.guarded_write`
  （含 `.deskmate` 圣经的目录＝作者原稿，绝不直改写）；
- `document_finish` 名在 `risk.WRITE_TOOLS` 里，走既有 WRITE_LOCAL 门禁与审批卡；
- 草稿暂存与 index 落盘用 `flock` + `atomic_write_text`（pid/线程隔离临时文件）。

契约方向：**不做更像个人，只做判断更可核验**。这里只保证产物完整，不加记忆层。
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

import aisuite as ai

try:  # Windows 无 fcntl：降级为不加锁，语义不变（单进程会话内仍是原子写）
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

_DOC_ID_RE = re.compile(r"^[\w\-]{1,64}$")
_DRAFT_DIR = "docbuild"
_INDEX_NAME = "index.state"
_LOCK_NAME = ".lock"
_JOIN = "\n\n"

_APPEND_DESC = (
    "把长文档的一段**成品原文**收进同一份文档的草稿：逐字保存，不摘要、不截断、不改写。"
    "长文档一律用本工具分段攒、再用 document_finish 一次成稿；"
    "不要用 write_file 把一份大纲/脑洞拆成多个文件。"
    "每段必须是能直接放进成品的文字：不要写「未完待续」「（上接）」「以下省略」，"
    "不要拿提纲或摘要冒充正文，不要在正文里标第几段（section 只进索引、不进成品）。"
    "doc_id 是这份文档的标识（如 yingchuang-gang），一份文档全程用同一个。"
)
_STATUS_DESC = (
    "看这份长文档攒到哪了：段序 / 每段标题 / 字数 / 累计字数 / 是否已成稿。"
    "成稿前用它自检有没有漏段，别凭记忆判断。"
)
_FINISH_DESC = (
    "把同一 doc_id 下所有分段原文按序拼成**一份完整文档**，一次写入一个文件。"
    "成稿是纯拼接：不加分隔线、不加段号、不摘要、不改写、不截断；"
    "生成可以分段，产物只能是一个文件——别再改用 write_file 分文档写。"
    "path 是成稿文件名（相对会话目录，如「萤窗案-卷纲.md」），title 可选。"
    "只有调用本工具之后才算交付，之前别告诉作者写完了。"
)


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


_SCHEMAS = {
    "document_append": _schema(
        "document_append",
        _APPEND_DESC,
        {
            "doc_id": {"type": "string", "description": "这份文档的稳定标识，全程用同一个。"},
            "section": {"type": "string", "description": "这一段的小标题，只用于索引，不写进成品。"},
            "content": {"type": "string", "description": "这一段的完整成品原文（逐字保存）。"},
        },
        ["doc_id", "section", "content"],
    ),
    "document_status": _schema(
        "document_status",
        _STATUS_DESC,
        {"doc_id": {"type": "string", "description": "要查看的文档标识。"}},
        ["doc_id"],
    ),
    "document_finish": _schema(
        "document_finish",
        _FINISH_DESC,
        {
            "doc_id": {"type": "string", "description": "要成稿的文档标识。"},
            "path": {"type": "string", "description": "成稿文件名，落在会话可写目录内。"},
            "title": {"type": "string", "description": "可选：文档一级标题。"},
        },
        ["doc_id", "path"],
    ),
}


# ------------------------------------------------------------------ 路径与落盘

def _writable_roots(roots: Optional[list]) -> list[Path]:
    out: list[Path] = []
    for r in roots or []:
        try:
            if bool(getattr(r, "writable", False)):
                out.append(Path(str(getattr(r, "path", r))).expanduser().resolve())
        except (OSError, RuntimeError, ValueError):
            continue
    return out


def _draft_base(workspace: Path, roots: Optional[list]) -> Path:
    """草稿只放会话自己的 scratch；没有 scratch 时退到 /tmp 的会话隔离目录，
    绝不往用户 repo 里塞中间文件。"""
    for r in roots or []:
        try:
            if bool(getattr(r, "writable", False)) and getattr(r, "label", "") == "scratch":
                return Path(str(r.path)).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
    wr = _writable_roots(roots)
    if wr:
        return wr[0]
    key = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / "coworker-docbuild" / key


def _doc_dir(base: Path, doc_id: str) -> Path:
    return base / _DRAFT_DIR / doc_id


@contextmanager
def _locked(doc_dir: Path) -> Iterator[None]:
    """同一 doc_id 的 index 与段号分配要互斥（多入口写同文件须 flock，项目红线）。"""
    doc_dir.mkdir(parents=True, exist_ok=True)
    lock_path = doc_dir / _LOCK_NAME
    if fcntl is None:
        yield
        return
    with lock_path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, text: str) -> None:
    """统一走 deskmate/growth/_atomic.py：唯一临时文件 + os.replace。
    延迟导入，避开 catalog 的 import 期循环。"""
    from deskmate.growth._atomic import atomic_write_text

    atomic_write_text(path, text)


def _read_index(doc_dir: Path) -> dict[str, Any]:
    p = doc_dir / _INDEX_NAME
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("sections"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"sections": [], "finished_path": "", "finished_chars": 0}


def _write_index(doc_dir: Path, data: dict[str, Any]) -> None:
    _atomic_write(doc_dir / _INDEX_NAME, json.dumps(data, ensure_ascii=False, indent=2))


def _resolve_target(path: str, workspace: Path, roots: Optional[list]) -> Optional[Path]:
    try:
        p = Path(str(path)).expanduser()
        if not p.is_absolute():
            p = Path(workspace) / p
        p = p.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    allowed = _writable_roots(roots) or [Path(workspace).resolve()]
    for a in allowed:
        if p == a or a in p.parents:
            return p
    return None


# ------------------------------------------------------------------ 工具工厂

def document_tools(workspace: Path, roots: Optional[list] = None) -> list:
    """造 3 个长文档工具。草稿与成稿都被钳在本会话的 writable roots 内。"""
    ws = Path(workspace)
    shared_roots = roots  # 与 PermissionEngine 共享同一份可变列表（运行时加目录可见）

    def document_append(doc_id: str, section: str, content: str) -> str:
        did = str(doc_id or "").strip()
        if not _DOC_ID_RE.fullmatch(did) or ".." in did:
            return "doc_id 不合法：只能用中英文、数字、下划线、连字符，最长 64 字，不能带路径。"
        if not isinstance(content, str) or not content.strip():
            return "没收到这一段的正文，什么都没写入。"

        base = _draft_base(ws, shared_roots)
        doc_dir = _doc_dir(base, did)
        with _locked(doc_dir):
            idx = _read_index(doc_dir)
            n = len(idx["sections"]) + 1
            _atomic_write(doc_dir / f"{n:04d}.part", content)  # 逐字，不 strip
            label = str(section or f"第{n}段").strip()
            idx["sections"].append({"n": n, "title": label, "chars": len(content)})
            _write_index(doc_dir, idx)
        total = sum(int(s.get("chars", 0)) for s in idx["sections"])
        return (
            f"已收第 {n} 段「{label}」（{len(content)} 字，原文逐字保存）。"
            f"当前共 {n} 段 / {total} 字；**尚未成稿**，别告诉作者已经写完。"
            "全部段收齐、用 document_status 自检后，再调 document_finish 一次成稿。"
        )

    def document_status(doc_id: str) -> str:
        did = str(doc_id or "").strip()
        if not _DOC_ID_RE.fullmatch(did) or ".." in did:
            return "doc_id 不合法：不能带路径。"
        doc_dir = _doc_dir(_draft_base(ws, shared_roots), did)
        idx = _read_index(doc_dir)
        sections = idx["sections"]
        if not sections:
            return f"「{did}」还没有任何分段。用 document_append 开始攒原文。"
        total = sum(int(s.get("chars", 0)) for s in sections)
        lines = [f"「{did}」共 {len(sections)} 段 / {total} 字"]
        if idx.get("finished_path"):
            lines.append(f"已成稿一次：{idx['finished_path']}（{idx.get('finished_chars', 0)} 字）")
        else:
            lines.append("尚未成稿（产物还不存在，别告诉作者写完了）")
        lines.append("段序：")
        for s in sections:
            lines.append(f"  {s.get('n')}. {s.get('title', '')}（{s.get('chars', 0)} 字）")
        return "\n".join(lines)

    def document_finish(doc_id: str, path: str, title: str = "") -> str:
        did = str(doc_id or "").strip()
        if not _DOC_ID_RE.fullmatch(did) or ".." in did:
            return "doc_id 不合法：不能带路径。"
        doc_dir = _doc_dir(_draft_base(ws, shared_roots), did)
        with _locked(doc_dir):
            idx = _read_index(doc_dir)
            sections = idx["sections"]
            if not sections:
                return "这份文档还没有任何分段，先 document_append 攒原文，没有内容不能成稿。"
            parts: list[str] = []
            for s in sections:
                part = doc_dir / f"{int(s.get('n', 0)):04d}.part"
                if not part.is_file():
                    return f"第 {s.get('n')} 段草稿缺失（{part.name}），先补回这一段再成稿。"
                # 只去掉尾部空行；可见文字一个字都不动
                parts.append(part.read_text(encoding="utf-8").rstrip("\n"))

            target = _resolve_target(path, ws, shared_roots)
            if target is None:
                return "路径不允许：成稿必须落在本次会话的可写目录内，不能写到目录外。"
            try:
                from deskmate.sacred_paths import guarded_write
            except Exception:  # noqa: BLE001 - 守卫缺失时不能静默放行
                return "路径守卫不可用，拒绝成稿（fail-closed）。"
            if not guarded_write(target):
                return "拒绝写入：这个目录是作者原稿（含 .deskmate 圣经），原稿正文绝不直改写。"

            body = _JOIN.join(parts)
            head = str(title or "").strip()
            if head:
                body = f"# {head}\n\n{body}"
            _atomic_write(target, body)
            idx["finished_path"] = str(target)
            idx["finished_chars"] = len(body)
            _write_index(doc_dir, idx)

        return (
            f"已成稿：1 个文件 / {len(parts)} 段 / {len(body)} 字 → {target}。"
            "各段原文按序拼接，未截断、未摘要、未改写，也没有插入分隔线或段号。"
            "现在才可以告诉作者写完了。"
        )

    for fn, approval in (
        (document_append, False),
        (document_status, False),
        (document_finish, True),
    ):
        name = fn.__name__
        fn.__name__ = name
        fn.__doc__ = _SCHEMAS[name]["function"]["description"]
        fn.__aisuite_tool_metadata__ = ai.ToolMetadata(
            name=name,
            category="document",
            risk_level="low",
            capabilities=["document"],
            requires_approval=approval,
        )
        fn.__coworker_schema__ = _SCHEMAS[name]

    return [document_append, document_status, document_finish]
