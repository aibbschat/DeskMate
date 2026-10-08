# ROUND-09 指令 · 反思层 R1（江河设计 · 第二段「反思」）

> 给 Trae Work 的执行指令。本指令对应《小桔·江河设计》文档的**第二段·反思**（文档分期里的「P2·反思」）。
> 为避免与之前已做的「汇流增强 + 教学预置」（我们称为 P2：person 注册表 / 截断 / lessons）混淆，本文件称**反思层 R1**。
> 指挥员=萧哥，QC=WorkBuddy 读真实 diff + 跑测试出判词。

---

## 一、目标（文档 §三）
把世界观从「到点重新生成、覆盖旧的」变成「**对已有看法做出裁决，并留下为什么**」——
沉淀变成**有记忆的沉淀**。具体做三件事：
1. **回看判断**：翻最近的判据记录——哪几条判了、后来应验没有；
2. **回看看法**：翻 `item_history`——哪些看法被改过、为什么改；
3. **照镜子**：产出 `maintain / revise / overturn` 结论 + **理由**，落到对应的 worldview / 认知条目上。

## 二、现状（真实代码，已核对）
- `deskmate/agency/behaviors/worldview_reflection.py`（L534-661）：已实现"攒够 5 条理解 + 过 7 天冷却→LLM 生成 worldview 覆盖旧"。**只有生成、没有裁决、没有 reason 留痕**——这是"重写"不是"修订"。
- `deskmate/cognitive/store.py` 的 `item_history` 表（L353-363）：已有 `action/old_value/new_value/reason/source/ts` 四列+时序账本，但**从没被回看**（巡检 §2.5 实锤 judge 每次独立判、不记得以前怎么判）。
- `deskmate/agency/engine.py:_run_low_risk`（L110-201）：`if action_type == "worldview_reflection":` 分支是反思行为的**并列挂点**。
- `deskmate/memory/manager.py` L656-668：已有 lessons 注入块（P2-R3），反思块照此风格加。
- `deskmate/cognitive/memory_injector.py` L377-402：已有 `build_lessons_context`，反思 context 照此风格加。

## 三、设计（文档 §三 + 萧哥拍板 §八.2）
- **复用 `item_history`**，不新开 `reflections` 表（除非确需承载"回看动作本身"，本工单先复用）。
- **硬断言**：`reason` 为空 = 本次反思作废（不写 worldview、不落 record_reflection），**落代码断言**，不靠自觉。
- **风险钉死**：verdict 回写 worldview 时，"为什么变"的痕迹必须留在 `item_history` 并被召回，**不得被覆盖吞掉**（worldview_reflection 的 `_persist_worldview` 写 stance 时，必须同步在 item_history 留 `action='reflected'` + reason）。
- LLM 环节 **fail-open**（与世界观回望同风格），不许因新机制拖崩 tick。

## 四、落点（真实文件 + 行号/函数）
1. **A 新文件 `deskmate/agency/behaviors/reflection.py`**
   - `run_reflection(base_dir, config) -> dict`：仿 `worldview_reflection.run_worldview_reflection` 风（fail-open，返回 `{reflected, reason, verdicts, bailed}`）。
   - 内部：读 `item_history` 最近 N 条变化（subject_kind=judgment/profile_item/worldview）→ 调 LLM 裁决 maintain/revise/overturn + reason → 调 store 落痕。
   - LLM 用 `deskmate.memory.llm.chat_memory` + `memory_llm_available()` 闸门（参照 worldview_reflection L607-619），temperature 0.6、max_tokens 600、timeout 30。
2. **B `deskmate/agency/engine.py` `_run_low_risk`** 在 L201（`worldview_reflection` 分支后）加：
   ```python
   if action_type == "reflection":
       from .behaviors.reflection import run_reflection
       res = run_reflection(str(self.base_dir), settings.reflection)
       if not res.get("reflected"):
           return None  # 冷却中 / 无变化 / 没 provider → 不刷屏
       return self.log.add(action_type="reflection", risk="low",
           status=TaskStatus.DONE, topic="", summary=res.get("summary",""),
           detail=res.get("essay",""), count=res.get("verdicts",0))
   ```
3. **C `deskmate/agency/settings.py`**（`AgencySettings` 默认值 + `reflection` 配置段）：
   - `whitelist_actions` 默认列表加 `"reflection"`（与 `"worldview_reflection"` 并列）；
   - 新增 `reflection` 配置段（dict）：`{"cooldown_hours": 168, "max_recent": 20, "min_changes": 3}`。
   - ⚠️ 频度**与 worldview 错开**（同 7 天窗但不同批次/不同星期几，避免同 tick 双重 LLM 轰炸）。**具体错峰规则见下方「待萧哥拍板」。**
