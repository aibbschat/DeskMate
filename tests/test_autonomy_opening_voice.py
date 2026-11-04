"""目标制 opening 的表达层契约：只给真实状态与判定条件，不给表达模板。

2026-11-04 20:10 变化轮真机报告（``run-07f06ae720``）原文：

    读到了——文件已经变了，按约定向您报告。
    **盯梢验收·flag：已变 READY** ✅
    - **变化点**：…
    - **文件当前原文**：…
    - **我的判断**：…

根因不在人格：自主任务链路（``_build_task_engine``）不注入
``XIAOJU_SYSTEM``，喂出三段式的是 ``build_goal_opening`` 里
「给出变化点、原文链接和你的一句判断」+ 四条 ``·`` 清单——正对应
2026-10-20~10-21 查明的「bullet 思维外化 + 结构化要求逼正式腔」。

11-05 这一铲把表达方式从 opening 撤掉：**协议标记、判定条件、送达语义
一个不少；表达模板一条不许回来**。撤的是系统给的模板，不是她的表达自由。
"""

from deskmate.autonomy.goal import (
    NO_CHANGE,
    STATE_MARK,
    STUCK_MARK,
    build_goal_opening,
)

# 会把模型输出逼成「报告体」的结构化要求（旧 opening 原串）。
_BANNED_TEMPLATES = [
    "给出变化点",
    "原文链接",
    "你的一句判断",
    "本轮判断：",
    "· 目标状态",
    "不要写报告、不要总结",
]

# 协议与功能红线，去掉模板时一个都不能丢。
_REQUIRED_PROTOCOL = [
    NO_CHANGE,
    STATE_MARK,
    STUCK_MARK,
    "sleep_for",
    "不要创建或修改任何定时任务",
    "上轮状态：",
    "上轮检查时间：",
    "不超过 200 字",
]


class TestOpeningVoice:
    def test_opening_不含表达模板与bullet清单(self):
        for prev in ({}, {"state": "hash=abc", "last_checked": 1730000000.0},
                     {"attempts": [{"at": 1730000000.0, "outcome": "error",
                                    "steps": ["run_shell(cat flag.txt)"]}],
                      "stuck_rounds": 2, "flat_rounds": 4}):
            text = build_goal_opening(prev)
            leaked = [t for t in _BANNED_TEMPLATES if t in text]
            assert not leaked, f"表达模板回到 opening：{leaked}"

    def test_协议标记与判定条件一个不少(self):
        text = build_goal_opening({"state": "hash=abc", "last_checked": 1730000000.0})
        missing = [t for t in _REQUIRED_PROTOCOL if t not in text]
        assert not missing, f"协议/功能被误删：{missing}"
        assert "hash=abc" in text, "跨轮状态仍须进 opening"

    def test_变化轮仍说明会送达并结案(self):
        """撤的是「怎么写」，不是「这轮会不会送到、结不结案」。"""
        text = build_goal_opening({})
        assert "送达" in text and "结案" in text
        assert "不再复查" in text

    def test_静默轮仍是状态说明而非格式禁令(self):
        text = build_goal_opening({})
        assert "结果不送达" in text, "静默轮的送达语义要讲清"
        assert "下次计划时间继续" in text

    def test_去bullet不误伤跨轮尝试记录(self):
        """_attempts_block 的历史清单是数据，不是表达模板，要留着。"""
        prev = {
            "state": "hash=abc 仍未变",
            "last_checked": 1730000000.0,
            "attempts": [{"at": 1730000000.0, "outcome": "error",
                          "steps": ["run_shell(cat /tmp/x/flag.txt)"],
                          "error": "No such file or directory"}],
            "stuck_rounds": 2,
            "flat_rounds": 4,
        }
        text = build_goal_opening(prev)
        assert "run_shell" in text and "No such file" in text
        assert "不要再原样重复" in text
