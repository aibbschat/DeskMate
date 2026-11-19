"""小说一致性校验域（从 core.py 机械抽取，PR16/#33）。

规则校验 / LLM 校验 / 增强校验。行为与抽取前一致。
_chat / _extract_relationship_pairs / _RELATION_OPPOSITES 经父模块 core 命名空间
解析（_L），故既有 monkeypatch.setattr(core, "_chat", ...) 用例无需改动。
core.py 通过 re-export 保持 from deskmate.novel_memory.core import X 不变。
"""

from __future__ import annotations

import re

# _L 懒代理父模块（复检修复）：避免子模块被「首个直接导入」时与父模块 re-export 循环导入。
# 仅在函数调用访问 _L.X 时解析父模块（此时必已完整加载）；monkeypatch(父, X) 经 getattr 实时生效。
class _LazyParent:
    def __getattr__(self, name):
        from deskmate.novel_memory import core as _m
        return getattr(_m, name)
_L = _LazyParent()


def check_consistency(bible_docs: dict[str, str], new_text: str) -> dict:
    """检测新文本是否与人物圣经中的关系存在矛盾。

    纯逻辑函数，不调用 LLM。基于关键词对立检测。

    Args:
        bible_docs: 从 read_bible() 或 _parse_bible_docs() 得到的圣经文档字典。
        new_text: 新写的章节文本。

    Returns:
        {"consistent": bool, "issues": list[str], "checked_pairs": int}
    """
    chars_text = bible_docs.get("CHARACTERS", "")
    if not chars_text:
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    bible_pairs = _L._extract_relationship_pairs(chars_text)
    if not bible_pairs:
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    issues: list[str] = []
    checked = 0

    for (a, b), rel_words in bible_pairs.items():
        # 只检查新文本中同时提到两个角色的情况
        if a not in new_text or b not in new_text:
            continue
        checked += 1

        # 判断圣经中这对角色的关系方向
        bible_is_friendly = any(
            fw in rw for rw in rel_words
            for fw in ("友", "好", "爱", "伙伴", "联盟")
        )
        bible_is_hostile = any(
            hw in rw for rw in rel_words
            for hw in ("敌", "仇", "恨", "对立")
        )

        # 在新文本中找这两个角色共同出现的句子
        sentences = re.split(r"[。！？\n]", new_text)
        for sent in sentences:
            if a not in sent or b not in sent:
                continue

            # 检查句子中是否出现与圣经矛盾的关系词
            # pos_word = 敌/仇/恨, neg_word = 友/爱
            # 圣经友好 + 文本出现敌意词(pos_word) → 矛盾
            # 圣经敌对 + 文本出现友好词(neg_word) → 矛盾
            for pos_word, neg_word in _L._RELATION_OPPOSITES:
                if bible_is_friendly and pos_word in sent:
                    issues.append(
                        f"矛盾：圣经中「{a}」与「{b}」为友好关系，"
                        f"但新文本中「{sent.strip()}」暗示敌对"
                    )
                    break  # 一句话只报一次
                if bible_is_hostile and neg_word in sent:
                    issues.append(
                        f"矛盾：圣经中「{a}」与「{b}」为敌对关系，"
                        f"但新文本中「{sent.strip()}」暗示友好"
                    )
                    break

    # 去重
    issues = list(dict.fromkeys(issues))
    return {
        "consistent": len(issues) == 0,
        "issues": issues,
        "checked_pairs": checked,
    }


# ---------------------------------------------------------------------------
# LLM 语义级矛盾检测
# ---------------------------------------------------------------------------

