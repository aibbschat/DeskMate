"""LLM 输出卫生：剥离「推理标签」等非正文残留。

## 为什么需要

模型（尤其带思维链的网关 / 中转）有时把**思考过程**和**正文**一起吐出来，
中间只隔一个闭合标签。若不去掉，它会**跟着正文一起落盘**：

- 2026-09-21 现场：``~/.deskmate/diary/2026-09-21.md`` 的 ``10:32`` 则，
  正文前有一段 700 余字的"预述"（把同一件事先讲了一遍），末尾带一个孤立的
  ``</think>``（开标签被上游截掉了，只剩闭合的）。全库仅此 1 处，属偶发。

日记是**给他的私密记述**，混进"模型的草稿纸"是质量事故；对外发帖更是。
所以清洗放在**字段级**（每段叙事 / 每条观察各自过一遍），
绝不在**整篇文档**上做——文档级剥离会连带删掉本则之前的所有正常内容。

## 剥什么

1. **成对推理块** ``<think ...>...</think>``（含 thinking / reasoning / analysis /
   scratchpad 同族标签，大小写不敏感）→ 整块删除。
2. **特殊 token** ``<|im_start|>`` / ``<|endoftext|>`` 之类 → 删除。
3. **孤立闭合标签** ``</think>``（没有配对的开始标签）→ 把它**之前**的内容当推理
   丢掉，只留它后面那段。这正是模型"先想后答、开标签被上游吃掉"的形态。
4. **孤立开始标签** ``<think>``（没有配对闭合）→ 从它开始到结尾都不算正文。

## 安全阀

第 3 条是唯一有"删除"语义的操作，所以设了两道防线：

- 若剥离后**什么都不剩**，说明判断错了（那其实是正文）→ 退回"只删标签、不删内容"。
- 若剥离掉的**占比 > 90%** 而剩下的很短同样可疑，同样退回。
  宁可留一点脏，不可吃掉他的字。

返回 ``(cleaned, dropped)``：``dropped`` 为是否发生过内容剥离，
调用方据此决定要不要留一条 warning（可观测性）。
"""
from __future__ import annotations

import re

# 推理家族标签名（``thinking`` / ``reasoning`` 等网关各有各的叫法）
_FAMILY = r"(?:think|thinking|reasoning|analysis|scratchpad)"

_BLOCK_RE = re.compile(rf"<\s*{_FAMILY}\b[^>]*>.*?<\s*/\s*{_FAMILY}\s*>", re.I | re.S)
_OPEN_RE = re.compile(rf"<\s*{_FAMILY}\b[^>]*>", re.I)
_CLOSE_RE = re.compile(rf"<\s*/\s*{_FAMILY}\s*>", re.I)
# 特殊 token：``<|im_start|>`` ``<|endoftext|>`` ``<|assistant|>`` …
_SPECIAL_RE = re.compile(r"<\|[^|>\n]{0,64}\|>")

# 剥离后剩下的正文若短于此长度且占比过低，判定为"判断错了"，回退
_MIN_KEEP_CHARS = 20
_MIN_KEEP_RATIO = 0.1


def has_reasoning_artifacts(text: str) -> bool:
    """文本里是否含推理标签 / 特殊 token（用于质检扫描，不改内容）。"""
    s = text or ""
    if not s:
        return False
    return bool(
        _BLOCK_RE.search(s)
        or _OPEN_RE.search(s)
        or _CLOSE_RE.search(s)
        or _SPECIAL_RE.search(s)
    )


def strip_reasoning_artifacts(text: str) -> tuple[str, bool]:
    """剥离推理残留。返回 ``(清洗后文本, 是否有内容被剥掉)``。

    ``text`` 为空时原样返回 ``("", False)``；无残留时**逐字节原样返回**，
    避免正常路径被无谓地改写（与 ``manager._strip_diary_sections`` 同一原则）。
    """
    if not text:
        return text or "", False
    text = str(text)

    if not has_reasoning_artifacts(text):
        return text, False

    cleaned = _BLOCK_RE.sub("", text)
    cleaned = _SPECIAL_RE.sub("", cleaned)

    # 孤立的开始标签：从它起到结尾都算推理（正常模型不会把正文塞在 begin 之内）
    m = _OPEN_RE.search(cleaned)
    if m:
        cleaned = cleaned[: m.start()]

    # 孤立的闭合标签：它之前的都是推理，只留它之后那段（取最后一个，取最长剥离）
    last = None
    for last in _CLOSE_RE.finditer(cleaned):  # noqa: B007 - 需要最后一次匹配
        pass
    if last is not None:
        tail = cleaned[last.end():]
        # 安全阀：剥完几乎不剩东西，说明这段本来就是正文，只删标签不删内容
        if len(tail.strip()) >= _MIN_KEEP_CHARS and (
            len(tail) >= len(cleaned) * _MIN_KEEP_RATIO
        ):
            cleaned = tail
        else:
            cleaned = _CLOSE_RE.sub("", cleaned)

    return cleaned, cleaned != text
