"""LLM 输出卫生：推理标签不得落进日记（`</think>` 泄漏回归）。

背景（2026-09-21 实测）
----------------------
``~/.deskmate/diary/2026-09-21.md`` 的 ``10:32`` 则，正文前混进了 700 余字的
"预述"（把同一件事先讲了一遍）并以**孤立的** ``</think>`` 收尾——上游只吐了
闭合标签，开标签被网关吃掉。全库仅此 1 处，属偶发，但日记是他的私密记述，
混进模型的草稿纸是质量事故。

本文件锁死三条不变量：
1. 推理残留（成对块 / 孤立闭合 / 孤立开始 / 特殊 token）必须被剥掉；
2. 无残留时**逐字节原样返回**（正常路径不许被无谓改写）；
3. 安全阀：剥离后若所剩无几，说明判断错了，退回"只删标签、不删内容"。
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from deskmate.growth.diary import DiaryWriter, _strip_reasoning
from deskmate.growth.diary_store import _clean_body
from deskmate.text_clean import has_reasoning_artifacts, strip_reasoning_artifacts

# 真实现场：预述 + 孤立闭合标签 + 正文（长度取自 2026-09-21.md）
_REAL_CASE = (
    "上午九点四十，我落定了昨天的定稿，写了网文信息差设计的四种模式。"
    "又顺手定了明天报文的类型——按轮换规则，明天该是评论型了。"
    "</think>上午十点半，你纠正了我一次发布流程。你要我在执行发布前先用 "
    "run_shell 去读配置，拿 token 和 URL，而且 token 不许打到输出里。"
)
# 安全阀（剩余正文短于 20 字符 → 判定为误判，回退"只删标签"）要求
# 测试里的"正文"要足够长，否则测的是回退分支而不是剥离分支。
_BODY = "真正的正文从这里开始，这一段故意写得足够长，长到不会被安全阀回退。"


class TestStripReasoning:
    def test_paired_block_removed(self):
        out, changed = strip_reasoning_artifacts("<think>想想</think>正文在这里，够长了。")
        assert changed and out == "正文在这里，够长了。"
        assert not has_reasoning_artifacts(out)

    def test_stray_close_drops_the_prelude(self):
        """孤立 ``</think>``：它之前的都是推理（真实现场的形态）。"""
        body = "正文这段要留下，而且长度足够长不会被安全阀回退。"
        out, changed = strip_reasoning_artifacts("预述" * 40 + "</think>" + body)
        assert changed and out == body

    def test_stray_open_drops_the_tail(self):
        out, _ = strip_reasoning_artifacts("正文在前，写得够长了<think>后面都是草稿")
        assert out == "正文在前，写得够长了"

    def test_special_tokens_removed(self):
        out, _ = strip_reasoning_artifacts("<|im_start|>正文<|im_end|>")
        assert out == "正文"

    def test_clean_text_returned_byte_identical(self):
        """无残留 → 原样返回（不是"清洗后看起来一样"，是**同一个字符串**）。"""
        s = "今天没什么动静。\n\n我安静地待着。"
        out, changed = strip_reasoning_artifacts(s)
        assert changed is False and out is s

    def test_safety_valve_keeps_content_when_tail_too_short(self):
        """剥完几乎不剩东西 = 判断错了 → 只删标签，不删内容。"""
        prelude = "很长的内容" * 60
        out, _ = strip_reasoning_artifacts(prelude + "</think>短")
        assert "很长的内容" in out and "短" in out and "</think>" not in out

    def test_real_site_case(self):
        out, changed = strip_reasoning_artifacts(_REAL_CASE)
        assert changed
        assert "上午九点四十" not in out      # 预述被剥掉
        assert "上午十点半，你纠正了我一次发布流程" in out
        assert "</think>" not in out

    def test_non_string_input_is_safe(self):
        assert strip_reasoning_artifacts("") == ("", False)
        assert strip_reasoning_artifacts(None) == ("", False)


class TestDiaryWritePath:
    """写入侧的接线：字段级剥离必须真的接上（不然清洗函数只是个摆设）。"""

    def test_narrate_output_is_stripped(self):
        writer = DiaryWriter.__new__(DiaryWriter)  # 只测静态方法，不建库
        with mock.patch(
            "deskmate.growth.diary_narrator.narrate",
            return_value="草稿" * 60 + "</think>" + _BODY,
        ):
            got = DiaryWriter._narrate([{"kind": "chat"}], seq=1, at="10:32")
        assert got == _BODY

    def test_strip_reasoning_helper_tolerates_none(self):
        assert _strip_reasoning(None) == ""
        assert _strip_reasoning(123) == "123"

    def test_append_observation_natural_strips(self, tmp_path: Path):
        w = DiaryWriter(tmp_path)
        r = w.append_observation_natural(
            "2026-09-21", "预述" * 60 + "</think>" + _BODY, at="10:32",
        )
        assert r["written"] == 1
        text = Path(r["path"]).read_text(encoding="utf-8")
        assert "预述" not in text
        assert _BODY in text

    def test_append_observation_bullets_stripped(self, tmp_path: Path):
        w = DiaryWriter(tmp_path)
        r = w.append_observation(
            "2026-09-21",
            ["预述" * 60 + "</think>" + _BODY, "另一条正常观察"],
            at="11:00",
        )
        assert r["written"] == 2
        text = Path(r["path"]).read_text(encoding="utf-8")
        assert "预述" not in text
        assert _BODY in text and "另一条正常观察" in text

    def test_clean_body_strips_reasoning(self):
        """SQLite 日记路径（用户输入框 / 前端提交）同样要过一遍。"""
        out = _clean_body("预述" * 60 + "</think>" + _BODY + "\n\n第二段")
        assert "预述" not in out
        assert out.startswith(_BODY)
        assert "第二段" in out

    def test_clean_body_keeps_normal_paragraphs(self):
        """回归：剥标签不能顺手把「保留段落换行」的既有契约弄坏。"""
        out = _clean_body("第一段\n\n第二段\n\n\n第三段")
        assert out == "第一段\n\n第二段\n\n第三段"
