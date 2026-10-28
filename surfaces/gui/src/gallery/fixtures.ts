// 画廊的假数据：只描述**形状**，不复制任何真实内容。
//
// 规则：这里出现的每一条都必须是编的（人名、命令、路径都是占位）。画廊是个可以在内部
// 构建里随便点的地方，它不该成为第二个"看一眼就泄露聊天记录"的入口。
import type { InboxItem } from "../api";
import type { ApprovalEscalationInfo, Item } from "../types";

const T0 = 1_760_000_000;

/** 审批卡的各个形态：普通、三种升级原因、已被批、已被拒、已经失效、带常设规则/来源说明。 */
export const approvalStates: { label: string; item: Item }[] = [
  {
    label: "普通审批（写文件）",
    item: { kind: "approval", name: "write_file", args: { path: "草稿/第一章.md", content: "# 第一章\n\n……" }, reason: "" },
  },
  {
    label: "升级 · 审查者拿不准",
    item: {
      kind: "approval",
      name: "run_shell",
      args: { command: "python3 scripts/clean.py --all" },
      reason: "runs a local script that rewrites files",
      escalation: { kind: "reviewer_unsure", reason: "参数里有个 --all，看不出影响范围" },
      toolCallId: "call-fixture-1",
    } satisfies Item,
  },
  {
    label: "升级 · 策略要求人工",
    item: {
      kind: "approval",
      name: "send_message",
      args: { target: "slack:T000/C000", text: "……" },
      reason: "external send",
      escalation: { kind: "human_required", reason: "外发消息一律由人确认" } as ApprovalEscalationInfo,
    },
  },
  {
    label: "升级 · 审查者不可用",
    item: {
      kind: "approval",
      name: "run_shell",
      args: { command: "git push backup main" },
      reason: "",
      escalation: { kind: "reviewer_unavailable", reason: "模型调用超时" },
    },
  },
  {
    label: "旧字段 reviewerUnsure",
    item: { kind: "approval", name: "write_file", args: { path: "a.md", content: "x" }, reason: "", reviewerUnsure: "路径不在工作区里" },
  },
  { label: "已批准", item: { kind: "approval", name: "write_file", args: { path: "a.md", content: "x" }, reason: "", resolved: "once" } },
  { label: "已拒绝", item: { kind: "approval", name: "run_shell", args: { command: "rm -rf build/" }, reason: "", resolved: "deny" } },
  { label: "已失效（轮次结束没人回答）", item: { kind: "approval", name: "write_file", args: { path: "a.md", content: "x" }, reason: "", resolved: "expired" } },
  {
    label: "带来源说明 + 常设规则候选",
    item: {
      kind: "approval",
      name: "run_shell",
      args: { command: "./build.sh" },
      reason: "",
      provenance: "这个脚本是本次会话里刚由 agent 自己写出来的",
      standingTarget: "./build.sh",
    },
  },
  { label: "只读命令", item: { kind: "approval", name: "run_shell", args: { command: "ls -la" }, reason: "", readonlyOk: true } },
];

/** 一整个回合的样子：用户说话 → 工具步骤 → 门卡 → 回答。 */
export const transcriptSample: Item[] = [
  { kind: "user", text: "把这一章的错别字扫一遍，别动剧情。", ts: T0 },
  { kind: "assistant", text: "先看看这一章多长。", ts: T0 + 1 },
  { kind: "tool", id: "c1", name: "read_file", args: { path: "草稿/第一章.md" }, status: "ok", preview: "12,480 字" },
  { kind: "tool", id: "c2", name: "grep", args: { pattern: "的的" }, status: "ok", preview: "3 处" },
  { kind: "approval", name: "write_file", args: { path: "草稿/第一章.md", content: "……" }, reason: "", toolCallId: "c3" },
  {
    kind: "notice",
    tone: "info",
    text: "上下文已压缩，早先的细节收进了摘要。",
  },
  { kind: "memory", id: 1, text: "萧盛偏好：改稿只改错字，不动叙事结构。" },
  { kind: "assistant", text: "扫完了：3 处「的的」，2 处「在再」混用。改法是逐条替换，剧情一个字没动。", ts: T0 + 30 },
];

/** 其余各种卡片：问题、目录、计划、工具安装、组队、工作项、灵感、意图讨论。 */
export const otherStates: { label: string; item: Item }[] = [
  {
    label: "提问（选项 + 自由输入）",
    item: {
      kind: "question",
      question: "这一章要先按时间线顺一遍，还是先查人物称谓？",
      options: ["先顺时间线", "先查称谓", "两个都做"],
      allow_text: true,
    },
  },
  { label: "目录授权", item: { kind: "dirreq", reason: "需要读这一章的参考资料", path: "~/Documents/史料", writable: false, primary: true } },
  { label: "计划审批", item: { kind: "planreq", plan: "1. 通读全章\n2. 标记错字\n3. 逐条替换并复核" } },
  { label: "工具安装", item: { kind: "toolreq", tool: "pandoc", reason: "转 docx 需要它", installable: true, source: "brew" } },
  {
    label: "组队闸门",
    item: {
      kind: "teamreq",
      members: [
        { persona: "historian", name: "考据", model: "anthropic:claude-fable-5", reason: "查称谓与年代" },
        { persona: "lit-editor", name: "校订", model: "openai:gpt-5", reason: "错字与标点" },
      ],
      enable_chat: true,
      note: "两条线可以并行，最后我合。",
    },
  },
  {
    label: "工作项分解",
    item: {
      kind: "itemsreq",
      items: [
        { title: "顺时间线", criteria: "章内时间锚点全部对齐", description: "只看时间词，不改句" },
        { title: "核对称谓", criteria: "人名官称前后一致" },
      ],
      note: "拆成两条，各自可验收。",
    },
  },
  { label: "创作灵感", item: { kind: "inspire", text: "康得宝的慢性中毒可以再埋一层：药味被茶香盖住。", refs: ["第六章 · 萤窗案"] } },
  {
    label: "意图讨论",
    item: {
      kind: "intent_discuss",
      text: "你想让我按「先查史料再动笔」的节奏来，对吗？",
      intent_id: "i-1",
      round: 1,
      phase: "draft",
    },
  },
];

/** 信箱里的那几张（后台/无人值守时停在这里）。 */
export const inboxStates: { label: string; item: InboxItem }[] = [
  {
    label: "信箱 · 审批（带升级原因）",
    item: {
      id: "inbox-1",
      tool_call_id: "call-fixture-1",
      session_id: "s1",
      kind: "approval",
      title: "写文件",
      body: "",
      state: "pending",
      resolution: null,
      inbox: "inbox",
      created_at: "2026-10-28T09:00:00Z",
      resolved_at: null,
      data: {
        tool: "write_file",
        arguments: { path: "草稿/第一章.md", content: "……" },
        escalation: { kind: "reviewer_unsure", reason: "改动涉及正文段落" },
      },
    },
  },
  {
    label: "信箱 · 提问",
    item: {
      id: "inbox-2",
      session_id: "s1",
      kind: "question",
      title: "要按哪个版本改？",
      body: "两个版本差异在第三章的结尾。",
      state: "pending",
      resolution: null,
      inbox: "inbox",
      created_at: "2026-10-28T09:05:00Z",
      resolved_at: null,
      options: [{ label: "旧版" }, { label: "新版" }],
      allow_text: true,
    },
  },
];
