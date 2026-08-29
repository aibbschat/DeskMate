"""pytest 回归门禁：记忆质量评测集。

每次改动记忆逻辑（去重 / 分层 / 提炼等）跑 `python -m pytest tests/memory_bench/`
即可确认「越用越懂你」没有退化。召回率低于阈值则测试失败。
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from fixture_xiaosheng import build_xiaosheng_fixture  # noqa: E402
from runner import RECALL_THRESHOLD, evaluate  # noqa: E402


def test_memory_benchmark_recall_rate():
    d = Path(tempfile.mkdtemp(prefix="membench_"))
    build_xiaosheng_fixture(d)
    rep = evaluate(d)
    # 打印完整报告，便于 CI / 本地排查
    print("\n" + rep["text"])
    assert rep["total"] >= 40, "题库题量不足（应 ≥ 40）"
    assert rep["recall_rate"] >= RECALL_THRESHOLD, (
        f"记忆召回率 {rep['recall_rate']:.1%} 低于阈值 {RECALL_THRESHOLD:.0%}，"
        f"疑似记忆检索回归"
    )
