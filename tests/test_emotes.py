"""Tests for P1-4 表情系统（coworker/emotes.py）。

P0 止血三板斧：① 系统侧语义标注 ② 结构化词表注入 ③ few-shot 示例。
前端 Manifest 化已完成（emotes.manifest.json + SVG 渲染），本测试覆盖后端语义层。
"""

from __future__ import annotations

import pytest

from coworker import emotes


class TestManifestLoading:
    def test_manifest_has_16_emotes(self):
        assert len(emotes.EMOTES) == 16

    def test_core_emotes_present(self):
        for eid in ["wave", "cheer", "idea", "shy", "v", "sleepy", "tea",
                     "hug", "stuck", "facepalm", "peek", "clap", "check",
                     "shock", "heart", "inbox"]:
            assert eid in emotes.EMOTES, f"missing emote: {eid}"

    def test_each_emote_has_semantic_fields(self):
        for eid, e in emotes.EMOTES.items():
            assert e["name_zh"], f"{eid} missing name_zh"
            assert e["meaning"], f"{eid} missing meaning"
            assert e["category"], f"{eid} missing category"
            assert e["emotion"], f"{eid} missing emotion"

    def test_inherit_pack_expanded(self):
        """moling-mono 继承 moling-default，语义应已展开。"""
        packs = emotes.list_packs()
        mono = [p for p in packs if p["id"] == "moling-mono"]
        assert mono and mono[0]["inherit"] == "moling-default"


class TestSemanticAnnotation:
    """P0-1：用户消息里的 emote 标记展开为语义说明。"""

    def test_markdown_image_syntax(self):
        text = "我写不出来了 ![卡文抓狂](emote:stuck)"
        result = emotes.expand_emotes_in_content(text)
        assert "[用户表情：卡文抓狂 —— 含义：" in result
        assert "卡文" in result
        assert "emote:stuck" not in result  # 原始标记被替换

    def test_bracket_syntax(self):
        text = "嗨 [[emote:wave]]"
        result = emotes.expand_emotes_in_content(text)
        assert "[用户表情：招手问好" in result
        assert "[[emote:wave]]" not in result

    def test_with_pack_name(self):
        """emote:PACK:NAME 带包名写法。"""
        text = "休息一下 ![摸鱼喝茶](emote:moling-mono:tea)"
        result = emotes.expand_emotes_in_content(text)
        assert "[用户表情：摸鱼喝茶" in result
        assert "emote:" not in result

    def test_unknown_emote_falls_back_to_alt(self):
        text = "![奇怪表情](emote:unknown_face)"
        result = emotes.expand_emotes_in_content(text)
        assert "[用户表情：奇怪表情]" in result

    def test_unknown_emote_no_alt_removed(self):
        text = "看看 [[emote:unknown]]"
        result = emotes.expand_emotes_in_content(text)
        assert "[[emote:unknown]]" not in result
        assert "看看" in result

    def test_no_emote_unchanged(self):
        text = "普通消息，没有表情"
        assert emotes.expand_emotes_in_content(text) == text

    def test_content_parts_list(self):
        """OpenAI content-parts 形态。"""
        content = [
            {"type": "text", "text": "我卡住了 ![卡文抓狂](emote:stuck)"},
            {"type": "image_url", "image_url": {"url": "x"}},
        ]
        result = emotes.expand_emotes_in_content(content)
        assert isinstance(result, list)
        assert "[用户表情：卡文抓狂" in result[0]["text"]
        assert result[1]["type"] == "image_url"  # 非 text 部分不动

    def test_multiple_emotes(self):
        text = "![招手问好](emote:wave) 你好 ![比心](emote:heart)"
        result = emotes.expand_emotes_in_content(text)
        assert result.count("[用户表情：") == 2
        assert "招手问好" in result
        assert "比心" in result


