"""Tests for 8.5 画像迁移——全局创作偏好跨项目自动注入（deskmate/creation/global_profile.py）。

核心：换新书时全局风格画像 + vault 偏好自动注入改编圣经上下文，让开新书后 AI 还懂你。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deskmate.creation import pipeline as P
from deskmate.creation.global_profile import (
    PROFILE_MARK,
    build_global_profile,
    has_profile,
    inject_profile,
    load_and_inject,
)


def _write_style_profile(desk: Path, summary: str, features: dict | None = None) -> None:
    data = {"summary": summary, "features": features or {"sentence_style": "短句为主"}}
    (desk / "style_profile.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _write_preference(desk: Path, name: str, content: str) -> None:
    d = desk / "vault" / "preferences"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(content, encoding="utf-8")


class TestBuildGlobalProfile:
    def test_with_style_and_preferences(self, tmp_path):
        desk = tmp_path
        _write_style_profile(desk, "历史悬疑小说，半文半白", {"sentence_style": "短句为主", "rhythm": "节奏明快"})
        _write_preference(desk, "2026-08-26.md", "2026-08-26: 用户希望 AI 更懂用户。")
        profile = build_global_profile(desk)
        assert PROFILE_MARK in profile
        assert "历史悬疑小说" in profile
        assert "短句为主" in profile
        assert "更懂用户" in profile
        assert "全局创作偏好" in profile

    def test_empty_dir_returns_empty(self, tmp_path):
        assert build_global_profile(tmp_path) == ""

    def test_only_style_profile(self, tmp_path):
        _write_style_profile(tmp_path, "权谋正剧")
        profile = build_global_profile(tmp_path)
        assert "权谋正剧" in profile
        assert PROFILE_MARK in profile

    def test_only_preferences(self, tmp_path):
        _write_preference(tmp_path, "2026-08-26.md", "用户偏好务实方案。")
        profile = build_global_profile(tmp_path)
        assert "务实方案" in profile
        assert PROFILE_MARK in profile

    def test_writing_style_note_detected(self, tmp_path):
        _write_preference(tmp_path, "萧盛写作风格.md", "# 萧盛写作风格关键特征\n\n1. 短句为主")
        profile = build_global_profile(tmp_path)
        assert "萧盛写作风格" in profile

    def test_profile_length_bounded(self, tmp_path):
        """聚合输出应控制在合理范围（不超过 800 字），避免污染圣经。"""
        _write_style_profile(tmp_path, "x" * 500, {"sentence_style": "y" * 200})
        _write_preference(tmp_path, "2026-08-26.md", "z" * 300)
        profile = build_global_profile(tmp_path)
        assert len(profile) < 800


class TestInjectProfile:
    def test_inject_before_toc(self):
        ctx = "# 改编圣经\n\n顾贞观\n\n【章节目录】\n第1章"
        profile = f"【全局创作偏好】{PROFILE_MARK}\n- 文风：权谋"
        out = inject_profile(ctx, profile)
        assert out.index("全局创作偏好") < out.index("章节目录")
        assert "权谋" in out

    def test_idempotent(self):
        ctx = f"# bible\n\n【全局创作偏好】{PROFILE_MARK}\n\n【章节目录】"
        profile = f"【全局创作偏好】{PROFILE_MARK}\n- 新内容"
        out = inject_profile(ctx, profile)
        assert out == ctx  # 已注入则不重复

    def test_empty_profile_noop(self):
        ctx = "# bible\n\n【章节目录】"
        assert inject_profile(ctx, "") == ctx

    def test_no_toc_appends_end(self):
        ctx = "# 改编圣经\n\n顾贞观"
        profile = f"【全局创作偏好】{PROFILE_MARK}\n- 文风"
        out = inject_profile(ctx, profile)
        assert out.endswith(profile)

    def test_has_profile(self):
        assert has_profile(f"text {PROFILE_MARK} end")
        assert not has_profile("no mark here")


class TestLoadAndInject:
    def test_end_to_end(self, tmp_path):
        _write_style_profile(tmp_path, "历史悬疑")
        ctx = "# bible\n\n【章节目录】\n第1章"
        out = load_and_inject(ctx, base_dir=tmp_path)
        assert "历史悬疑" in out
        assert PROFILE_MARK in out

    def test_no_data_returns_original(self, tmp_path):
        ctx = "# bible\n\n【章节目录】"
        assert load_and_inject(ctx, base_dir=tmp_path) == ctx


class MockNovelIndex:
    """模拟原著索引，供 pipeline 集成测试。"""
    def build(self, degrade: bool = False) -> dict:
        return {"bible": "# 改编圣经\n\n顾贞观：主角\n提塘官：配角"}
    def chapter_toc(self) -> str:
        return "第1章 开局\n第2章 发展\n第3章 高潮"


class TestPipelineIntegration:
    def test_novel_context_injects_global_profile(self, tmp_path, monkeypatch):
        """CreativePipeline._novel_context('bible') 应自动注入全局创作偏好。"""
        _write_style_profile(tmp_path, "权谋正剧，半文半白")
        pl = P.CreativePipeline(space="test:profile", base_dir=tmp_path, executor=P.StubExecutor())
        pl.novel_index = MockNovelIndex()
        ctx = pl._novel_context("bible")
        assert "权谋正剧" in ctx
        assert PROFILE_MARK in ctx
        assert "章节目录" in ctx
        # 全局偏好在章节目录之前
        assert ctx.index("全局创作偏好") < ctx.index("章节目录")

    def test_novel_context_cached(self, tmp_path, monkeypatch):
        """_novel_context 懒加载缓存，第二次调用不重复注入。"""
        _write_style_profile(tmp_path, "悬疑")
        pl = P.CreativePipeline(space="test:cache", base_dir=tmp_path, executor=P.StubExecutor())
        pl.novel_index = MockNovelIndex()
        ctx1 = pl._novel_context("bible")
        ctx2 = pl._novel_context("bible")
        assert ctx1 == ctx2
        assert ctx1.count(PROFILE_MARK) == 1  # 只注入一次

    def test_no_novel_index_returns_empty(self, tmp_path):
        pl = P.CreativePipeline(space="test:empty", base_dir=tmp_path, executor=P.StubExecutor())
        assert pl._novel_context("bible") == ""
