# OpenWorker 专家系统 vs QwenPaw Skills 对标改造方案

> 背景：阿里 AgentScope 团队开源 QwenPaw（原 CoPaw，Apache 2.0，`github.com/agentscope-ai/QwenPaw`）。
> 其 Skills 插件机制与 OpenWorker 专家系统高度同构。本文做代码级对标，并给出可落地的改造项。
> 取证日期：2026-08-11。QwenPaw 源码 HEAD `1bc16a9`。

---

## 一、两边现状速览

| 维度 | QwenPaw Skills | OpenWorker 专家系统 |
|---|---|---|
| 定义格式 | 目录 + `SKILL.md`（YAML frontmatter）+ 可选 `references/`、`scripts/` | `deskmate/memory/experts.ts` 里硬编码的 `Expert[]` 数组 |
| 注册机制 | 双库 manifest（`skill.json`，原子写 + `fcntl` 跨进程锁），`enabled`/`channels` 路由 | 编译期静态数组，UI 直接读 `EXPERTS` 渲染卡片 |
| 工具暴露 | **不进 function-calling schema**，靠 prompt 注入 + `/skill` 斜杠命令；第三方 harness 才显式导出 | `systemPrompt` 注入对话后端，靠 LLM 扮演角色；`manuscriptReview` 等是数据驱动开关 |
| 热加载 | 目录增删即改 manifest，下次 `resolve_effective_skills` 自然生效；无重启 | 改 `experts.ts` 需重编前端 |
| 分发/市场 | `hub.py` 多源安装（clawhub/url/zip/github/modelscope/aliyun…）+ 共享池 auto_update | 无 |
| 安全 | 工具守卫 + 文件访问控制 + 技能安全扫描 | 自动任务 `Mode.AUTO` 放行 consequential + 写入路径作用域 + CRITICAL 拒杀两道硬防线 |

**关键差异一句话**：QwenPaw 的 Skill 是"文件系统对象 + prompt 注入"，咱们的专家是"源码常量 + 角色扮演"。
它在「热加载/分发」上比咱们强；咱们在「工具可被模型主动调用」「垂直深度（圣经/编校）」上比它强。

---

## 二、QwenPaw 机制取证（关键源码）

### 1. Skill 定义 = 目录 + SKILL.md（`src/qwenpaw/agents/skill_system/store.py:965`）
```python
def read_skill_from_dir(skill_dir, source):
    skill_md = skill_dir / "SKILL.md"
    content = read_text_file_with_encoding_fallback(skill_md)
    post = frontmatter.loads(content)            # 解析 YAML frontmatter
    description = str(post.get("description", "") or "")
    references = _directory_tree(skill_dir / "references") if (skill_dir/"references").exists() else {}
    scripts    = _directory_tree(skill_dir / "scripts")    if (skill_dir/"scripts").exists()    else {}
    return SkillInfo(name=skill_dir.name, description=description, content=content, source=source, ...)
```
`validate_skill_content`（L1011）强制 `name`/`description` 非空、`metadata` 为 dict。

### 2. 发现/注册 = 双库 + manifest 路由（`src/qwenpaw/agents/skill_system/registry.py:1201`）
```python
def resolve_effective_skills(workspace_dir, channel_name) -> list[str]:
    manifest = read_skill_manifest(workspace_dir)
    for skill_name, entry in sorted(manifest.get("skills", {}).items()):
        if not entry.get("enabled", False):
            continue
        channels = entry.get("channels") or ["all"]
        if "all" in channels or channel_name in channels:
            resolved.append(skill_name)
    return resolved
```
- 两层存储：`WORKING_DIR/skill_pool`（共享复用）+ `<workspace>/skills`（每 agent 可编辑）。
- manifest 原子写：`mutate_json` + `fcntl.LOCK_EX` 跨进程锁。

### 3. MCP 工具暴露 = 统一 "Driver" + 热插拔（`src/qwenpaw/drivers/manager.py:197`）
```python
async def reload_driver(self, name):
    handler = await self._build_and_init_handler(card)   # 先建新连接
    async with self._lock:
        old = self._handlers.get(name)
        self._handlers[name] = handler                    # 原子替换
    if old is not None:
        await self._shutdown_handler(old)                 # 优雅关停旧连接
```
更轻量 `refresh_driver`（L230）仅在 `endpoint/credentials/enabled` 变化时重连；`DriverConfigWatcher` 每 2s 轮询 `drivers/<protocol>/<name>.yaml` 触发磁盘热更新。**核心：build-before-swap，失败保留旧 handler，不断服务。**

