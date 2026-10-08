"""memory_append 在 LLM 端点故障时的熔断降级行为（R12 后记忆写不进的根因修复）。

根因：deskmate-memory MCP 写 section='memory' 时，去重/合并逻辑强制调 LLM
（_check_cross_section_duplicate + _try_merge_similar），而这些走 opencode.ai/zen；
该端点故障时 SDK 自动重试把 4-8s 放大成 MCP 超时，且相似条目被「跳过」白写。

修复：append_to_section 的 memory 段改为熔断感知——opencode 免费组不可用
（catalog 拉不到 / 已熔断）时跳过 LLM 查重、纯文件追加，不卡、不误跳。
"""
import pytest
from unittest.mock import patch

from deskmate.memory.manager import DeskMateMemoryManager


def test_memory_append_skips_llm_when_free_group_unavailable(tmp_path):
    """端点故障（chat 通道熔断）→ memory 段纯文件追加，不调 LLM、不卡、不误跳。"""
    mgr = DeskMateMemoryManager(base_dir=tmp_path)
    with patch.object(DeskMateMemoryManager, "_memory_llm_ok", return_value=False), \
         patch("deskmate.memory.manager.chat") as mock_chat:
        res = mgr.append_to_section(
            "memory",
            "#correction 萧盛确认《国子监秘闻录》背景：哲宗亲政夕高太后已崩",
            target_section="correction",
        )
        assert "已写入" in res, res
        mock_chat.assert_not_called()  # 端点坏时绝不应发起 LLM 调用


def test_memory_append_uses_llm_when_available(tmp_path):
    """端点正常（_memory_llm_ok=True）→ memory 段仍走 LLM 查重/合并。

    关键点：必须先预置一个已存在的同 § 条目，第二次写入才会进入 found 分支
    触发 _try_merge_similar（新 § 直接追加、不调 LLM，否则 mock_chat 永不被调用）。
    这里直接把熔断感知门 _memory_llm_ok 钉为 True，等价于「端点可用」；
    mock_chat 统一返回 "NO_DUP"：跨§查重不短路（返回 None）、合并因安全校验
    失败返回 None 走纯追加——两条路径都真实发起了 LLM 调用。
    """
    mgr = DeskMateMemoryManager(base_dir=tmp_path)
    with patch.object(DeskMateMemoryManager, "_memory_llm_ok", return_value=True), \
         patch("deskmate.memory.manager.llm_available", return_value=True), \
         patch("deskmate.memory.manager.chat") as mock_chat:
        mock_chat.return_value = "NO_DUP"
        # 首次：在 § correction 建一条既有条目（正文 ≥30 字，新 § 不调 LLM；
        # 但为二次写入的 _try_merge_similar 提供足够长的 existing_body）
        mgr.append_to_section(
            "memory",
            "萧盛今日确认旧设定版本：高太后仍在宫中垂帘听政，哲宗尚未亲政，"
            "此为之前小桔曾误判采用的旧 canon，需以新拍板为准。",
            target_section="correction",
        )
        mock_chat.reset_mock()  # 只数「第二次写入」发起的 LLM 调用
        # 二次：同 § 写入新信息 → 跨§查重 + 同主题合并 两处都应调 LLM
        res = mgr.append_to_section(
            "memory", "萧盛确认新设定：高太后已崩、哲宗亲政夕", target_section="correction"
        )
        assert "已写入" in res, res
        mock_chat.assert_called()  # 端点可用时应调 LLM
