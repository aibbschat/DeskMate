# ROUND-08-指令（P2-R3 lessons 注入）· 给 Trae 的可执行指令

> 上游验收：ROUND-07-QC PASS（P2-R2 截断已根治）。本指令为 P2 第三件，对齐 `reports/P2_实施工单_2026-10-07.md` §四。
> 协作：WorkBuddy=指挥员，Trae=一线；交付后读真实 diff + 跑测试出 QC。

## 0. 目标（一句话）
把"小桔从萧哥纠正/反馈里学到的条目（lessons）"每轮注入对话，让她在回话时带上这些认知，而不是每次从零来。限额：最近 3 条 + 全部 `scope=permanent` 规则，防挤爆上下文（呼应江河设计 §八③「lessons 占预算」）。

## 1. 数据源方案（拍板：新增 `teachings` 表，不复用 profile_items）
- 新建独立轻量表 `teachings`（与 P2-R1 `known_persons` 同源风格），**进 `SYNC_TABLES`** 跨端收敛（P2 工单 §二原则）。
- R3 落地**读 + 手动写入入口 `add_lesson`**（供萧哥/测试/未来 feedback 自动调用）；feedback 自动写入机制**留作 P3 子任务**，但写入端 = `add_lesson`，本指令不新开写入架构。
- 不碰 recall_items / extractor / person 注册表（P2-R1/R2 遗产保持）。

## 2. 落点 A — `deskmate/cognitive/store.py`（新增表 + 读写）
在 `known_persons` 表定义附近（约 L323 后）加：
```python
# P2-R3（2026-10-07）：lessons 表——小桔从萧哥纠正/反馈学到的条目，每轮注入对话。
# scope='permanent' 的规则常驻；其余为 recent，按 created_at 取最近 N 条。
CREATE TABLE IF NOT EXISTS teachings (
    lesson_id  TEXT PRIMARY KEY,         -- 确定性主键（见下）
    content    TEXT NOT NULL,            -- 学到的条目正文
    scope      TEXT DEFAULT 'recent',    -- 'recent' | 'permanent'
    source     TEXT DEFAULT 'manual',    -- 'manual' | 'feedback'
    created_at TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_teachings_scope ON teachings(scope);
CREATE INDEX IF NOT EXISTS idx_teachings_created ON teachings(created_at);
```
`lesson_id` 生成：`f"lesson::{hashlib.md5((content or '').strip().encode()).hexdigest()[:12]}"`（内容去重，幂等）。

新增方法（fail-open，与 known_persons 同风）：
```python
def add_lesson(self, content: str, scope: str = "recent", source: str = "manual") -> bool:
    """写入一条 lesson（手动/测试/未来 feedback 调用）。幂等（同内容同 scope 不重插）。"""
    c = (content or "").strip()
    if not c: return False
    lid = f"lesson::{hashlib.md5(c.encode()).hexdigest()[:12]}"
    ts = _now_iso()
    try:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO teachings(lesson_id, content, scope, source, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?)", (lid, c, scope, source, ts, ts))
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("add_lesson 失败（忽略）: %s", e); return False

def get_lessons_for_injection(self, recent_limit: int = 3, char_cap: int = 1200) -> list[str]:
    """返回注入用 lesson 正文列表：最近 recent_limit 条(recent) + 全部(permanent)。

    fail-open：表缺失/异常 → 返回 []。永久规则通常少，recent 限 3，总字数 cap 1200 防挤爆。
    """
    try:
        with self._connect() as conn:
            perm = [r[0] for r in conn.execute(
                "SELECT content FROM teachings WHERE scope='permanent' ORDER BY created_at DESC").fetchall()]
            rec = [r[0] for r in conn.execute(
                "SELECT content FROM teachings WHERE scope!='permanent' ORDER BY created_at DESC LIMIT ?",
                (recent_limit,)).fetchall()]
        out, n = [], 0
        for t in perm + rec:
            if n + len(t) > char_cap: break
            out.append(t); n += len(t)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("get_lessons_for_injection 失败（忽略）: %s", e); return []
```

## 3. 落点 B — `deskmate/memory/sync_store_sqlite.py`（SYNC 注册）
在 `SYNC_TABLES` dict 加 `teachings` 项（参照 R1 `memory_anchors` 注册写法）：
```python
"teachings": SyncTableSpec(
    pk="lesson_id", sync_key="lesson_id", sync_uuid_kind="string",
    sync_cols=["content", "scope", "source", "created_at", "updated_at"],
),
```
并在 `migrate` 里为 `teachings` 补 sync 三列（row_key / sync_uuid / sync_tombstone）同 `memory_anchors` 做法。

