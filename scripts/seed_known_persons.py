#!/usr/bin/env python3
"""P2-R1 种子脚本 · LLM 扫「已出版」书稿 → 抽出人名灌 known_persons（直接 active）。

为什么需要
----------
P1 的 person 锚原先靠 env ``DESKMATE_PERSON_NAMES`` 手工逗号列表 + 子串匹配——
对「人物浩如烟海、持续增长」的历史写作来说等于默认沉睡（萧哥：得配好，不然白做了）。
本脚本用书单的正文给注册表打底（``source='seed'``、``status='active'``），之后
运行时再靠「模式 B 自生长」把 LLM 新发现的人名（``source='auto'``）慢慢养起来。

红线（务必遵守）
----------------
* **只读**：绝不写 ``~/Documents/已出版/**`` 下任何原稿正文（含 ``.deskmate/``）；
  本脚本只读正文、只往 deskmate.db 的 ``known_persons`` 表写人名。
* fail-open：单本读失败 / 单块 LLM 抽失败 → 告警跳过，不中断整批。

书单来源
--------
复用 ``scripts/build_scholar_index.py`` 的显式书单 ``DEFAULT_BOOKS``（不自动扫目录，
避免混入合同/样张/目录页），另纳入萧哥小说《20.国子监秘闻录》（含人物小传）。

用法
----
    python3 scripts/seed_known_persons.py                 # 真跑：抽人名并灌库
    python3 scripts/seed_known_persons.py --dry-run       # 只抽不写：打印人名
    python3 scripts/seed_known_persons.py --book 国子监   # 只跑书名含「国子监」的
    python3 scripts/seed_known_persons.py --limit-chunks 5  # 每本只抽前 5 块（试跑省钱）
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 两端一致：脚本在 scripts/ 下，仓库根与其父目录都要在 sys.path
_SCRIPTS = Path(__file__).resolve().parent
_REPO = _SCRIPTS.parent
for _p in (str(_REPO), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from build_scholar_index import (  # noqa: E402
    DEFAULT_BOOKS,
    PUBLISHED,
    extract_docx,
    split_chunks,
)

# 国子监小说（萧哥作品）——人物也是人名，小桔读稿本就在用；seed 仅只读抽人名。
# 人物小传是显式人名清单，一并纳入（零 LLM 依赖也能贡献种子）。
EXTRA_BOOKS: tuple[tuple[str, str], ...] = (
    ("20.国子监秘闻录/国子监秘闻录.docx", "国子监秘闻录"),
    ("20.国子监秘闻录/国子监秘闻录_人物小传.md", "国子监秘闻录·人物小传"),
)
SEED_BOOKS: tuple[tuple[str, str], ...] = tuple(DEFAULT_BOOKS) + EXTRA_BOOKS

PERSON_SYSTEM_PROMPT = """你是「人名抽取器」。从下面这段文字里抽出**专有的人名**（历史人物或文学作品中的人物均可），包含名、字、号等常用称谓。

