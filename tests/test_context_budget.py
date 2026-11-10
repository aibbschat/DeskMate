"""上下文 token 预算 + 闲聊轮跳重块 回归（2026-11-10）。

为什么有这组测试：
每轮注入实测 ~40KB（≈3 万 token）——USER 画像 12.4KB / cognitive 8KB / world 7.5KB /
prefetch 5.3KB 是 prefill 大头，闲聊一句「早」也照灌。这里锁住三条护栏：
1) 总预算按估算 token 控制，超了先丢明显可选的长块，核心内容（画像/记忆检索/事实/
   情绪/时钟）只截断、不整块丢；
2) 闲聊轮跳过 cognitive/world/prefetch/lessons/reflection/intent 等重块；
3) 预算开关关掉时恢复旧行为（不裁剪），排障可用。
"""
from __future__ import annotations

import pytest

from deskmate.memory import context_budget as cb
from deskmate.memory.manager import DeskMateMemoryManager


# ---------------------------------------------------------------------------
# 基础函数
# ---------------------------------------------------------------------------
def test_estimate_tokens_cjk_heavier_than_ascii():
    assert cb.estimate_tokens("") == 0
    assert cb.estimate_tokens("你好世界") > cb.estimate_tokens("abcd")


@pytest.mark.parametrize(
    "msg",
    ["早", "在吗", "谢谢", "今天怎么这么累", "我累了", "晚安", "你叫什么名字", "哈哈"],
)
def test_light_turn_detected(msg):
    assert cb.is_light_turn(msg)


@pytest.mark.parametrize(
    "msg",
    [
        # 短句也可能需要记忆/前文：不能当闲聊（否则她会丢开记忆瞎答）
        "林岚后来怎样",
        "他最后死了吗",
        "你还记得吗",
        "还记得上次那个吗",
        "之前说的那个设定",
        "帮我改第三章",
        "《夜不收》粮仓方向怎么改",
        "接着写下一节",
        "查一下这个人物",
        "今天想写点东西",
        "这段怎么改",
        "把大纲整理一下",
    ],
)
def test_work_turn_not_light(msg):
    assert not cb.is_light_turn(msg)


def test_light_env_switch(monkeypatch):
    monkeypatch.setenv("DESKMATE_CONTEXT_LIGHT", "0")
    assert not cb.is_light_turn("早")


def test_long_message_not_light_even_without_keywords():
    assert not cb.is_light_turn("我" * 30)


@pytest.mark.parametrize(
    "msg",
    [
        # 2026-11-10 线上误判回归：这些都被判成闲聊，把记忆/课时/意图整块丢了
        "早上好，接着写第二章",  # 此前被单字「早」短路
        "麻烦把第四章大纲整理一下",  # 此前被单字「烦」短路
        "困了，明天再写",  # 此前被单字「困」短路
        "我早说过这段要删",  # 此前被单字「早」短路
        "按这个思路生成一份周报",  # 短任务指令
        "跑一遍全部测试",
        "执行",
        "正史",  # 短主题词：此前被默认判成闲聊，prefetch 整块被丢（trace 测试挂掉）
        "好了没",  # 生产 trace 实录：问任务进度
        "进度到哪了",
        "我们接着聊",  # 需要前文/反思才接得上
    ],
)
def test_short_task_commands_are_not_chitchat(msg):
    assert not cb.is_light_turn(msg)


@pytest.mark.parametrize("msg", ["你吃饭了吗", "早！", "我累了", "怎么这么累"])
def test_social_short_messages_still_light(msg):
    assert cb.is_light_turn(msg)


# ---------------------------------------------------------------------------
# 渲染：上限 / 预算 / 闲聊
# ---------------------------------------------------------------------------
def test_render_caps_blocks(monkeypatch):
    monkeypatch.setenv("DESKMATE_CONTEXT_BUDGET", "1")
    blocks = [cb.ContextBlock("a", "x" * 5000, priority=10, cap=100)]
    text, stats = cb.render_blocks(blocks, user_message="写第三章", budget_tokens=100000)
    assert len(text) <= 100 + len(cb._ELLIPSIS)
    assert "a:cap" in stats["trimmed"]