_CONSISTENCY_LLM_SYS = (
    "你是长篇小说的人物关系审核员。给你一份【人物关系圣经】和一段【新章节文本】。"
    "你的任务是判断新章节是否存在与圣经矛盾的人物关系变化。\n"
    "\n"
    "重点检测以下隐含矛盾（正则检测不到的）：\n"
    "1. 关系性质变化但未明确写出关键词（如「投靠」「背叛」「暗通」「倒戈」"
    "暗示阵营转变，但未出现「敌」「友」字样）\n"
    "2. 行为与关系不一致（圣经中是盟友，新文本中一方偷偷设局对付另一方）\n"
    "3. 态度转变缺乏铺垫（长期友好的角色突然冷淡/敌视，无合理转折）\n"
    "4. 阵营归属矛盾（圣经中属A阵营，新文本中表现为B阵营效力）\n"
    "\n"
    "只报告有把握的矛盾。如果新章节有合理的剧情转折铺垫，不算矛盾。\n"
    "输出严格 JSON：\n"
    '{"consistent": true/false, "issues": ["矛盾1描述", "矛盾2描述"], "reasoning": "简述"}\n'
    "consistent=true 时 issues 为空数组。只输出 JSON，不要其他文字。"
)


def check_consistency_llm(bible_docs: dict[str, str], new_text: str) -> dict:
    """LLM 语义级矛盾检测。

    能识别正则检测不到的隐含关系变化（投靠、背叛、暗通、行为矛盾等）。
    需要 LLM provider 凭证；无密钥或异常时返回空结果（降级为纯正则检测）。

    Args:
        bible_docs: 从 read_bible() 得到的圣经文档字典。
        new_text: 新写的章节文本。

    Returns:
        {"consistent": bool, "issues": list[str], "checked_pairs": int}
    """
    chars_text = bible_docs.get("CHARACTERS", "")
    if not chars_text or not new_text.strip():
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    # 复用 deskmate.memory.llm 的 llm_available（走 SecretStore，自动跟随 provider）
    try:
        from deskmate.memory.llm import llm_available
    except ImportError:
        llm_available = lambda: False  # type: ignore

    if not llm_available():
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    # 构建圣经摘要（限制长度避免 token 爆炸）
    bible_summary = chars_text[:4000]
    outline = bible_docs.get("OUTLINE", "")
    if outline:
        bible_summary += f"\n\n## 情节主线\n{outline[:1500]}"

    new_text_trimmed = new_text[:6000]

    user_prompt = (
        f"【人物关系圣经】\n{bible_summary}\n\n"
        f"---\n\n【新章节文本】\n{new_text_trimmed}\n\n"
        f"请判断新章节是否与圣经中的人物关系存在矛盾。"
    )

    try:
        resp = _L._chat(_CONSISTENCY_LLM_SYS, user_prompt, temperature=0.15)
    except Exception:
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    # 解析 JSON
    import json

    m = re.search(r"\{.*\}", resp, re.DOTALL)
    if not m:
        return {"consistent": True, "issues": [], "checked_pairs": 0}

    try:
        data = json.loads(m.group(0))
        issues = [str(i) for i in data.get("issues", []) if i]
        return {
            "consistent": bool(data.get("consistent", True)) and len(issues) == 0,
            "issues": issues,
            "checked_pairs": len(issues),  # LLM 检测到的矛盾数
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"consistent": True, "issues": [], "checked_pairs": 0}


def check_consistency_enhanced(bible_docs: dict[str, str], new_text: str) -> dict:
    """增强矛盾检测：先跑正则快速检测，有 API Key 时再跑 LLM 语义检测，合并结果。

    Args:
        bible_docs: 从 read_bible() 得到的圣经文档字典。
        new_text: 新写的章节文本。

    Returns:
        {"consistent": bool, "issues": list[str], "checked_pairs": int}
    """
    # 第一层：正则快速检测（永远执行）
    regex_result = check_consistency(bible_docs, new_text)

    # 第二层：LLM 语义检测（有 API Key 时执行）
    llm_result = check_consistency_llm(bible_docs, new_text)

    # 合并结果
    all_issues = regex_result["issues"] + llm_result["issues"]
    # 去重（LLM 和正则可能报告同一矛盾）
    all_issues = list(dict.fromkeys(all_issues))

    return {
        "consistent": len(all_issues) == 0,
        "issues": all_issues,
        "checked_pairs": regex_result["checked_pairs"] + llm_result["checked_pairs"],
    }
