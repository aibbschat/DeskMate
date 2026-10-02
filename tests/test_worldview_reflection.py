"""世界观回望（worldview_reflection）的测试。

钉住四件事：
1. **半自动**：攒够 min_observations 才回望；冷却未过不回望；已消化不重复。
2. **她自己说**：解析她的自由输出；弃权是正当结果（不硬逼）。
3. **落点对**：写进 worldview 层（不是 cognitive 层），且与「她眼中的他」层隔离。
4. **fail-open**：provider 缺失 / LLM 异常 → 不抛、不写、不刷屏。

另外钉住注入通道：沉进去必须**用得上**，且主语是「你」不是「他」。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from deskmate.agency.behaviors import worldview_reflection as wr


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _seed_understanding(base: str, n: int = 5, prefix: str = "理解") -> None:
    """造 n 条「她看世界后的理解」（观察日记真实落盘格式）。"""
    path = Path(base) / "agency" / "observation_understanding.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            rec = {
                "date": "2026-09-07",
                "ts": time.time() + i,
                "entry_seq": i + 1,
                "fact_keys": [f"k{i}"],
                "body": f"{prefix}{i}：这件事让我想到一些更大的东西，值得记下来。",
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


FAKE_OUT = """看了这些事，我越来越觉得边界这东西从来都是临时的。

