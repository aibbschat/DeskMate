"""P1-A 写入纪律：任何写 learned_history 的路径都不许「默认 live」。

对应 docs/memory-discipline-design.md §3（写入纪律）与 §8（门禁）：

1. 数据层兜底 —— ``LearnedStore.append`` 不传 ``source`` 时，按类别 / 措辞
   **逐条保守推断**（设定→setting、进度→progress、偏好→live），并自动补书维度；
   不再让旁路写入冒充「萧哥口述」进主块。
2. 调用点显式化 —— digest / recorder / app 三处旁路必须显式传 ``source``。
   用 AST 静态扫，防止后续新增调用点悄悄退回旧行为。
3. 分类口径无关 —— ``infer_source`` 的启发式不能只在 LLM 抽取器的 ``work``
   类别下生效；``classify_fact`` 返回的 ``other`` 也必须能识别设定/进度措辞。

全部用 tmp 库，不碰真实 ``~/.deskmate/deskmate.db``。
"""

from __future__ import annotations

import ast
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from deskmate.memory.learned_store import (
    LearnedStore,
    classify_fact,
    infer_source,
    source_book,
    source_prefix,
)

REPO = Path(__file__).resolve().parents[1]

#: 三个旁路写入点：所有 ``LearnedStore(...).append`` / ``store.append`` 都必须显式传 source
BYPASS_FILES = (
    "deskmate/digest/engine.py",
    "deskmate/auto_tasks/recorder.py",
    "deskmate/memory/ingestion_pipeline.py",
    "coworker/server/app.py",
)


def _append_calls(path: Path) -> list[ast.Call]:
    """收集文件里所有指向 LearnedStore 的 append 调用。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "append":
            continue
        owner = node.func.value
        # LearnedStore(...).append(...) 直接构造
        if isinstance(owner, ast.Call) and isinstance(owner.func, ast.Name):
            if owner.func.id == "LearnedStore":
                out.append(node)
                continue
        # store.append(...) / _learned_store(...).append(...)
        if isinstance(owner, ast.Name) and owner.id in ("store", "_store"):
            out.append(node)
        elif isinstance(owner, ast.Call) and isinstance(owner.func, ast.Name):
            if owner.func.id == "_learned_store":
                out.append(node)
    return out


class InferSourceTest(unittest.TestCase):
    def test_setting_marks_work_with_classify_fact_category(self):
        """classify_fact 返回 other 时也要识别设定措辞（不能只认 LLM 的 work）。"""
        text = "《夜不收》设定：大和盟总旗主是韩孝民"
        self.assertEqual(infer_source(None, classify_fact(text), text), "setting")

    def test_progress_marks_work_with_classify_fact_category(self):
        text = "《夜不收》第十一章改完了"
        self.assertEqual(infer_source(None, classify_fact(text), text), "progress")

    def test_preference_always_stays_live(self):
        text = "用户偏好雨天闷头码字，决定不改成早上写"
        self.assertEqual(infer_source(None, classify_fact(text), text), "live")

    def test_illegal_raw_source_falls_back(self):
        self.assertEqual(infer_source("bogus", "other", "《夜不收》大纲改为三幕"), "setting")


class AppendDisciplineTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = LearnedStore(self.base)

    def tearDown(self):
        self._tmp.cleanup()

    def _rows(self) -> list[tuple[str, str]]:
        with sqlite3.connect(str(self.base / "deskmate.db")) as conn:
            return conn.execute(
                "SELECT fact, source FROM learned_history ORDER BY id"
            ).fetchall()

    def test_no_source_setting_is_not_live(self):
        self.store.append(["《夜不收》设定：大和盟总旗主是韩孝民"])
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(source_prefix(rows[0][1]), "setting")
        self.assertEqual(source_book(rows[0][1]), "夜不收")

    def test_no_source_progress_is_not_live(self):
        self.store.append(["《夜不收》第十一章改完了"])
        rows = self._rows()
        self.assertEqual(source_prefix(rows[0][1]), "progress")

    def test_no_source_preference_stays_live(self):
        self.store.append(["用户偏好雨天闷头码字"])
        self.assertEqual(self._rows()[0][1], "live")

    def test_illegal_source_falls_back_to_inference(self):
        self.store.append(["《夜不收》大纲改为三幕"], source="bogus")
        self.assertEqual(source_prefix(self._rows()[0][1]), "setting")

    def test_explicit_source_not_overridden(self):
        self.store.append(["《夜不收》设定：大和盟总旗主是韩孝民"], source="external")
        self.assertEqual(self._rows()[0][1], "external")

    def test_book_suffix_from_session_text(self):
        self.store.append(
            ["大和盟总旗主设定为韩孝民"],
            source="setting",
            session_text="接着聊《夜不收》第十一章",
        )
        self.assertEqual(self._rows()[0][1], "setting:夜不收")

    def test_book_id_argument_still_wins(self):
        self.store.append(["一条设定"], source="setting", book_id="衙探")
        self.assertEqual(self._rows()[0][1], "setting:衙探")

    def test_mixed_batch_infers_per_fact(self):
        """同一批里不同措辞要分别定来源，不能整批共用一个 live。"""
        self.store.append(
            [
                "用户偏好雨天写作",
                "《夜不收》设定：大和盟总旗主是韩孝民",
                "《夜不收》第十一章改完了",
            ]
        )
        got = {fact: source_prefix(src) for fact, src in self._rows()}
        self.assertEqual(
            got,
            {
                "用户偏好雨天写作": "live",
                "《夜不收》设定：大和盟总旗主是韩孝民": "setting",
                "《夜不收》第十一章改完了": "progress",
            },
        )


class BypassCallSiteTest(unittest.TestCase):
    def test_all_bypass_append_calls_declare_source(self):
        offenders: list[str] = []
        for rel in BYPASS_FILES:
            path = REPO / rel
            self.assertTrue(path.exists(), f"missing {rel}")
            for call in _append_calls(path):
                if not any(kw.arg == "source" for kw in call.keywords):
                    offenders.append(f"{rel}:{call.lineno}")
        self.assertEqual(
            offenders, [],
            "旁路写记忆必须显式传 source（或 source=None 明确交给数据层推断）",
        )


if __name__ == "__main__":
    unittest.main()
