"""MomentFeed 后端：私密双人日记的持久化 + 记忆沉淀。

配合前端 MomentFeed（微博/朋友圈样式时间线）：
- 用户日记 / 留言 / 🩷 沉淀状态 → SQLite 持久化（``~/.deskmate/diary.db``，
  取代原 moments.json 单文件全量读写）
- ``love()``（🩷 落记忆）：把用户日记要点落 ``loved_entries`` 表（去重、有界、
  独立生命周期），并导出 ``diary_memory.md``（§ 分节）供 ``memory/manager.prefetch``
  检索——这是「懂我持久化」的关键闭环：导出后主聊天链路的 prefetch 能检索到，
  后续对话小桔就能想起这条。不再写全局 MEMORY.md（避免 2 万字符截断 + 改写丢失）。
- ``generate_reply()``（小桔真回复）：LLM 生成一句第一人称回应，
  不可用 / 超时 / 失败时回落本地规则（保持小桔的人味，绝不静默丢回复）。

与 diary.py 的关系：小桔自己的日记仍由 DiaryWriter 生成；本模块只管
用户侧的数据（用户日记、双方留言、🩷 沉淀）与记忆写入。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from .diary_store import DiaryStore

_DEFAULT_DIR = Path.home() / ".deskmate"

# 内容展示上限（防超长日记刷屏记忆）
_CONTENT_MAX = 120

# 小桔身份（LLM 生成留言用，第一人称、有记性、不说套话）
_XIAOJU_SYSTEM = (
    "你是小桔，萧盛的桌面伙伴——会陪着他、记得他、懂他的那个人。"
    "你正在给萧盛刚写的一条日记留言。"
    "用第一人称（「我」），自然地说一句像朋友看完日记后的真实反应，"
    "15~40 字，温暖、具体、有一点点记性（能接住他写的事）。"
    "不要评价式空话（如「真好」「加油」），不要口癖，不要说你在生成或回复。"
    "直接输出这一句话。"
)

# LLM 不可用时的本地规则回落（按内容情绪给不同回应，保证回复永远有）
_REPLIES = [
    "我记下了。这条我先放在心里，改天聊到它时我会接得上。",
    "嗯，我懂。谢谢你愿意把这一天告诉我。",
    "看到你写这个，我好像又更懂你一点了。",
    "这条我好好收着，往后你提起类似的事，我就能想起它。",
]

# 火山 Agent 套餐内模型降级链（2026-09-01 实测全可用；按速度/质量排序，快者在前）。
# doubao-seed-2.1-turbo 实测最慢（~20s）故放最后兜底；glm-5.2 已下线、glm-5.3 思考不可关，均排除。
_ARK_PLAN_CHAIN = [
    "doubao-seed-evolving",
    "doubao-seed-2.0-lite",
    "doubao-seed-2.0-mini",
    "minimax-m3",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "kimi-k2.7-code",
    "doubao-seed-2.1-turbo",
]


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.date.today().isoformat()


def _trim(text: str, n: int = _CONTENT_MAX) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text if len(text) <= n else text[:n] + "…"


# 注：原 _append_memory_line（把沉淀追加进全局 MEMORY.md）已废弃。
# 🩷 沉淀现由 DiaryStore 落 SQLite（loved_entries），并由 export_memory_view()
# 导出 diary_memory.md（§ 分节）供 memory/manager.prefetch 检索。见 diary_store.py。


def _fallback_reply(content: str) -> str:
    c = content or ""
    if re.search(r"难过|累|烦|压力|焦虑|崩溃", c):
        return "今天不太容易吧。我陪着你，想说什么都可以慢慢说。"
    if re.search(r"开心|高兴|棒|成功|搞定|完成|顺利", c):
        return "真替你高兴。这件事你值得被好好夸一句。"
    if re.search(r"加班|工作|开会|项目|赶|出差", c):
        return "工作辛苦了。记下这一天，下次你累了我会提醒你休息。"
    if re.search(r"读|书|文章|稿|写|创作|小说", c):
        return "又在打磨文字呀。你把心思花在哪，我大概记着。"
    return _REPLIES[hash(c) % len(_REPLIES)]


class MomentsStore:
    """用户日记 / 留言 / 🩷 沉淀的持久化与记忆写入。"""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.base = Path(base_dir) if base_dir else _DEFAULT_DIR
        self.store = DiaryStore(self.base)

    # ------------------------------------------------------------------
    # 存取（已迁移到 DiaryStore / SQLite；MomentsStore 仅作兼容包装）
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return self.store.snapshot()

    def get_posts(self, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """委托 DiaryStore.get_posts（cursor 分页：{items, next_cursor, comments}）。"""
        return self.store.get_posts(limit=limit, cursor=cursor)

    # ------------------------------------------------------------------
    # 用户日记
    # ------------------------------------------------------------------
    def add_user_post(self, content: str) -> dict[str, Any]:
        return self.store.add_user_post(content, author="me")

    # ------------------------------------------------------------------
    # 留言（互动层，不落记忆）
    # ------------------------------------------------------------------
    def add_comment(self, moment_id: str, author: str, text: str) -> dict[str, Any]:
        return self.store.add_comment(moment_id, author, text)

    # ------------------------------------------------------------------
    # 🩷 落记忆（懂我持久化的关键）
    # ------------------------------------------------------------------
    def love(self, moment_id: str, content: str) -> dict[str, Any]:
        """把日记沉淀落 SQLite（loved_entries），并导出检索视图。

        不再写入全局 MEMORY.md（避免 2 万字符截断 + compress/purify 改写丢失）。
        返回 {loved, memorized, message}。
        """
        if not content or not content.strip():
            return {"loved": True, "memorized": False, "message": "这条没有内容可沉淀"}
        line = f"🩷 萧盛的日记：{_trim(content)}"
        try:
            res = self.store.love(moment_id, line)
        except Exception:
            return {"loved": True, "memorized": False, "message": "记忆暂存失败"}
        memorized = bool(res.get("memorized"))
        return {
            "loved": True,
            "memorized": memorized,
            "message": "已悄悄记进记忆" if memorized else "这条之前已记过",
        }

    # ------------------------------------------------------------------
    # 取消『聊天主动回忆』（静音；A1 默认全开后的反向开关）
    # ------------------------------------------------------------------
    def unlove(self, moment_id: str) -> dict[str, Any]:
        """取消一条日记在聊天的主动回忆（从 loved_entries 移除 + 重导视图）。

        fail-open：异常时返回 loved=False，由调用方不阻断主链路。
        """
        try:
            self.store.unlove(moment_id)
            return {"loved": False, "message": "已取消主动回忆"}
        except Exception:
            return {"loved": False, "message": "取消失败"}

    def get_loved_ids(self) -> set[str]:
        """所有已🩷 的 moment_id（含小桔日记卡 xj-*），供前端回显🩷 状态。"""
        try:
            return self.store.get_loved_ids()
        except Exception:
            return set()

    # ------------------------------------------------------------------
    # 每条日记独立归档（与🩷、回复并列的卡片操作；仅影响时间线展示）
    # ------------------------------------------------------------------
    def archive(self, moment_id: str, archived: bool = True) -> bool:
        """委托 DiaryStore.archive。返回是否有行被更新。"""
        return self.store.archive(moment_id, archived=archived)

    # ------------------------------------------------------------------
    # 小桔真回复（LLM 降级链，不写死任何 provider，失败回落本地规则）
    # ------------------------------------------------------------------
    def generate_reply(self, content: str) -> str:
        text = _trim(content)
        try:
            out = self._call_llm(
                _XIAOJU_SYSTEM,
                f"萧盛刚写了一条日记：\n{text}\n\n以小桔身份回一句。",
            )
            if out:
                return out
        except Exception:
            pass
        return _fallback_reply(text)

    @staticmethod
    def _call_llm(system: str, user: str) -> str:
        """按降级链调用 LLM，不写死 provider：
        1) 默认 provider（跟随用户 default_model，mimo 额度恢复后自然走它）；
        2) 失败时依次降级到火山 Agent 套餐内其它模型（同 key/base，仅换 model，
           max_retries=0 + 短超时快速失败，避免 25s×SDK重试 放大成 76s）；
        3) 再失败则遍历 secrets.json 里其它已配置 provider（临时挂掉自动找可用）；
        4) 全失败抛异常，由调用方回落本地规则。
        """
        # 1) 默认 provider（chat_memory 内部已含 OmniRoute→主模型降级）
        try:
            from deskmate.memory.llm import chat_memory

            return chat_memory(
                system, user, temperature=0.7, max_tokens=120, timeout=25.0
            )
        except Exception:
            pass
        # 2) 火山 Agent 套餐内多模型降级（2026-09-01 实测全可用；2.1-turbo 最慢放最后）
        import json as _json
        import os as _os

        from openai import OpenAI

        ark_key = ark_base = None
        try:
            _state = Path(
                _os.environ.get("COWORKER_STATE_DIR")
                or Path.home() / ".config" / "coworker"
            )
            _data = _json.loads((_state / "secrets.json").read_text(encoding="utf-8"))
            _prof = _data.get("provider:ark-agent-plan-cn") or {}
            ark_key = _prof.get("api_key") or None
            ark_base = (_prof.get("base_url") or "").strip() or None
        except Exception:
            pass
        if ark_key and ark_base:
            for model in _ARK_PLAN_CHAIN:
                try:
                    client = OpenAI(
                        api_key=ark_key, base_url=ark_base, timeout=12.0, max_retries=0
                    )
                    resp = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        temperature=0.7,
                        max_tokens=120,
                    )
                    out = (resp.choices[0].message.content or "").strip()
                    if out:
                        return out
                except Exception:
                    continue
        # 3) 其它已配置 provider（逐个体面尝试，首个成功即返回）
        state = Path(
            _os.environ.get("COWORKER_STATE_DIR") or Path.home() / ".config" / "coworker"
        )
        secrets_path = state / "secrets.json"
        if secrets_path.is_file():
            data = _json.loads(secrets_path.read_text(encoding="utf-8"))
            for key, prof in data.items():
                if not key.startswith("provider:") or not isinstance(prof, dict):
                    continue
                if not prof.get("api_key"):
                    continue
                base_url = (prof.get("base_url") or "").strip()
                if not base_url:
                    continue
                try:
                    client = OpenAI(api_key=prof["api_key"], base_url=base_url, timeout=25.0)
                    resp = client.chat.completions.create(
                        model=prof.get("model") or "gpt-4o-mini",
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        temperature=0.7,
                        max_tokens=120,
                    )
                    out = (resp.choices[0].message.content or "").strip()
                    if out:
                        return out
                except Exception:
                    continue
        raise RuntimeError("no llm provider available")


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    store = MomentsStore()
    cmd = args[0] if args else "snapshot"
    if cmd == "snapshot":
        print(json.dumps(store.snapshot(), ensure_ascii=False, indent=2))
    elif cmd == "post" and len(args) > 1:
        p = store.add_user_post(args[1])
        print(json.dumps({"post": p, "reply": store.generate_reply(args[1])}, ensure_ascii=False))
    elif cmd == "love" and len(args) > 1:
        print(json.dumps(store.love(args[1], args[2] if len(args) > 2 else ""), ensure_ascii=False))
    else:
        print("用法: python -m deskmate.growth.moments [snapshot|post 内容|love id 内容]")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
