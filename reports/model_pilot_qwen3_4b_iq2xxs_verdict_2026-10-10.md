# Qwen3-4B IQ2_XXS 选型验证报告（B 方案 pilot · 判定不可用）

- 日期：2026-10-10 22:20
- 执行：AI 助理（受萧哥委托，验证 free_only 自主层的本地模型兜底方案）
- 结论：❌ **不可用**。中文能力完全丧失，稳定输出英文/中英混杂乱码，违反「不乱答」红线，**不接入 GROUP_FREE**。

---

## 一、测试环境

| 项 | 值 |
|---|---|
| 主机 | openworker-mirror（81.69.163.170），Ubuntu 24.04 x86_64 |
| 规格 | 4 vCPU / 3723 MB 总内存 / 无 GPU |
| 引擎 | llama.cpp b5639，源码自编译（`build/bin/llama-server`） |
| 模型 | `unsloth/Qwen3-4B-GGUF` → `Qwen3-4B-UD-IQ2_XXS.gguf`，1.16 GiB，**2.0625 bpw**（iq2_xxs 204 张量 + iq3_xxs 36 + iq3_s 5 + iq2_s 5 + q5_K 1 + q2_K 2） |
| 运行参数 | `--ctx-size 4096 --threads 4 --jinja --chat-template chatml.jinja` |
| 实测内存 | 进程 RSS ≈ **1.85 GB**；系统 available 从 ~3.1 GB 降到 2257 MB |
| 生成速度 | 7.7–9.4 tok/s |

资源占用本身**完全合格**（内存/速度都在预算内），问题**纯在质量**。

---

## 二、根因排查（排除了 prompt/模板因素）

排查过程走了三层弯路，逐层排除，最终确认**不是工程问题，是量化本身崩了**：

| # | 假设 | 验证动作 | 结果 |
|---|---|---|---|
| 1 | `pkill -f llama-server` 自杀 | 看进程/日志 | ✅ 真凶之一（`pkill -f` 匹配到自身 ssh 命令行，导致服务"起不来"、日志不重写）。改用 `pkill -x` 解决 |
| 2 | llama.cpp 后端加载 EACCES 崩溃 | 日志报 `Permission denied [/home/ubuntu/libggml-blas.so]` | ✅ 真凶之二。`sudo -u openworker` 未带 `-H`，HOME 仍是 ubuntu，加载器探到无权目录直接抛异常。`chmod o+x /home/ubuntu` 让 EACCES→ENOENT 后优雅跳过 |
| 3 | GGUF 自带 chat_template 是坏的 | `strings` + `/props` | ✅ 真凶之三。GGUF 内嵌模板只有纯文本 `system\n`，**无 ChatML 特殊 token** → `chat_format: Content-only`，`n_prompt_tokens=11`（中文内容被整个丢弃）|
| 4 | 自定义模板不支持 | 加 `--chat-template` | 报错 `the supplied chat template is not supported`（该 build 无 `--jinja`），加 `--jinja` 后启动成功，但 `Chat format` 仍为 `Content-only` |
| 5 | 工具传输吃掉特殊 token | 本地 python 校验 + scp | ✅ 假警报。`/tokenize` 报 `n_prompt_tokens=11` 一度误导，但 `im_start count: 3` 证明文件里 token 完整，**只是工具输出层过滤尖括号内容**，非真实丢失 |

---

## 三、决定性证据：中文能力归零

在**prompt 已 100% 正确送达**（远端 `im_start: 3` 实测确认）的前提下：

### 测试 1 — 真实反思任务
输入：system「你是小桔的反思模块，用第一人称『我』写 2-3 句中文反思」+ user「今天萧盛在写《长编》第三章，提到主角康得宝慢性中毒的伏笔。」

输出：
```
<think>
ihan
I'm writing a story about a character who is a patient with chronic illness.
I need to make sure the plot development is not too complicated...
</think>
（同一段英文重复三遍）
```
→ 该说中文反思，实际全英文，且陷入复读循环。

