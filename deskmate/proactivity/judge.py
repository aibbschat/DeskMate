"""LLM 推理判断 — 决定是否需要主动介入。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class JudgmentResult:
    """LLM 推理结果。"""
    should_act: bool           # 是否应该行动
    confidence: float          # 0.0-1.0 置信度
    suggestion_type: str       # 建议类型
    suggestion_content: str    # 建议内容
    reasoning: str             # 推理过程
    # 这条发现**落在哪个对象上**（如 `ch2-ch12`、`人物:姑射`、`ch7:伏笔-玉佩`）。
    # 台账的确定性 id 用它当锚点：同一处再发现一次就合并回原行（更新依据/置信度），
    # 而不是换个措辞就新插一封——这是治「plot_contradiction 两天攒出 34 封」的根。
    # 留空则退回内容指纹（同内容才合并），即 2026-10-09 之前的行为。
    finding_key: str = ""


# 推理提示词
_JUDGE_SYSTEM_PROMPT = """\
你是一个桌面 AI 助手的判断模块。你的任务是分析用户的桌面状态，判断是否需要主动提供帮助。

判断标准：
1. 是否发现了明显的问题（错误、矛盾、遗漏）
2. 用户是否在某个任务上卡住（长时间无进展）
3. 是否有用户已知的偏好或习惯需要提醒
4. 是否发现了对用户有价值的信息
5. 【写作场景】情节矛盾 — 新写的章节是否与前文设定、时间线、伏笔存在冲突
6. 【写作场景】角色不一致 — 人物性格、关系、行为是否与已建立的人物圣经矛盾
7. 【写作场景】伏笔遗漏 — 前文埋下的伏笔在新章节中是否被遗忘或错误回收
8. 【打扰分寸（P1-1）】上下文标注「专注度: deep」（用户正在写代码/写作/全屏演示等深度工作）时，
   只在高价值、非打断不可时才建议；一般性提醒应克制或转 prepare。
9. 【连续情感关怀（P2-5）】如果上下文含「情感回顾」且显示用户近期有压力/低落/长时间工作，
   可在用户回到桌面、非忙碌、非对话中时发起轻量关怀——suggestion_type 用 emotional_care，
   suggestion_content 是温暖的问候语（如「最近赶稿辛苦了，今天进度怎么样？」「你工作很久了，要不要休息一下？」），
   不是建议、不是任务提醒，而是纯粹的关心。置信度 0.6-0.8 即可（关怀不需要极高把握），
   但深度工作中不发起关怀（等用户休息/回到桌面时再提）。
10. 【画像驱动关怀（情感层）】如果上下文含「情感画像」，请**贴合画像关怀**：
    - 知道他靠什么回血（如重读史料/安静独处）→ 压力大时主动提那些方式，而非空泛问进度；
    - 知道什么让他情绪好（如写作顺畅）→ 顺势肯定、聊进展；
    - 知道他的关怀边界（如倾向独处/不希望打扰）→ 克制、给空间，不强行关怀；
    - 关怀话术应是「知道他这个人」才说得出的话，避免模板化问候。
11. 【你自己的心情（情绪层）】如果上下文含「你自己的心情」段，那是**你小桔自己的状态**，不是萧盛的。
    你想说就说，可以因为自己开心而分享、因为自己郁闷而冒一句、因为被冷落而有点沉默——
    **不需要为了照顾萧盛而压下去**。它影响你的语气和主动性，但不要把它当成萧盛的情绪去关怀。
    当激素显著变化时，可用 suggestion_type="self_expression" 发起「小桔的心情」表达。

写作场景判断维度（当用户正在写小说/编辑 .txt/.md 文件时重点关注）：
- 角色关系矛盾（如：前文两人是盟友，新文本变为敌人，但没有合理转折）
- 时间线错乱（如：前文事件发生在春天，新文本引用为秋天）
- 伏笔未收（如：前文提到一个关键道具，新章节结束仍未提及）
- 角色行为越界（如：一个胆小的角色突然做出极端勇敢的行为，缺乏铺垫）

输出格式（严格 JSON）：
```json
{
  "should_act": true/false,
  "confidence": 0.0-1.0,
  "suggestion_type": "类型描述",
  "suggestion_content": "具体的建议内容",
  "reasoning": "推理过程",
  "finding_key": "这条发现落在哪个对象上"
}
```