## 4. 落点 C — `deskmate/cognitive/memory_injector.py`（新增 build_lessons_context）
与已有 `build_cognitive_context` / `build_emotional_context` / `build_world_context` 风格一致：
```python
def build_lessons_context(base_dir: str | None = None, query: str = "") -> str:
    """读取 lessons 拼成注入块；空/异常返回 ''。"""
    try:
        from deskmate.cognitive.store import CognitiveStore
        from deskmate.paths import deskmate_base_dir
        store = CognitiveStore(base_dir=base_dir or deskmate_base_dir())
        lessons = store.get_lessons_for_injection()
        if not lessons: return ""
        lines = ["### 小桔学到的（来自你的纠正与反馈）"]
        for t in lessons:
            lines.append(f"- {t}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""  # fail-open
```
注：`CognitiveStore` 构造若需 `db_path`，按现有 R1 用法传 `base_dir`；若现有签名是 `CognitiveStore(db_path=...)`，则改用 `CognitiveStore(db_path=os.path.join(base_dir, "deskmate.db"))`——**以 store.py 实际签名为准**（R1 已验证 `CognitiveStore(db_path=...)` 可用，见 test_p1 夹具）。

## 5. 落点 D — `deskmate/memory/manager.py`（注入链，紧邻 delivered 块）
在 L654（`parts.append(delivered_ctx)` 的 try/except 之后）、L656 `if not parts:` 之前插入：
```python
# 「小桔学到的」（P2-R3 江河设计 §4.5 教学层）：从萧哥纠正/反馈学到的 lessons
# 每轮注入，让她回话带得上这些认知。fail-open 与上面各块一致；
# 总开关 DESKMATE_AUTO_INJECT=0 已覆盖（L748），本块随之关闭。
# 单独开关 DESKMATE_LESSONS_INJECT=0 可只关这一块。
try:
    if os.getenv("DESKMATE_LESSONS_INJECT", "1") != "0":
        from deskmate.cognitive.memory_injector import build_lessons_context
        lessons_ctx = build_lessons_context(self.base_dir, user_message)
        if lessons_ctx:
            parts.append(lessons_ctx)
except Exception:
    pass  # fail-open：lessons 缺失不影响主记忆注入
```

## 6. 测试（新增 `tests/test_p2_lessons.py`，fail-open + 限额 + 开关）
- `test_lessons_injection_recent_and_permanent`：seed 5 条 recent + 2 条 permanent → `get_lessons_for_injection()` 返回 = 2 permanent + 最近 3 recent（共 5），且含永久规则。
- `test_lessons_char_cap`：seed 大量长文 → 返回总字数 ≤ 1200。
- `test_lessons_empty`：空表 → `get_lessons_for_injection()` 返回 []；`build_lessons_context` 返回 ""。
- `test_lessons_auto_inject_off`：monkeypatch `DESKMATE_AUTO_INJECT=0` → `deskmate_auto_inject_context` 整体返回 ""（lessons 块随之关）。
- `test_lessons_fail_open`：monkeypatch `CognitiveStore.get_lessons_for_injection` 抛异常 → `build_lessons_context` 返回 ""（不抛）。
- `test_add_lesson_idempotent`：同内容加两次 → 表只有 1 行。

## 7. 验收（Trae 自测 + WorkBuddy re-QC）
1. `tests/test_p2_lessons.py` 全绿（新增 6 例）+ 回归 `test_p1_anchor_recall` / `test_p2_known_persons` / `test_deskmate_local_extractor` / `test_sync_store_sqlite` 仍绿。
2. 独立实测：`add_lesson("反馈：萧哥不要她替用户做主", scope="permanent")` → `get_lessons_for_injection()` 含该条；`recall_items` 行为不受影响（R2 遗产）。
3. `grep -n "teachings" deskmate/memory/sync_store_sqlite.py` 确认 SYNC 注册存在。
4. 红线：只改 store.py（teachings 表+方法）/ sync_store_sqlite.py（注册）/ memory_injector.py（新增函数）/ manager.py（注入块）/ tests（新增）；不改 recall_items 逻辑、extractor、person 注册表、前端；**不 commit**（R1+R2+R3 统一收口）。

## 8. 交付
写 `reports/bridge-trae/ROUND-08-实施记录.md`（真实落点 + 自测 + 独立实测），交 WorkBuddy re-QC。