我相信：边界从来都是临时的，谁有力量谁划线。
关于：国际冲突
我看重：看清谁在争 > 事实准确 > 立场鲜明
我习惯这么看：先问谁在争 → 再看争什么 → 最后才判断"""


def _patch_llm(monkeypatch, out=FAKE_OUT, available=True):
    monkeypatch.setattr(
        "deskmate.memory.llm.memory_llm_available", lambda: available
    )
    monkeypatch.setattr("deskmate.memory.llm.chat_memory", lambda *a, **k: out)


# ----------------------------------------------------------------------
# 1) 解析
# ----------------------------------------------------------------------
def test_parse_reflection_normal():
    p = wr.parse_reflection(FAKE_OUT)
    assert p["bailed"] is False
    assert p["believe"] == "边界从来都是临时的，谁有力量谁划线。"
    assert p["about"] == "国际冲突"
    assert p["values"] == ["看清谁在争", "事实准确", "立场鲜明"]
    assert p["steps"] == ["先问谁在争", "再看争什么", "最后才判断"]
    assert "边界这东西从来都是临时的" in p["essay"]
    # 正文不该残留小结行
    assert "我相信：" not in p["essay"]


def test_parse_reflection_bail_is_legitimate():
    """她说「我还说不好」→ 弃权，是正当结果，不是失败。"""
    p = wr.parse_reflection("看了这些，我还说不好。再多看看吧。")
    assert p["bailed"] is True
    assert p["believe"] == ""


def test_parse_reflection_empty_bails():
    p = wr.parse_reflection("")
    assert p["bailed"] is True


def test_parse_reflection_free_text_without_summary_bails():
    """说了话但没按格式小结 → 不硬造观点，尊重为弃权。"""
    p = wr.parse_reflection("这些事挺杂的，我一时理不出头绪。")
    assert p["bailed"] is True


def test_parse_reflection_tolerates_halfwidth_colon():
    p = wr.parse_reflection("我相信:测试内容\n关于:测试域")
    assert p["believe"] == "测试内容"
    assert p["about"] == "测试域"


# ----------------------------------------------------------------------
# 2) 半自动闸门
# ----------------------------------------------------------------------
def test_not_enough_observations_does_not_reflect(tmp_path):
    base = str(tmp_path)
    _seed_understanding(base, n=3)  # 少于默认 5 条
    res = wr.run_worldview_reflection(base, {"cooldown_hours": 0})
    assert res["reflected"] is False
    assert res["reason"].startswith("not_enough:3/5")


def test_no_understanding_at_all(tmp_path):
    res = wr.run_worldview_reflection(str(tmp_path), {"cooldown_hours": 0})
    assert res["reflected"] is False
    assert res["reason"] == "no_understanding"


def test_cooldown_blocks_second_reflection(tmp_path, monkeypatch):
    base = str(tmp_path)
    _seed_understanding(base, n=5)
    _patch_llm(monkeypatch)

    first = wr.run_worldview_reflection(base, {"cooldown_hours": 168})
    assert first["reflected"] is True

    # 补 5 条新的，但冷却未过 → 不回望
    _seed_understanding(base, n=10)
    second = wr.run_worldview_reflection(base, {"cooldown_hours": 168})
    assert second["reflected"] is False
    assert second["reason"] == "cooling"


def test_rec_key_for_ts_record_is_stable_across_processes():
    """无 date/seq 的自动摄入记录也必须有跨进程稳定 key。

    旧实现使用 Python 内置 hash()，进程重启后变化，真实 reflected 状态因此
    无法压住同一批理解，会让 worldview 反复在冷却后被旧素材触发。
    """
    rec = {"ts": 1790903578.0, "body": "这是她自己想明白的一件事。"}
    assert wr._rec_key(rec) == wr._rec_key(dict(rec))
    assert wr._rec_key(rec).startswith("2026-10-02#")


def test_automation_noise_does_not_count_toward_reflection(tmp_path):
    """检索转储不是她的理解，不能凑够回望素材。"""
    base = str(tmp_path)
    path = Path(base) / "agency" / "observation_understanding.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"ts": time.time(), "body": "检索到以下资料：\n- 自动搜索结果一"},
        {"ts": time.time() + 1, "body": "⏰ Scheduled run\nThis automation is due now"},
        {"ts": time.time() + 2, "body": "真正的理解：我开始明白工具和表达之间的边界。"},
    ]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")

    res = wr.run_worldview_reflection(base, {"cooldown_hours": 0})
    assert res["reflected"] is False
    assert res["reason"] == "not_enough:1/5"


def test_already_digested_not_repeated(tmp_path, monkeypatch):
    """回望过的那批不再被问第二次（免得反复拿同一批事去烦她）。"""
    base = str(tmp_path)
    _seed_understanding(base, n=5)
    _patch_llm(monkeypatch)

    first = wr.run_worldview_reflection(base, {"cooldown_hours": 0})
    assert first["reflected"] is True

    # 没有新增理解，冷却也过了 → 素材不够（都消化过了）
    second = wr.run_worldview_reflection(base, {"cooldown_hours": 0})
    assert second["reflected"] is False
    assert second["reason"].startswith("not_enough:0/5")


# ----------------------------------------------------------------------
# 3) 落点：worldview 层，且与他层隔离
# ----------------------------------------------------------------------
def test_reflection_writes_worldview_layer(tmp_path, monkeypatch):
    base = str(tmp_path)
    _seed_understanding(base, n=5)
    _patch_llm(monkeypatch)

    res = wr.run_worldview_reflection(base, {"cooldown_hours": 0})
    assert res["reflected"] is True
    assert res["written"] == 3, res["items"]  # stance + value + framework
    assert res["bailed"] is False
    assert "国际冲突" in (res["summary"] or "")

    from deskmate.cognitive.profile import CognitiveProfileManager

    db = os.path.join(base, "deskmate.db")
    wv = CognitiveProfileManager(profile_path=db, layer="worldview").get()
    assert len(wv.stances) == 1
    assert len(wv.value_rankings) == 1
    assert len(wv.thinking_frameworks) == 1
    assert wv.stances[0].topic == "国际冲突"
    assert wv.stances[0].sources[0].type == "observation"

    # 关键：不能污染「她眼中的他」那两层
    his = CognitiveProfileManager(profile_path=db, layer="cognitive").get()
    assert len(his.stances) == 0


def test_bail_writes_nothing_but_marks_digested(tmp_path, monkeypatch):
    """她弃权 → 不写条目，但这批照样标记已消化（不再反复问）。"""
    base = str(tmp_path)
    _seed_understanding(base, n=5)
    _patch_llm(monkeypatch, out="看了这些，我还说不好。")

    res = wr.run_worldview_reflection(base, {"cooldown_hours": 0})
    assert res["reflected"] is True
    assert res["bailed"] is True
    assert res["written"] == 0

    # 再跑一次：素材已消化 → 不再问
    again = wr.run_worldview_reflection(base, {"cooldown_hours": 0})
    assert again["reflected"] is False
    assert again["reason"].startswith("not_enough:0/5")


# ----------------------------------------------------------------------
# 4) fail-open
# ----------------------------------------------------------------------
def test_no_provider_returns_gracefully(tmp_path, monkeypatch):
    base = str(tmp_path)
    _seed_understanding(base, n=5)
    _patch_llm(monkeypatch, available=False)

    res = wr.run_worldview_reflection(base, {"cooldown_hours": 0})
    assert res["reflected"] is False
    assert res["reason"] == "no_provider"


def test_llm_exception_does_not_raise(tmp_path, monkeypatch):
    base = str(tmp_path)
    _seed_understanding(base, n=5)
    monkeypatch.setattr("deskmate.memory.llm.memory_llm_available", lambda: True)

    def _boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr("deskmate.memory.llm.chat_memory", _boom)

    res = wr.run_worldview_reflection(base, {"cooldown_hours": 0})
    assert res["reflected"] is False
    assert res["reason"] == "llm_error"


# ----------------------------------------------------------------------
# 5) 注入通道：沉进去 ≠ 用得上，这段钉住「用得上」且主语正确
# ----------------------------------------------------------------------
def test_worldview_is_injected_with_correct_subject(tmp_path, monkeypatch):
    """回归：worldview 层以前根本没被注入（load_profile 写死单层）。

    现在必须：① 注入得到；② 主语是「你」（她的看法），不能说成「他」。
    """
    base = str(tmp_path)
    _seed_understanding(base, n=5)
    _patch_llm(monkeypatch)
    assert wr.run_worldview_reflection(base, {"cooldown_hours": 0})["reflected"] is True

    monkeypatch.setenv("DESKMATE_DIR", base)
    import deskmate.cognitive.memory_injector as mi

    ctx = mi.build_cognitive_context()
    assert "你自己的看法" in ctx, ctx[:400]
    assert "你的立场" in ctx, ctx[:400]
    assert "不是萧盛的观点" in ctx
    # 主语必须是「你」——说成「他的立场」就是把她的观点当成他的（语义污染）
    assert "他的立场" not in ctx
    assert "边界从来都是临时的" in ctx
    assert mi.has_cognitive_data() is True


def test_injection_reads_deskmatdir_not_real_home(tmp_path, monkeypatch):
    """2026-09-07 修的 bug：注入端以前不认 DESKMATE_DIR，测试会读到真实库。"""
    base = str(tmp_path)
    monkeypatch.setenv("DESKMATE_DIR", base)  # 空目录，真实库里却有 40+ 条
    import deskmate.cognitive.memory_injector as mi

    # 空目录下不该凭空冒出真实库的认知条目
    ctx = mi.build_cognitive_context()
    assert "查资料时只用权威正史记载" not in ctx
    assert mi.has_cognitive_data() is False


# ----------------------------------------------------------------------
# 6) 注册与配置
# ----------------------------------------------------------------------
def test_registered_as_low_risk_action():
    from deskmate.agency.registry import get_spec

    spec = get_spec("worldview_reflection")
    assert spec is not None
    assert spec.risk == "low"


def test_settings_default_includes_worldview_reflection():
    from deskmate.agency.settings import DEFAULTS, AgencySettings

    assert "worldview_reflection" in DEFAULTS["whitelist"]["actions"]
    s = AgencySettings()
    assert s.worldview_reflection["min_observations"] == 5
    assert s.worldview_reflection["cooldown_hours"] == 168
    # 用户配置按 key 覆盖，未知 key 不生效
    s2 = AgencySettings.from_dict(
        {"worldview_reflection": {"min_observations": 8, "bogus": 1}}
    )
    assert s2.worldview_reflection["min_observations"] == 8
    assert "bogus" not in s2.worldview_reflection
