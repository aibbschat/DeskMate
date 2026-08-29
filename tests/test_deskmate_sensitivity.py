"""8.4 敏感信息分级 — 单元测试。

覆盖：分级（普通/敏感/隐私）、未成年人→隐私、redact 脱敏、
章节标记（幂等）、mark_memory_text 分节打 tag、should_sync。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deskmate.memory.sensitivity import (
    NORMAL,
    SENSITIVE,
    PRIVATE,
    SENSITIVE_TAG,
    PRIVATE_TAG,
    NOSYNC_TAG,
    classify,
    is_sensitive,
    redact,
    mark_section,
    mark_memory_text,
    should_sync,
)


def test_classify_normal():
    lvl, reasons = classify("用户喜欢在周一早上要进度摘要")
    assert lvl == NORMAL
    assert reasons == []


def test_classify_sensitive_phone():
    lvl, reasons = classify("我的手机号是 13812345678")
    assert lvl == SENSITIVE
    assert "手机号" in reasons


def test_classify_sensitive_address():
    lvl, _ = classify("我家住北京市海淀区某某路")
    assert lvl >= SENSITIVE


def test_classify_private_minor_age():
    # 「儿子 + 7岁」→ 未成年人 → 隐私（最高级）
    lvl, reasons = classify("我儿子今年7岁，正在上小学")
    assert lvl == PRIVATE
    assert "未成年人年龄" in reasons


def test_classify_private_id_card():
    lvl, _ = classify("身份证号 11010119900307123X 已登记")
    assert lvl == PRIVATE


def test_is_sensitive_true_false():
    assert is_sensitive("手机 13900001111") is True
    assert is_sensitive("用户偏好历史悬疑题材") is False


def test_redact_phone():
    out = redact("联系我 13812345678 谢谢")
    assert "13812345678" not in out
    assert "1**********" in out


def test_redact_id_card():
    out = redact("证号 11010119900307123X 已核")
    assert "11010119900307123X" not in out


def test_redact_minor_age():
    out = redact("我女儿5岁")
    assert "5岁" not in out
    assert "**岁" in out


def test_mark_section_idempotent():
    h = "§ 家庭情况 #preference"
    h1 = mark_section(h, SENSITIVE)
    assert SENSITIVE_TAG in h1
    h2 = mark_section(h1, SENSITIVE)
    assert h2 == h1  # 已标不重复加


def test_mark_section_private_adds_nosync():
    h = mark_section("§ 子女信息", PRIVATE)
    assert PRIVATE_TAG in h
    assert NOSYNC_TAG in h


def test_mark_section_normal_unchanged():
    assert mark_section("§ 普通偏好", NORMAL) == "§ 普通偏好"


def test_mark_memory_text_adds_tags():
    text = (
        "§ 写作偏好 #preference\n用户爱写历史悬疑\n\n"
        "§ 家庭情况\n我儿子7岁，家住杭州\n\n"
        "§ 工作进展\n本周完成第三章"
    )
    out, count = mark_memory_text(text)
    assert count == 1  # 仅「家庭情况」命中隐私被打 tag
    assert "§ 家庭情况 #private #no-sync" in out
    assert "§ 写作偏好 #preference" in out  # 普通不被改
    assert "§ 工作进展" in out


def test_mark_memory_text_idempotent():
    text = "§ 家庭情况\n我儿子7岁"
    out1, c1 = mark_memory_text(text)
    out2, c2 = mark_memory_text(out1)
    assert c1 == 1
    assert c2 == 0  # 二次标记不再计
    assert out1 == out2


def test_should_sync():
    assert should_sync({"preference"}) is True
    assert should_sync({"private", "no-sync"}) is False
    assert should_sync(set()) is True
