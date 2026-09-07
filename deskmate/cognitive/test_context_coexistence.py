"""contexts 维度 + 矛盾分级（并存 vs 真矛盾）+ 注入标注。

覆盖：
- contexts 字段持久化往返（零 DDL，旧数据默认空，不报错）
- 矛盾分级：contexts 不相交 → coexist（不置疑、不堆 gap）；
            默认空 / 相交 → contradiction（置疑 + 生成 gap，保持旧行为）
- 注入：_fmt_* 带「适用」标注；select_for_context 按情境裁剪
"""
import sys
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from deskmate.cognitive.profile import CognitiveProfileManager
from deskmate.cognitive.models import (
    Stance,
    ArgumentPattern,
    ValueRanking,
    ThinkingFramework,
    CognitiveProfile,
    SourceRef,
)
from deskmate.cognitive.memory_injector import (
    _fmt_stance,
    _fmt_value,
    _fmt_argument,
    _fmt_thinking,
    CognitiveMemoryInjector,
)


def _pm():
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"
    return CognitiveProfileManager(db), tmp


def test_contexts_persist_roundtrip():
    pm, tmp = _pm()
    s, op = pm.add_stance(
        "历史观", "大事不虚，小事不拘",
        SourceRef(type="vault", title="创作谈"),
        contexts=["创作圈"],
    )
    assert op == "created"
    assert s.contexts == ["创作圈"]
    pm2 = CognitiveProfileManager(tmp / "deskmate.db")
    got = pm2.get().stances[0]
    assert got.contexts == ["创作圈"], got.contexts
    # 旧数据（无 contexts）加载默认空，不报错
    assert got.status == "active"
    shutil.rmtree(tmp)
    print("OK test_contexts_persist_roundtrip")


def test_contexts_coexist_not_questioned():
    """情境不相交 → 合法并存：不置疑、不堆 gap（容纳矛盾）。"""
    pm, tmp = _pm()
    pm.add_stance("待人", "护短 朋友 讲义气", SourceRef(type="interview", title="访谈"),
                  contexts=["创作圈"])
    s2, op = pm.add_stance("待人", "不护短 朋友 讲义气 对外冷漠", SourceRef(type="interview", title="访谈"),
                           contexts=["学术圈"])
    assert op == "coexist", op
    assert s2.status == "active"
    assert len(pm.get().cognitive_gaps) == 0, pm.get().cognitive_gaps
    shutil.rmtree(tmp)
    print("OK test_contexts_coexist_not_questioned")


def test_default_contexts_still_contradiction():
    """默认空 contexts = 全局适用 = 相交 → 保持旧行为：置疑 + 生成 gap。"""
    pm, tmp = _pm()
    pm.add_stance("待人", "护短 朋友 讲义气", SourceRef(type="interview", title="访谈"))
    s2, op = pm.add_stance("待人", "不护短 朋友 讲义气 对外冷漠", SourceRef(type="interview", title="访谈"))
    assert op == "contradiction", op
    assert s2.status == "questioned"
    assert len(pm.get().cognitive_gaps) == 1
    shutil.rmtree(tmp)
    print("OK test_default_contexts_still_contradiction")


def test_injection_labels_contexts():
    assert "适用：创作圈" in _fmt_stance(Stance(content="护短 朋友", contexts=["创作圈"]))
    assert "适用：学术圈" in _fmt_argument(ArgumentPattern(content="用类比讲史", contexts=["学术圈"]))
    assert "适用：学术圈" in _fmt_thinking(
        ThinkingFramework(domain="历史", content="先制度后人物", contexts=["学术圈"]))
    assert "适用：创作圈" in _fmt_value(
        ValueRanking(domain="创作", ranking=["真实", "好看"], contexts=["创作圈"]))
    # 无 contexts 不标「适用」
    assert "适用" not in _fmt_stance(Stance(content="护短 朋友"))
    print("OK test_injection_labels_contexts")


def test_select_for_context_filters():
    profile = CognitiveProfile(
        stances=[
            Stance(content="护短 朋友", contexts=["创作圈"]),
            Stance(content="对外人严格"),  # 全局适用（无 contexts）
            Stance(content="学术上较真", contexts=["学术圈"]),
        ],
    )
    inj = CognitiveMemoryInjector()
    out = inj.select_for_context(profile, "创作圈", limit=20)
    text = out[0]["text"]
    assert "护短 朋友" in text
    assert "对外人严格" in text  # 全局适用保留
    assert "学术上较真" not in text  # 学术圈情境被裁掉
    # 不传 context → 全量
    out_all = inj.select_for_context(profile, "", limit=20)
    assert "学术上较真" in out_all[0]["text"]
    print("OK test_select_for_context_filters")