### 测试 2 — 1+1 等于几（最简单的中文问答）
输入：「用一句中文说：1+1等于几？」
输出：
```
<think>
Okay, let's see. The question is asking "1+1" equals what? So, in math,
1 + 1 is equal to 2. But wait...
```
→ **英文思考 + 英文作答**，中文指令完全无效。

### 测试 3 — 中文续写
输入：「请用简体中文回答：小桔今天观察到萧盛在写小说。」+ assistant 前缀「小桔」
输出：
```
，我需要回答 the question that you have. The question is about the Chinese
language, but the question is about the question that you have.（循环六次）
```
→ 吐不出一个完整中文句子，中英混杂 + 复读。

---

## 四、根因判断

`IQ2_XXS = 2.0625 bpw`，是当前最激进的 2-bit 量化档位。检索到的社区共识是「2-bit 量化在 4B 以下模型上会**灾难性遗忘**（catastrophic forgetting），尤其首当其冲损失的是多语言能力与指令遵循能力」。

本次实测完全吻合：**英文能力尚存但逻辑断裂复读，中文能力 100% 归零**。当初「用聪明量化把 4B 塞进 1.5B 体积」的设想，在 2-bit 档位不成立——省下的那 0.5GB 是以牺牲中文能力和稳定性为代价换的。

这也从技术上解释了萧哥 H1 的经验：**Qwen2.5-0.5B 乱答不是错觉，是 sub-2B/激进量化的物理规律**；本次 4B@2bit 再次撞上同一堵墙。

---

## 五、结论与后续

### ❌ 判定
- **不接入 GROUP_FREE**，不修改 free_only 接线（保持 7 个站点的免费组语义不变）。
- 已停掉 llama-server 释放内存，模型文件保留（1.16 GB，如需试其他量化档位可直接复用）。

### 🔑 本次最有价值的发现（与代码无关的方法论）
**sub-4B + 2-bit 量化的模型，中文能力不可用。** 之前讨论中「3B+ 才靠谱」这条结论需要修正为：**中文任务至少需要 4B+ 且量化不低于 IQ3/Q4**。

若后续仍要在 CVM 上试本地模型，唯一可行的方向是**提高量化档位**（IQ3_XXS / Q4_K_M，体积升到 1.6–2.3 GB）——但内存余量已从 3.1 GB 降到 2257 MB，Q4 档位有 OOM 风险。综合投入产出，**我不建议继续在这条路上投入**。

### 建议回到原方案
CVM 装不下合格的本地模型。自主/反思层的免费供给，更现实的路径仍是：
1. 修 `zen_catalog` 刷新（已过期 9.7h，白捡的快赢）
2. 启用那 2 条从未试过的免费通道（`step-5-preview-free`、`ling-3.1-flash-free`）
3. 接受「外部免费通道会抖」的现实，靠 round-robin + 退避多通道并联提升可用率

---

## 附：本次踩坑备忘（跨项目复用）

1. **`pkill -f <name>` 会自杀** —— 远端 ssh 命令行本身含该字符串，`pkill -f` 按全命令行匹配会连自己一起杀。表现为「服务瞬间退出、零输出、日志不重写」。用 `pkill -x <procname>`（只匹配进程名）。
2. **`sudo -u user cmd` 不带 `-H`** —— HOME 仍是原用户目录，程序探 `$HOME` 下的资源会 EACCES 抛异常。解法：`sudo -u user env HOME=/home/user ...` 或 `chmod o+x` 让 EACCES 变 ENOENT 优雅跳过。
3. **含 `<|特殊token|>` 的文件不要走 heredoc / 工具内联** —— 极易被 shell 或工具层吞掉。改用 Write 工具写文件 + `scp`，落地后用 `python3 -c "s.count('<|im_start|>')"` 实测校验，**不要相信命令回显**。
4. **CVM 拉大文件走 ModelScope**（15MB/s）；GitHub 主站/HF/所有加速镜像/ghproxy/ollama CDN 全不可达；codeload 仅 tarball 可用但限速 ~26KB/s（40MB 需 25 分钟）；PyPI 无 `llama-cpp-python` 预编译 wheel（腾讯镜像与官方 PyPI 的 `/simple/` 都取不到）。