规则：
1. 只抽具体的人物名，不要抽「他/她/众人/作者/我们/朝廷/百官」等泛指代词或群体；
2. 不要抽地名、书名、官职（除非与人物名不可分）；
3. 同一人只输出一个最常用的名字；
4. 只输出严格 JSON，不要任何其他文字：{"persons": ["名1", "名2"]}
   若本段没有人物名，输出 {"persons": []}"""

# 离谱过滤：人名一般在 2-12 字，且不含空白/标点（防 LLM 偶尔输出整句）。
_BAD_CHARS = set(" \t\n\r，。、；：（）()《》<>「」【】,.;:!?\"'")


def _read_text(path: Path) -> str:
    """只读正文：.docx 走零依赖解析；.txt/.md 做编码探测（仓库实测多 GBK 系）。"""
    if path.suffix.lower() == ".docx":
        text, _ = extract_docx(path)
        return text or ""
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _clean_name(name: str) -> str:
    """轻量清洗：去空白、剔标点/整句，保留合理长度的人名。"""
    n = (name or "").strip()
    if not n or len(n) < 2 or len(n) > 12:
        return ""
    if any(c in _BAD_CHARS for c in n):
        return ""
    return n


def _extract_persons_from_chunk(chunk: str, chat) -> list[str]:
    """单块 → 人名列表（任何异常向上抛，由调用方 fail-open 跳过该块）。"""
    from deskmate.cognitive.extractor import _extract_json

    resp = chat(
        system=PERSON_SYSTEM_PROMPT,
        user=f"文字内容：\n---\n{chunk}\n---\n\n只输出 JSON。",
        temperature=0.0,
        max_tokens=600,
        timeout=180.0,
    )
    data = _extract_json(resp)
    persons = data.get("persons", []) if isinstance(data, dict) else []
    if not isinstance(persons, list):
        return []
    return [c for c in (_clean_name(p) for p in persons if isinstance(p, str)) if c]


def _migrate_known_persons(store) -> None:
    """先迁移 known_persons（装触发器），再灌种子 → 新行自动落 oplog、可跨端同步。

    fail-open：迁移失败只告警——本地仍能灌，跨端稍后由 daemon 的 migrate 补齐。
    """
    try:
        from deskmate.memory.sync_store_sqlite import migrate

        with store._connect() as conn:
            node = "mac"
            try:
                r = conn.execute("SELECT node FROM _sync_meta WHERE id=1").fetchone()
                if r and r[0]:
                    node = r[0]
            except Exception:  # noqa: BLE001 —— 无 _sync_meta（首跑）→ 用默认 node
                pass
            migrate(conn, node, "known_persons")
    except Exception as e:  # noqa: BLE001 —— fail-open：迁移失败不影响本地灌库
        print(f"[警告] known_persons 迁移失败（本地仍灌，跨端稍后补）: {e}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="LLM 扫已出版书 → 灌 known_persons 人名种子")
    ap.add_argument("--dry-run", action="store_true", help="只抽不写库，打印人名")
    ap.add_argument("--book", default="", help="只跑书名/路径含该子串的书")
    ap.add_argument("--limit-chunks", type=int, default=0,
                    help="每本最多抽前 N 块（0=不限，试跑省钱）")
    args = ap.parse_args(argv)

    from deskmate.memory.llm import chat, llm_available

    if not llm_available():
        print("[中止] LLM 不可用（未配 provider / 无密钥），无法扫描抽人名。")
        return 1

    books = [(rel, title) for rel, title in SEED_BOOKS
             if not args.book or args.book in rel or args.book in title]
    print(f"书单根目录：{PUBLISHED}")
    print(f"待扫 {len(books)} 本"
          f"{'（dry-run，不写库）' if args.dry_run else ''}\n")

    all_names: set[str] = set()
    for rel, title in books:
        path = PUBLISHED / rel
        if not path.is_file():
            print(f"  [缺失] {path}")
            continue
        if ".deskmate" in str(path):
            print(f"  [跳过] 命中 .deskmate 守卫：{path}")
            continue
        try:
            text = _read_text(path)
        except Exception as e:  # noqa: BLE001 —— 单本读失败跳过
            print(f"  [读取失败] {title}: {e}")
            continue
        if not text.strip():
            print(f"  [跳过] 无正文：{title}")
            continue

        chunks = split_chunks(text)
        if args.limit_chunks > 0:
            chunks = chunks[: args.limit_chunks]
        found: set[str] = set()
        n_fail = 0
        for _heading, body in chunks:
            # 重试 3 次：撞付费配额(429)退避后重试，仍是真失败才 fail-open 跳过
            for attempt in range(3):
                try:
                    found.update(_extract_persons_from_chunk(body, chat))
                    break
                except Exception as e:  # noqa: BLE001 —— 单块抽失败
                    if attempt < 2:
                        time.sleep(15 * (attempt + 1))  # 退避重试，避免瞬间刷爆配额
                        continue
                    n_fail += 1
                    if n_fail <= 3:
                        print(f"    [块抽失败] {title}: {e}")
            time.sleep(0.3)  # 节流：块间错峰，避免 1000+ 调用瞬间撞火山 5h 配额
        all_names.update(found)
        print(f"  ✓ {title:<32} {len(chunks):>4} 块 → {len(found):>3} 人"
              f"{f'（{n_fail} 块失败）' if n_fail else ''}")

    print(f"\n去重后共 {len(all_names)} 个人名。")
    if args.dry_run:
        print("样例（前 40 个）：", sorted(all_names)[:40])
        return 0

    from deskmate.cognitive.store import CognitiveStore

    store = CognitiveStore()
    _migrate_known_persons(store)
    added = store.seed_known_persons(sorted(all_names), "seed")
    total = len(store.list_active_persons())
    print(f"已灌入 known_persons：新增 {added} 条（幂等跳过 {len(all_names) - added} 条），"
          f"当前 active 共 {total} 条。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))