class TestSystemBlock:
    """P0-2 + P0-3：词表注入 + 规则 + few-shot。"""

    def test_contains_vocab_table(self):
        block = emotes.emote_system_block()
        assert "知了表情词表" in block
        assert "| 表情名(emote) | 中文含义 |" in block
        # 每个表情都应在词表里
        for eid in ["wave", "stuck", "heart", "tea"]:
            assert f"| {eid} |" in block

    def test_contains_rules(self):
        block = emotes.emote_system_block()
        assert "只能用上表列出的表情" in block
        assert "禁止发明新表情名" in block
        assert "收到用户消息中形如" in block

    def test_contains_few_shot(self):
        block = emotes.emote_system_block()
        assert "卡文抓狂" in block  # few-shot 示例 1
        assert "招手问好" in block  # few-shot 示例 2
        assert "比心" in block  # few-shot 示例 3

    def test_with_active_pack(self):
        block = emotes.emote_system_block(pack="moling-mono")
        assert "emote:moling-mono:" in block  # 带包名输出示例
        assert "moling-mono" in block

    def test_without_pack_uses_bare(self):
        block = emotes.emote_system_block(pack=None)
        # 无包名时用 emote:NAME 写法
        assert "emote:wave" in block or "emote:表情名" in block


class TestBakeAssistantEmotes:
    """助手输出回填包名。"""

    def test_bare_emote_gets_pack(self):
        emotes.set_active_pack("moling-default")
        try:
            text = "你好 ![招手问好](emote:wave)"
            result = emotes.bake_assistant_emotes(text)
            assert "emote:moling-default:wave" in result
            assert "emote:wave" not in result.replace("emote:moling-default:wave", "")
        finally:
            emotes.set_active_pack(None)

    def test_already_packed_unchanged(self):
        emotes.set_active_pack("moling-default")
        try:
            text = "![招手问好](emote:moling-mono:wave)"
            result = emotes.bake_assistant_emotes(text)
            assert result == text  # 已带包名不动
        finally:
            emotes.set_active_pack(None)

    def test_no_active_pack_unchanged(self):
        emotes.set_active_pack(None)
        text = "![招手问好](emote:wave)"
        assert emotes.bake_assistant_emotes(text) == text

    def test_non_string_unchanged(self):
        assert emotes.bake_assistant_emotes(123) == 123
        assert emotes.bake_assistant_emotes(None) is None

    def test_content_parts_list(self):
        emotes.set_active_pack("moling-default")
        try:
            content = [{"type": "text", "text": "hi ![招手问好](emote:wave)"}]
            result = emotes.bake_assistant_emotes(content)
            assert "emote:moling-default:wave" in result[0]["text"]
        finally:
            emotes.set_active_pack(None)


class TestSuggestEmote:
    """确定性表情推荐。"""

    def test_trigger_match(self):
        assert emotes.suggest_emote("太棒了，完成了！") == "cheer"

    def test_chinese_name_match(self):
        assert emotes.suggest_emote("我来比心") == "heart"

    def test_no_match_returns_none(self):
        assert emotes.suggest_emote("完全无关的文本xyz") is None

    def test_empty_text(self):
        assert emotes.suggest_emote("") is None
        assert emotes.suggest_emote(None) is None


class TestEmoteQueries:
    def test_by_emotion(self):
        happy = emotes.emotes_by_emotion("happy")
        assert "cheer" in happy
        assert "v" in happy

    def test_by_category(self):
        greeting = emotes.emotes_by_category("greeting")
        assert "wave" in greeting
        assert "shy" in greeting

    def test_list_packs(self):
        packs = emotes.list_packs()
        ids = [p["id"] for p in packs]
        assert "moling-default" in ids
        assert "moling-mono" in ids


class TestActivePack:
    def test_set_get(self):
        assert emotes.get_active_pack() is None  # 初始
        assert emotes.set_active_pack("moling-mono") is True
        assert emotes.get_active_pack() == "moling-mono"
        emotes.set_active_pack(None)
        assert emotes.get_active_pack() is None

    def test_invalid_pack_rejected(self):
        assert emotes.set_active_pack("nonexistent-pack") is False