### 4. Spawn Subagent = 隔离子代理（`src/qwenpaw/agents/tools/agent_management.py:1024`）
```python
async def spawn_subagent(task, fork=False, background=False, timeout=600,
                          allowed_tools=None, skills=None, batch=None):
    """在当前 workspace 内派生临时子代理：全新会话上下文，共享身份/工作区，
       默认继承全部工具与 skills；用 allowed_tools/skills 白名单裁剪。"""
```
子代理 skills 在 `runtime/builder.py:281` 做交集过滤：
```python
subagent_skills = request_context.get("subagent_skills")
if isinstance(subagent_skills, list):
    parent_set = set(effective_skills)
    effective_skills = [s for s in subagent_skills if s in parent_set]
```
`fork=True` 在独立 git worktree 工作；批量上限 `MAX_SPAWN_BATCH_SIZE=10`、并发 `MAX_SPAWN_BATCH_CONCURRENCY=3`。

---

## 三、专家系统改造方案（4 个可落地项）

### 改造项 A：专家定义从「源码常量」改为「文件清单 + manifest」（对标 QwenPaw SKILL.md）
- **现状**：`experts.ts` 里 `Expert[]` 硬编码，新增专家要改源码、重编译前端。
- **目标**：把每个专家抽成独立目录 `deskmate/experts/<id>/EXPERT.md`（frontmatter 存 id/displayName/icon/description/capabilities/status/personaId/manuscriptReview/metadata），外加 `systemPrompt.md`、`references/`。`experts.ts` 退化为 `loadExperts()` —— 启动时扫描目录，解析 frontmatter，缓存。
- **收益**：① 新增/禁用专家零编译；② 为「用户自定义专家 / 社区分发」铺路；③ 与 QwenPaw 心智模型一致，未来可互导。
- **风险**：frontmatter 解析需容错（缺字段给默认值），保持向后兼容（内置 5 专家仍静态打包，自定义叠加）。
- **工作量**：约 1 天。

### 改造项 B：专家注册支持「启用开关 + 渠道路由」（对标 `resolve_effective_skills`）
- **现状**：所有专家全量渲染、全量可用。
- **目标**：manifest 加 `enabled` 字段 + `channels`（如 `["roundtable", "chat", "all"]`），圆桌/对话/自动任务各取所需。
- **收益**：自动任务场景可只激活「编辑专家」，避免无关系统提示污染；与 A 项同做。

### 改造项 C：专家热加载（对标 QwenPaw 目录扫描）
- **现状**：改 `experts.ts` 需重编。
- **目标**：A 项落地后，专家清单 = 启动时扫描 + 监听目录变化（或用"刷新专家"API）。不重启进程即可生效。
- **收益**：开发调试、用户装自定义专家即时可用。

### 改造项 D：专家工具主动调用（咱们的强项，保留并强化）
- **现状**：专家靠 `systemPrompt` 让 LLM 扮演角色，无法被模型作为"工具"主动调用。
- **决策**：**保留现状**。QwenPaw 反向——它的 Skill 不进 function-calling schema，只能 prompt 注入。咱们的"角色扮演 + 数据驱动开关"对创作陪伴场景更自然，不必为对标而退化。
- **注意**：若未来需要"专家作为可调用工具"（如圆桌里按需召唤某专家做专项分析），可在 A2A/工具层加一层适配，而不是改专家本体。

---

## 四、圆桌 MCP 热插拔 / Spawn Subagent 改造项

> 现状已读：`deskmate/roundtable/coordinator.py`。圆桌是单轮编排（host + 4 进程内 persona + 外部 A2A peer），
> 上下文装配在 `_build_story_context`（L0/L1/L2 分层 + FTS5 检索），续聊走 `context` 重跑整场（已知体验债）。

### 改造项 E：圆桌 MCP 热插拔（对标 QwenPaw `reload_driver`）
- **现状**：圆桌外部能力仅靠硬编码 `PeerSpec`（Hermes/OpenCode），无动态工具接入；MCP 未在圆桌接入。
- **目标**：引入一层「圆桌 Driver/工具注册表」，支持：
  - 配置即加载：从 `drivers/<protocol>/<name>.yaml`（含 `endpoint`/`credentials`/`enabled`/`policy`）启动；
  - 热插拔：监听配置变化，`build-before-swap`（先建新连接成功再替换，失败保留旧连接），不断服务；
  - 策略热更：`policy`（allow/ask/deny）变化不重连，仅刷新决策。
