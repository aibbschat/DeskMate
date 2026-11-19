"""群级共享材料面 —— 群里放一份，各 bot 自取。

萧哥 11-06 的「群共享文件」形态：群主在群里发一个文件，各人自己去读，
不需要一个一个发。这里把「找材料」从**每个 bot 的在线自主决策**上移为
**群级一次性本地 IO 准备**：一次备料，N 位 bot 共用同一份原文出处。

为什么这一层能同时保质、省时、省钱（全部为 11-06 本机实测，见 docs §16）：
- 旧 ``BookShelf.read`` 按文件名排序，《夜不收》前 12000 字是授权协议／粗纲／
  大纲，230KB 正文排第 9 ⇒ bot 第一次「读《夜不收》」读到的是合同，
  只能反复试探 ``chapter`` 撞正文。**既是质量事故，也是 R 轮工具往返的源头**。
- 每次工具往返都要重付一轮长思考（ reasoning 模型默认 60~90s 首 token，
  同题一次直出 329.4s / 20208 completion token，其中 thinking 27194 字）。
  砍掉「找文件」往返不动最后那一轮产生质量的深度思考。

硬红线（保质优先，违反即回退）：
- **共享的是原文分块 + 文件索引，绝不现场做 LLM 抽取／有损压缩／纪要**；
  零 LLM、纯本地 IO、只读走 ``sacred_paths``（复用 ``BookShelf`` 的守卫）。
- **不削真 Agent**：``read_book`` 仍在，bot 可随时按文件名关键词补读别的材料。
- 自然语言触发，零表单：书名从群里那句人话里抽，抽不到就不备料。
- 不改思考强度、不压正文字数、不减唤醒人数。

§17 补充（11-06 21:25 萧哥定调）：本层还会把**小桔已经落盘的既有沉淀**
（逐日圣典／进度简报／逐字留档／文风画像）原样摆进同一份群共享材料。
那是她每天读过、早已存在的文件，性质是「群里多放一份」，不是本次为讨论
生成的压缩物 ⇒ 不触发上面那条红线，红线原文不改，详见 docs §17.0。

§18（11-08 萧哥定调）：**大纲／粗纲等规划类文档一律降级为「只进索引」**。
它们本来只是参考，若还按里面的旧章纲／旧结局去想，脑洞就停在既有层面，
等于把废弃规划又喂回群聊。现在 body 只装 ``classify==0`` 的正文／小说文本；
参考类与协议类都只列在索引里，bot 真需要时仍可 ``read_book(chapter=关键词)``
自主点名补读（不削真 Agent）。正文 docx 一份不减，仍是全量原文分块。
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .experience import collect_experience, experience_files, load_style_profile
# 分类规则的**单一事实源**在 reading.py（BookShelf.read 留空补读也用它）；
# 这里 re-export，保持 ``from deskmate.bots.materials import classify`` 兼容。
from .reading import BookShelf, classify, deep_read_chars, parse_book_title

# 备料正文预算：默认 80000 字（deep_read_chars，见 __init__）；书比预算长时按
# 「开头 + 停笔点」装、中间显式标出（reading.Manuscript.render）。env
# DESKMATE_BOT_MATERIAL_CHARS 仍可收紧。
# 经验层独立预算：435 万字的书桶绝不能整桶塞进 prompt。
_DEFAULT_EXPERIENCE_CHARS = 12_000
_RELOAD_MARKS = ("重读", "重新读", "再读一遍", "刷新材料", "重新加载")


def _fingerprint(files: Sequence[Path]) -> str:
    """文件集合的 mtime+size 指纹；改稿后自动失效，不必人工清缓存。"""
    parts = []
    for p in files:
        try:
            st = p.stat()
            parts.append(f"{p.name}:{int(st.st_mtime)}:{st.st_size}")
        except OSError:
            parts.append(f"{p.name}:?:?")
    return "|".join(parts)


@dataclass(frozen=True)
class MaterialPack:
    """一次备料的产物。同一 room 的所有 bot 拿到完全相同的一份（字节一致）。"""

    book: str
    fingerprint: str
    body: str
    index: str
    experience: str = ""
    # 2026-11-19 PR12: 轻量失效判据（只 stat，不读正文/经验文件）。
    body_fingerprint: str = ""
    experience_meta: str = ""

    def prompt_block(self) -> str:
        """注入 prompt 的固定前缀。经验层在前，原文居中，索引在后。

        2026-11-08 读稿层重做：正文按「开头 + 停笔点」装，覆盖范围在索引里
        如实写清（多少字第几章、哪些章没加载）；**不再说「全书正文」**。
        """
        exp = self.experience + "\n\n" if self.experience else ""
        return (
            "# 群里已经共享的书稿材料（只读，可直接引用）\n\n"
            "下面的「正文」是已经写下的事实，**本次加载的覆盖范围见文末索引**"
            "（书比预算长时只装开头和结尾停笔点，中间没装的章会列出来，可点名补读）。"
            "大纲／粗纲／设定／简报等非正文材料默认不加载，也不许拿它们当既定框架"
            "或据此推断后续走向；要脑洞就以正文已写事实和作者最新裁定为准。\n\n"
            + exp
            + self.body
            + "\n\n"
            + self.index
        )


class RoomMaterials:
    """按 room 缓存的共享材料。纯内存：群是随建随撤的临时容器，服务重启重新备料
    只需 0.04s 本地 IO，落库反而引入持久化与原子写风险（守项目红线）。

    ``prepare()`` 每次都重算 fingerprint：改稿后下一次发言自动换新，
    不需要用户记着说「重读」；显式说「重读」只是强制刷新同一份。
    """

    def __init__(
        self,
        shelf: BookShelf,
        *,
        max_chars: int | None = None,
        experience_chars: int | None = None,
        style_loader=None,
        experience_sections: Sequence[str] | None = None,
    ) -> None:
        self.shelf = shelf
        # 2026-11-08 读稿层：正文预算默认 80000 字（deep_read_chars）。书比预算
        # 长时不再从头硬砍到 80%，而是「开头 + 结尾（停笔点）」各装一段、中间
        # 显式标出（reading.Manuscript.render）；覆盖范围写进索引，不再说
        # 「已加载全书」。DESKMATE_BOT_MATERIAL_CHARS（200~80000）仍可收紧。
        raw = os.environ.get("DESKMATE_BOT_MATERIAL_CHARS")
        self.max_chars = int(raw) if (raw and raw.isdigit() and 200 <= int(raw) <= 80_000) else (
            max_chars if max_chars is not None else deep_read_chars()
        )
        raw_exp = os.environ.get("DESKMATE_BOT_EXPERIENCE_CHARS")
        if experience_chars is not None:
            self.experience_chars = int(experience_chars)
        elif raw_exp and raw_exp.isdigit() and 200 <= int(raw_exp) <= 30_000:
            self.experience_chars = int(raw_exp)
        else:
            self.experience_chars = _DEFAULT_EXPERIENCE_CHARS
        self.style_loader = style_loader or load_style_profile
        # 2026-11-08 读稿层：经验层默认**只留作者裁定**（萧哥「以正文为准」）。
        # 简报／圣典／文风可能过时、且与正文打架（实测「进仓只有码头一条路」
        # vs 正文两道门），默认只列索引、要点名才读；作者裁定是他本人最新
        # 口径，留着防「死序」污染。传 None 保持旧行为（全量）供测试/兼容。
        self.experience_sections: tuple[str, ...] | None = (
            tuple(experience_sections) if experience_sections is not None else ("ruling",)
        )
        self._cache: dict[str, MaterialPack] = {}
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 读
    def get(self, room_id: str) -> Optional[MaterialPack]:
        with self._lock:
            return self._cache.get(str(room_id))

    def drop(self, room_id: str) -> None:
        """撤群时清掉材料；消息历史不受影响（RoomStore 只打墓碑）。"""
        with self._lock:
            self._cache.pop(str(room_id), None)
            # 11-08 双轨材料时期的 :deep 遗留键，清了无害（幂等）。
            self._cache.pop(str(room_id) + ":deep", None)

    # ---------------------------------------------------------------- 备料
    def prepare(
        self,
        room_id: str,
        text: str,
        *,
        force: bool = False,
    ) -> Optional[MaterialPack]:
        """从群里一句人话备料；抽不到书名时沿用该群已有材料（不猜、不回退路径）。

        2026-11-08 读稿层（萧哥「以正文为准，读到停笔点」）：正文预算默认
        ``deep_read_chars()``（80000）；书比预算长时自动装「开头 + 结尾停笔点」，
        中间章节显式列出缺口（索引里的「本次未加载」），bot 可点名补读。
        **不再有任何「已加载全书」的假话**；深挖/普通材料双轨早已合一。
        """
        rid = str(room_id)
        force = force or self._is_reload(text)
        known = self.shelf.list_books()
        title = parse_book_title(text, known=known)
        with self._lock:
            cached = self._cache.get(rid)
        if not title:
            # 没提书名就沿用群里已放的那份；**但仍要验新鲜度**——作者改稿后
            # 下一句哪怕只是「接着聊」，也不能继续拿旧原文给 bot 用。
            if cached is None:
                return None
            title = cached.book

        book_dir = self.shelf.resolve(title)
        if book_dir is None:
            return cached
        files = self.shelf._chapter_files(book_dir)
        if not files:
            return cached

        # 2026-11-19 PR12：先用纯 stat 指纹做一次廉价命中——正文与经验文件都没动
        # 就直接返回缓存，不再白读 ruling/brief 与文风画像（旧实现每句群消息都读）。
        body_fp = _fingerprint(files)
        exp_files = experience_files(book_dir)
        exp_meta = self._experience_meta(exp_files)
        if (
            os.environ.get("DESKMATE_MATERIALS_FASTPATH", "1") != "0"
            and not force
            and cached is not None
            and cached.book == title
            and cached.body_fingerprint == body_fp
            and cached.experience_meta == exp_meta
        ):
            return cached

        # 群级一次备料：小桔既有沉淀也纳入同一份指纹，改稿 / 新简报 / 书桶
        # 补集后下一句哪怕只是「接着聊」也自动换新。
        exp = collect_experience(
            book_dir,
            max_chars=self.experience_chars,
            style_loader=self.style_loader,
            sections=self.experience_sections,
        )
        fingerprint = body_fp
        if exp.has_any:
            fingerprint += "|exp:" + exp.fingerprint
        if (
            not force
            and cached is not None
            and cached.book == title
            and cached.fingerprint == fingerprint
        ):
            return cached

        pack = self._build(
            title, files, fingerprint, exp,
            body_fingerprint=body_fp, experience_meta=exp_meta,
        )
        with self._lock:
            self._cache[rid] = pack
        return pack

    @staticmethod
    def _is_reload(text: str) -> bool:
        return any(m in str(text or "") for m in _RELOAD_MARKS)

    def _experience_meta(self, exp_files: Sequence[Path]) -> str:
        """经验文件的廉价失效判据：只 stat 文件 + 读文风计数器（不读正文）。

        文风桶的 ``updates/chars`` 就是 ``experience._fingerprint`` 里的
        ``style:`` 段，这里复刻同一判据，避免「补了一轮文风却继续拿旧材料」。
        没有 .deskmate 沉淀时不读用户文风库（与 collect_experience 的隔离承诺一致）。
        """
        if not exp_files:
            return ""
        meta = _fingerprint(exp_files)
        try:
            profile = (self.style_loader or load_style_profile)() or {}
        except Exception:
            profile = {}
        bucket = (profile.get("buckets") or {}).get("manuscript") or {}
        ver = "|".join(
            str(bucket.get(k) or profile.get(k) or "") for k in ("updates", "chars")
        )
        return meta + "|style:" + ver

    def _build(
        self,
        title: str,
        files: Sequence[Path],
        fingerprint: str,
        exp=None,
        *,
        body_fingerprint: str = "",
        experience_meta: str = "",
    ) -> MaterialPack:
        # 正文优先；同类内仍按文件名稳定排序，保证跨 bot 字节一致。
        # 2026-11-08 读稿层：正文按章切分后按「开头 + 停笔点」装（见 reading.Manuscript），
        # 覆盖范围（多少字 / 哪些章没加载）写进索引，不再谎称「已加载全书」。
        ranked = sorted(files, key=lambda p: (classify(p.name), p.name))
        manuscript = self.shelf.manuscript(title)
        coverage = manuscript.render(self.max_chars) if manuscript is not None else None
        body = coverage.body if coverage is not None else "（这本书里没有可加载的正文材料。）"

        total_note = (
            f"；正文共 {manuscript.total:,} 字 / {len(manuscript.chapters)} 章"
            if manuscript is not None
            else ""
        )
        lines = [f"《{title}》书稿目录（共 {len(files)} 份{total_note}）："]
        for p in ranked:
            label = {
                0: "正文/小说（已写正文，参与本次加载）",
                1: "参考·大纲/粗纲/人物/脑洞（既有规划，未加载，可点名补读）",
                2: "协议/简介/目录（非创作材料，未加载）",
            }[classify(p.name)]
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            lines.append(f"- {p.name}（{label}，{size} 字节）")
        if manuscript is not None and coverage is not None:
            loaded = set(coverage.loaded_indexes)
            partial = set(coverage.partial_indexes)
            lines.append("正文分章（本次加载范围见文末；标未加载/部分加载的章可点名补读）：")
            for chapter in manuscript.chapters:
                if chapter.index in partial:
                    state = " · 部分加载"
                elif chapter.index in loaded:
                    state = ""
                else:
                    state = " · 本次未加载"
                lines.append(f"- {chapter.heading}（{chapter.chars:,} 字{state}）")
            lines.append(coverage.note)
        lines.append(
            "需要上面某一份而本次没加载的内容时，用 read_book 的 chapter 参数填"
            "**章节**（如 chapter=\"第十一章\" / \"第五到第七章\"）或"
            "**文件名关键词**（例如 chapter=\"绝命救援\"）单独补读；"
            "chapter 不是路径，填绝对路径不会命中。没读到的部分不要凭空编情节或段号。"
        )
        lines.append(
            "注意：大纲／粗纲／设定等参考文档是**既有规划**，既不是已写正文，也不是"
            "作者终裁。讨论新走向或要脑洞时不要拿它们当既定框架，更不要据此推断"
            "后续情节、章序或人物命运；需要以正文已写事实和作者最新裁定为准。"
        )
        if exp is not None and exp.has_any:
            lines.append("")
            lines.append("小桔已经读过并逐日维护的沉淀（同属群共享材料，可点名补读）：")
            lines.extend(exp.index_lines)
        return MaterialPack(
            book=title,
            fingerprint=fingerprint,
            body=body,
            index="\n".join(lines),
            experience=exp.text if exp is not None else "",
            body_fingerprint=body_fingerprint,
            experience_meta=experience_meta,
        )


def default_materials(shelf: BookShelf) -> RoomMaterials:
    return RoomMaterials(shelf)