4. **D `deskmate/cognitive/store.py`** 新增：
   - `record_reflection(item_id, verdict, reason, subject_kind)`：在 `item_history` 落 `action='reflected'` + `old_value=verdict` + `reason`（reason 空直接 return False，硬断言）；
   - `get_reflections_for_injection(limit=3, char_cap=800)`：fail-open，返回最近反思结论（含 reason 摘要），永久/近期策略同 lessons。
5. **E `deskmate/cognitive/memory_injector.py`** 新增 `build_reflection_context(base_dir)`（仿 `build_lessons_context` L377，无数据空串 + fail-open）。
6. **F `deskmate/memory/manager.py`** 注入链（L656-668 lessons 块之后、parts 判空之前）加反思块：
   ```python
   try:
       if os.environ.get("DESKMATE_REFLECTION_INJECT", "1") != "0":
           from deskmate.cognitive.memory_injector import build_reflection_context
           refl_ctx = build_reflection_context(self.base_dir)
           if refl_ctx:
               parts.append("【小桔最近的自我反思】\n" + refl_ctx)
   except Exception:
       pass
   ```
   （`DESKMATE_AUTO_INJECT=0` 总开关已覆盖，无需额外处理。）

## 五、验收判据（必须逐条可测）
- `reason` 空 → `record_reflection` 返回 False 且 `item_history` 不写反射痕（断言拦）；
- 跑一次 `run_reflection` 后，`item_history` 出现 `action='reflected'` 行且 `reason` 非空；
- `get_reflections_for_injection` 返回最近的反思（含 reason 摘要），空库返回 `[]`；
- `build_full_context_for_turn` 在 `DESKMATE_REFLECTION_INJECT=1` 含「小桔最近的自我反思」块，`=0` 不含；
- 低频：不与 worldview_reflection 同日触发（配置 cooldown + 错峰）；
- fail-open：provider 缺失 / 素材不足 / 写库失败 → 返回 `{reflected:False}` 不抛、不阻断 tick；
- 不破坏既有 worldview 回望（回归 `test_*` 相关用例）。

## 六、红线（爆炸半径）
- 只改 A–F 六处 + 新增测试；**不改** worldview_reflection 主逻辑（只在 `_persist_worldview` 同步留 item_history 痕，且 fail-open）、**不改** judge 核心、不改 manager 其他注入块、不改前端；
- **不 commit**（反思层 R1 收口时与后续轮次一并 commit，走 fork/base）；
- 所有新增 LLM 调用 fail-open；不新增向量、不碰 SYNC 同步表（item_history 已有同步守卫）；
- 复用 item_history，不新开表（除非你明确要 reflections 表承载回看动作，默认不开）。

## 七、待萧哥拍板（写进指令，Trae 实现时按默认走，萧哥可改）
1. **反思频度**：默认 `cooldown_hours=168`（7 天，与 worldview 同窗）但**错峰**——worldview 用"攒够 5 条理解"触发，反思用"最近 item_history 变化 ≥ min_changes=3 条"触发，两者自然错峰；若萧哥要更稀（每周/每月），改 cooldown。
2. **是否现在启用 LLM 裁决**：默认**启用**（与 worldview 回望同款 fail-open LLM 生成 verdict+reason）。若萧哥想先埋框架（只 record_reflection 留痕、LLM 生成后置），告知 Trae 把 LLM 调用段用 `if os.environ.get("DESKMATE_REFLECTION_LLM","1")!="0"` 包起来。

## 八、测试
新增 `tests/test_p3_reflection.py`：
- `test_record_reflection_requires_reason`（空 reason 返回 False、不写痕）
- `test_record_reflection_writes_item_history`（非空 reason 落 `action='reflected'`）
- `test_get_reflections_for_injection_empty`（空库 → `[]`）
- `test_get_reflections_for_injection_recent`（最近优先 + char_cap）
- `test_build_full_context_injects_reflection_block`（manager 端到端，`=0` 关）
- `test_reflection_fail_open`（item_history 不存在/写库异常 → `record_reflection` 返回 False 不抛）

## 九、交付
写完写 `reports/bridge-trae/ROUND-09-实施记录.md`（真实落点 + 自测 + 独立实测），交 WorkBuddy re-QC。
