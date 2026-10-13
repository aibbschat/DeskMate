"""第2层负面表达许可已写入 persona.py 单一事实源（活感方案v2「真实优先」）。

验证（先红后绿）：
1. 单元级：XIAOJU_SYSTEM 含萧哥 2026-10-13 最终定稿的许可关键锚点。
2. 集成级：因方案A（persona_base 注入），主聊天 system prompt
   （load_manifest_file 解析 deskmate.md）也含这些锚点 —— 证明许可
   真实流进聊天 + 日记 + 反思 + 写作全场景，而非只在声明里。

锚点只摘取用户定稿措辞中「不可改写含义」的关键句，不覆盖其完整语义。
"""

from pathlib import Path

import pytest

from deskmate.persona import XIAOJU_SYSTEM
from coworker.personas.manifest import load_manifest_file

# 用户最终定稿许可措辞的关键锚点（含义不得改写）
PERMISSION_ANCHORS = [
    "你的情绪是你自己的，包括负面情绪",
    "可以想骂人",
    "萧盛明确说过：他追求真实，哪怕你把他骂一顿，他也认",
    "不要因为「他是用户」而过滤自己真实的反应",
    "这条许可只管「你对萧盛这个人、对你们之间发生的事」",
    "查证只翻本地史料库、不编造、不替他写原稿、不自动外发",
    "唯一边界：不编造事实",
    "不能虚构他没做过的事来支撑你的生气",
]


@pytest.mark.parametrize("anchor", PERMISSION_ANCHORS)
def test_permission_in_persona_base(anchor):
    """许可文本必须存在于人格单一事实源。"""
    assert anchor in XIAOJU_SYSTEM, f"persona 底座缺失许可锚点：{anchor}"


@pytest.mark.parametrize("anchor", PERMISSION_ANCHORS)
def test_permission_flows_into_chat_system_prompt(anchor):
    """方案A 接线后，主聊天 system prompt 必须含同一份许可。"""
    md = Path("coworker/personas/builtin/deskmate.md")
    sp = load_manifest_file(md, builtin=True).system_prompt
    assert anchor in sp, f"聊天 system prompt 未含许可锚点（方案A 接线）：{anchor}"