def test_render_drops_optional_blocks_but_keeps_core(monkeypatch):
    monkeypatch.setenv("DESKMATE_CONTEXT_BUDGET", "1")
    blocks = [
        cb.ContextBlock("clock", "现在 10:00", priority=0, cap=200),
        cb.ContextBlock("user_profile", "画像" * 800, priority=40, cap=6000),
        cb.ContextBlock("prefetch", "记忆" * 800, priority=35, cap=4500),
        # 功能块（分身开着才有）：只截断不整块丢
        cb.ContextBlock("cognitive", "认知" * 1200, priority=42, cap=2500, keep_light=False),
        cb.ContextBlock("world", "世界" * 1200, priority=48, cap=4000, keep_light=False),
        # 明显可选：先丢
        cb.ContextBlock("mailbox", "信箱" * 300, priority=55, cap=900, keep_light=False),
        cb.ContextBlock("lessons", "课时" * 300, priority=60, cap=1500, keep_light=False),
    ]
    text, stats = cb.render_blocks(blocks, user_message="写第三章", budget_tokens=900, light=False)
    assert "mailbox" in stats["dropped"] and "lessons" in stats["dropped"]
    assert "cognitive" not in stats["dropped"] and "world" not in stats["dropped"]
    assert "现在 10:00" in text
    assert "画像" in text, "核心画像只能截断、不能整块丢"
    assert cb.estimate_tokens(text) <= 1100


def test_render_light_skips_heavy_blocks(monkeypatch):
    monkeypatch.setenv("DESKMATE_CONTEXT_BUDGET", "1")
    blocks = [
        cb.ContextBlock("clock", "现在 10:00", priority=0, cap=200),
        cb.ContextBlock("prefetch", "记忆", priority=35, cap=3500, keep_light=False),
        cb.ContextBlock("cognitive", "认知", priority=50, cap=2500, keep_light=False),
        cb.ContextBlock("user_fact", "你偏好短句", priority=30, cap=2000, cap_light=900),
        cb.ContextBlock("user_profile", "画像", priority=40, cap=4000, cap_light=1200),
    ]
    text, stats = cb.render_blocks(blocks, user_message="早", budget_tokens=100000)
    assert stats["light"] is True
    assert "prefetch" in stats["dropped"] and "cognitive" in stats["dropped"]
    assert "你偏好短句" in text and "画像" in text


def test_render_budget_off_returns_everything(monkeypatch):
    monkeypatch.setenv("DESKMATE_CONTEXT_BUDGET", "0")
    blocks = [
        cb.ContextBlock("a", "甲" * 3000, priority=90, cap=100),
        cb.ContextBlock("b", "乙" * 3000, priority=99, cap=100),
    ]
    text, stats = cb.render_blocks(blocks, user_message="早", budget_tokens=10)
    assert stats["budget_enabled"] is False
    assert len(text) > 5000


# ---------------------------------------------------------------------------
# 集成：真实 manager 链路（tmp 目录 + 合成重块，不碰任何真实数据）
# ---------------------------------------------------------------------------
@pytest.fixture()
def heavy_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_WEATHER", "0")
    monkeypatch.setenv("DESKMATE_EMOTION_ENGINE", "0")
    monkeypatch.setattr(
        "deskmate.memory.manager.load_user", lambda base: "画像" * 4000
    )
    monkeypatch.setattr(
        DeskMateMemoryManager,
        "prefetch",
        # P1-B（WorkBuddy）给 prefetch 加了 book 关键字参数；测试替身要兼容
        lambda self, q, **_kw: "<memory-context>" + "记忆内容" * 4000 + "</memory-context>",
    )
    return DeskMateMemoryManager(base_dir=tmp_path)


def test_manager_context_respects_token_budget(heavy_manager, monkeypatch):
    monkeypatch.setenv("DESKMATE_CONTEXT_BUDGET", "1")
    monkeypatch.setenv("DESKMATE_CONTEXT_TOKEN_BUDGET", "1200")
    monkeypatch.setenv("DESKMATE_CONTEXT_LIGHT", "0")
    ctx = heavy_manager.build_full_context_for_turn("帮我改第三章")
    assert "画像" in ctx
    assert cb.estimate_tokens(ctx) <= 1400, f"预算失效：{cb.estimate_tokens(ctx)} token"


def test_manager_context_light_turn_skips_prefetch(heavy_manager, monkeypatch):
    monkeypatch.setenv("DESKMATE_CONTEXT_BUDGET", "1")
    monkeypatch.setenv("DESKMATE_CONTEXT_TOKEN_BUDGET", "100000")
    work_ctx = heavy_manager.build_full_context_for_turn("帮我改第三章")
    light_ctx = heavy_manager.build_full_context_for_turn("早")
    assert "记忆内容" in work_ctx
    assert "记忆内容" not in light_ctx, "闲聊轮不该注入记忆检索重块"
    assert "画像" in light_ctx
    assert len(light_ctx) < len(work_ctx)

def test_strip_auto_review_keeps_stable_profile():
    profile = (
        "# 用户画像\n## 基本信息\n- 萧盛，历史小说家\n"
        "## 工作模式与AI协作\n- 不喜欢被追问\n"
        "## 自动复盘 · 2026-11-10\n- 今天聊了第三章\n"
        "## 自动复盘 · 2026-11-09\n- 昨天聊了第二章\n"
    )
    out = cb.strip_auto_review(profile)
    assert "基本信息" in out and "工作模式" in out
    assert "自动复盘" not in out
    assert "今天聊了第三章" not in out