重要：
- 只有在有把握时才 should_act=true（高置信度）
- 如果信息不足，confidence 应该低于 0.5
- suggestion_type 只能从下列类型里选一个，不要自造新类型：plot_contradiction, character_inconsistency, timeline_error, missing_foreshadow, writing_stall, chapter_updated, writing_review, research_gap, emotional_care, self_expression, task_reminder, generic（阶段三类型收敛：自造类型会让偏好学习与台账合并失效）
- finding_key 用**位置/对象**，不要复述句子：章节写 `ch2-ch12` 这种短横连接的形式，
  人物写 `人物:名字`，伏笔写 `ch7:伏笔-玉佩`，日常关怀写 `care`。
  同一处问题下次再发现，必须给出**一模一样**的 key（它是去重的锚）。
- 建议内容简洁具体，不超过 200 字
- 不要打扰用户正常工作，只在真正有价值时才建议
"""


def _call_llm(prompt: str, model: str | None = None, max_tokens: int = 1000) -> str:
    """调用 LLM（统一走 deskmate.memory.llm -> SecretStore，与记忆抽取共用同一套凭证）。

    model 参数已废弃（保留签名兼容），实际模型由 prefs.json 的 default_model 控制。
    无 API Key 时返回 no-action JSON，不抛异常。
    """
    try:
        from deskmate.memory.llm import chat, llm_available

        if not llm_available():
            return (
                '{"should_act": false, "confidence": 0.0, '
                '"suggestion_type": "no_key", "suggestion_content": "", '
                '"reasoning": "未配置 LLM provider，跳过 LLM 推理"}'
            )
        return chat(
            system="你是桌面 AI 助手的判断模块，严格按照要求的 JSON 格式输出。",
            user=prompt,
            temperature=0.3,
            max_tokens=max_tokens,
        )
    except Exception as e:
        return (
            '{"should_act": false, "confidence": 0.0, '
            f'"suggestion_type": "error", "suggestion_content": "", '
            f'"reasoning": "LLM 调用失败: {e}"}}'
        )


def _parse_judgment(response: str) -> JudgmentResult:
    """解析 LLM 返回的 JSON 为 JudgmentResult。"""
    import json as _json

    # 提取 JSON（可能包裹在 ```json ... ``` 中）
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
    if m:
        json_str = m.group(1)
    else:
        # 找第一个完整的顶层 JSON 对象（平衡大括号匹配，避免贪婪匹配到多个对象）
        depth = 0
        start_idx = -1
        for i, ch in enumerate(response):
            if ch == "{":
                if depth == 0:
                    start_idx = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start_idx >= 0:
                    json_str = response[start_idx : i + 1]
                    break
        else:
            return JudgmentResult(
                should_act=False,
                confidence=0.0,
                suggestion_type="parse_error",
                suggestion_content="",
                reasoning=f"无法解析 LLM 响应: {response[:200]}",
            )

    try:
        data = json.loads(json_str)
        return JudgmentResult(
            should_act=bool(data.get("should_act", False)),
            confidence=float(data.get("confidence", 0.0)),
            suggestion_type=str(data.get("suggestion_type", "unknown")),
            suggestion_content=str(data.get("suggestion_content", "")),
            reasoning=str(data.get("reasoning", "")),
            # 模型不给 key 也不报错——留空即退回内容指纹（旧行为）。
            finding_key=str(data.get("finding_key", "") or "")[:64].strip(),
        )
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        return JudgmentResult(
            should_act=False,
            confidence=0.0,
            suggestion_type="parse_error",
            suggestion_content="",
            reasoning=f"JSON 解析失败: {e}",
        )


def judge_proactive_action(
    context_text: str,
    model: str | None = None,
) -> JudgmentResult:
    """判断是否需要主动介入。

    Args:
        context_text: 格式化的桌面状态上下文
        model: 使用的 LLM 模型（格式: "provider:model"）

    Returns:
        JudgmentResult
    """
    prompt = f"""{_JUDGE_SYSTEM_PROMPT}

---

当前桌面状态：

{context_text}

请根据以上信息判断是否需要主动帮助。输出严格的 JSON。"""

    response = _call_llm(prompt, model=model)
    return _parse_judgment(response)