- **收益**：圆桌可随时接新外部能力（高德地图、金融 MCP、搜索…），无需改代码重启；直接享受大厂 MCP 生态（见第五节）。
- **风险**：需限制圆桌对外部工具的写入权限（沿用现有 CRITICAL 拒杀 + 写入路径作用域）。
- **工作量**：约 2 天。

### 改造项 F：圆桌 Spawn Subagent（对标 `spawn_subagent`）
- **现状**：4 个 persona 在同一编排里共享同一份 `story_block`，长任务会互相污染、上下文膨胀。
- **目标**：允许把某个 persona（或临时专项分析）派生为「隔离子代理」：
  - 独立会话上下文（不继承整场 transcript，只拿 topic + 圣经摘要 + 指定章节）；
  - 工具/skills 白名单交集过滤（仅给该子代理需要的）；
  - 结果回传主会话，汇入纪要。
- **收益**：① 长任务隔离，避免上下文爆炸；② 可并行跑多个专项分析（如"只让人物专家深挖某角色弧光"）；③ 缓解续聊重跑整场的体验债（子代理按需现拉，不用重跑全场）。
- **风险**：子代理结果需做"锚定回归"——强制基于圣经/正文，防漂移（复用现有 `_bible_summary` 护栏声明）。
- **工作量**：约 2-3 天。

---

## 五、大厂插件生态对齐策略（"原生支持大厂插件，接入即用"）

### 关键发现：大厂插件正在快速收敛到 MCP 统一标准
- **阿里**：百炼平台 One Key MCP（2026-08-05 上线），一个 API Key 调全部 MCP 服务，首批 14 家伙伴（高德、东方财富、恒生聚源、得理法律、e签宝…）；Qwen3 原生支持 MCP。
- **腾讯**：元器 Agent 转 MCP Server（`tcb.cloud.tencent.com/mcp-server/yuanqi-agent`）；元器插件 MCP（`yuanqi-tool`，含 OCR、PDF 解析、搜狗搜索、文生图、AI 写作…）；微信支付 MCP 插件；腾讯云智能体平台预置 MCP 插件广场（腾讯位置服务等）。
- **字节**：扣子空间集成 60+ MCP 扩展；Trae（AI 原生 IDE）兼容 MCP。
- **百度**：千帆平台 MCP，全球首个电商交易类 MCP。
- **国际**：OpenAI、Google 均已支持 MCP；MCP.so 收录 11000+ MCP Server，魔搭社区 2900+。

### 策略建议：以 MCP 为主轴，不做私有适配
1. **主推 MCP 跨厂商兼容**（最优）：把第五节 E 项的「圆桌 Driver/工具注册表」直接做成 MCP Host —— 凡是大厂（阿里/腾讯/字节/百度）或社区发布的 MCP Server，填入 endpoint/key 即可接入圆桌/专家，**一个适配层吃下所有大厂**。这是性价比最高的"借生态"。
2. **可选 QwenPaw 风格适配层**（次级）：仅当想直接消费 QwenPaw 的 `SKILL.md` 格式社区插件时，做一个轻量转换器（SKILL.md → 咱们的 EXPERT.md / MCP 工具），不必重造。
3. **不做的事情**：不自建插件市场（萧哥已判断"自己做生态不现实"）；不绑定单一厂商私有协议。
4. **安全护栏**（必做）：沿用 CRITICAL 拒杀 + 写入路径作用域；MCP 工具默认 `ask` 策略（需确认再执行写操作），`allow` 仅限只读检索类。

### 一句话结论
**"对齐大厂插件生态"的正确解不是逐个对接，而是成为 MCP Host —— MCP 已经是大厂（阿里百炼/腾讯元器/字节扣子/百度千帆）共同押注的事实标准，做好一层 MCP 接入，等于原生支持所有这些平台的插件，接入即用。**

---

## 六、落地优先级（建议）
1. **改造项 A + B**（专家文件化 + manifest 路由）—— 1 天，低风险，打通"自定义/分发"基础。
2. **改造项 E**（圆桌 MCP 热插拔）—— 2 天，直接解锁大厂生态，ROI 最高。
3. **改造项 F**（Spawn Subagent）—— 2-3 天，缓解续聊债 + 长任务隔离。
4. **改造项 C**（专家热加载）—— 随 A 顺带做。
5. 改造项 D 暂不做（保留角色扮演优势）。

---
*本方案基于 QwenPaw HEAD 1bc16a9 源码与 OpenWorker fork/base 当前代码（2026-08-11）对标撰写。*
