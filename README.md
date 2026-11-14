# DeskMate（小桔 / 墨灵）

> 基于 [OpenWorker](https://github.com/andrewyng/openworker) 二次开发的本地优先写作助手，为长篇历史小说创作与史料考证而建。

## 这是什么

DeskMate 不是通用聊天机器人，而是一套围绕「写完并守住一部长篇小说」搭起来的本地工作台。

上游 OpenWorker 提供的是通用 agent runtime：多模型接入、MCP 工具、连接器、会话与任务调度。DeskMate 在它之上加了一层写作专用的能力——记住作者的设定与已写正文、把大纲拆成分章、让不同视角的专家审读同一份稿子、在合适的时候主动递一句话，并把这一切留在作者自己的机器上。

数据默认存本地 SQLite，模型凭据由使用者自行配置；仓库不内置任何 API 密钥，也不提供托管服务。

## 主要能力

| 方向 | 内容 |
| --- | --- |
| 创作流水线 | 大纲、分章、正文、审稿、改编的串联；章节级读稿带覆盖率标记，避免没读到全文时凭空续写 |
| 记忆与连续性 | 本地持久化的人物、实体与设定索引；以「已写正文」为唯一事实基准，大纲与结论同正文冲突时以正文为准 |
| 人格与主动性 | 固定人格、情感与意志层、日记；主动提议有冷却与送达留痕，打扰节奏可控制 |
| 专家圆桌 | 写作、编辑、史料、读者等不同视角的专家，各自带独立手艺技能，按「A 断言 + 正文锚点 → B 反证」互相核验 |
| 史料与检索 | FTS5 全文检索、简繁转换、可选的本地向量检索；史料与私人资料分库，引用要求标注出处 |
| 语音 | 本地语音听写（SenseVoiceSmall / sherpa-onnx）、语音回复与桌面端语音消息 |
| 桌面端 | Tauri 2 + React 的桌面应用，Python 后端以 sidecar 方式运行 |
| 云副本与同步 | 可选的云端镜像；基于 SQLite 与操作日志的双向收敛，配合同步窗口做本地／云端对齐 |

## 技术栈

| 层 | 选型 |
| --- | --- |
| 后端 | Python 3.10+，FastAPI / uvicorn / pydantic，MCP 客户端，A2A SDK，多家模型 provider |
| 桌面 | Tauri 2（Rust）+ React 18 + TypeScript + Vite + Tailwind CSS |
| 存储 | SQLite（原子写 + 文件锁）、FTS5 全文索引；可选本地向量索引 |
| 文稿解析 | python-docx（Word 原稿）、pypdf / pypdfium2（PDF）、OpenCC（简繁） |
| 语音 | SenseVoiceSmall int8 + sherpa-onnx；Rust 录音 sidecar |
| 测试 | pytest（后端）、Vitest / Playwright（前端） |

## 从源码运行

前置：Python 3.10+、Node 20+；桌面端另需 Rust 工具链（[rustup](https://rustup.rs/)）。

```shell
git clone https://github.com/aibbschat/DeskMate.git
cd DeskMate

# 1. 一次性初始化（在仓库根创建 .venv）
bash packaging/setup_dev_env.sh

# 2. 启动本地 agent 服务
.venv/bin/openworker-server --cwd ~/some/project --port 8765

# 3. 另一个终端启动界面
cd surfaces/gui
npm install
npm run dev            # 浏览器 UI
# 或 npm run tauri dev  # 桌面窗口（Tauri 会自行拉起后端）
```

可选能力按需安装：`.venv/bin/pip install -e '.[deskmate-embed,deskmate-stt]'`（本地向量检索与中文语音听写，均为可选依赖，不装则相关功能自动回落或明确报错）。

模型服务商的凭据需自行配置，项目不内置任何密钥。桌面打包脚本见 `packaging/`：`build_local_app.sh` 是本机开发构建（依赖仓库内 `.venv`），`build_dmg.sh` 是含 sidecar 的正式 macOS 包。

测试：仓库根 `.venv/bin/pytest`；`surfaces/gui` 下 `npm test` 与 `npm run e2e`。

## 仓库结构

| 路径 | 内容 |
| --- | --- |
| `coworker/` | 上游 agent runtime：引擎、模型 provider、连接器、MCP、会话、史料检索、权限 |
| `deskmate/` | 二开核心：人格、记忆、创作、圆桌、主动行为、感知、语音、路径守卫 |
| `surfaces/gui/` | 桌面前端与 Tauri 外壳 |
| `stt/` | 语音听写 Rust sidecar |
| `packaging/` | 开发环境初始化与桌面打包脚本 |
| `scripts/` | 运维、部署与同步脚本 |
| `tests/` | 后端 pytest 套件 |
| `docs/` · `reports/` | 设计文档、决策记录与审计报告 |
| `design/` · `ui-mocks/` | 视觉稿与交互样张 |
| `skills/` | 技能样例 |

开发主线为 `main`；仓库同时保留 `fork/base` 作为本地开发分支的镜像，两者内容一致。

## 状态与边界

- 个人项目，**不是 OpenWorker 官方分支或发行版**；不提供安装包、托管服务或技术支持。
- 上游代码持续演进，本仓库按自身节奏合入，不保证与上游同步。
- 默认本地优先：对话、文稿与记忆留在使用者机器上；启用云副本时的数据流向由使用者自己掌控。
- `docs/` 与 `reports/` 是开发过程的设计稿与审计笔记，不是使用文档，内容可能早于当前实现。

## 许可证与致谢

MIT。上游 OpenWorker 版权归 Andrew Ng (2024)，二次开发部分版权归 Sheng Xiao (2026)，详见 [LICENSE](LICENSE)——文件保留两段版权声明与完整 MIT 正文。

第三方组件保留各自的许可，例如 `coworker/history/vendor/opencc/`（Apache-2.0）；运行时依赖涉及 fastembed / onnxruntime / BGE（Apache-2.0、MIT）、sherpa-onnx 与 SenseVoice（Apache-2.0）、pypdfium2（BSD）等，许可说明见 `pyproject.toml` 注释。

感谢 [Andrew Ng 与 OpenWorker 贡献者](https://github.com/andrewyng/openworker)、[aisuite](https://github.com/andrewyng/aisuite)，以及上述开源项目。
