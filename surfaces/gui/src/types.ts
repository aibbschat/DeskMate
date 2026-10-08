export type EventType =
  | "ready"
  | "inbound"
  | "turn_start"
  | "assistant_delta"
  | "reasoning_delta"
  | "assistant_message"
  | "tool_proposed"
  | "permission_required"
  | "directory_requested"
  | "question_requested"
  | "plan_proposed"
  | "tool_started"
  | "tool_finished"
  | "iteration_end"
  | "turn_end"
  | "error"
  | "input_rejected"
  | "interrupted"
  | "model_changed"
  | "turn_done"
  // DeskMate extension events (dispatched by DeskMate MCP server)
  | "deskmate_suggestion"
  | "deskmate_memory_update"
  | "deskmate_memory_learned"
  | "deskmate_context_injected"
  | "compacting"
  | "compacted";

export interface WsEvent {
  type: EventType;
  data: any; // eslint-disable-line @typescript-eslint/no-explicit-any -- WS event payloads vary by type; narrowing in App.tsx's switch
}

// Re-exported for transcript items below. Lives in api.ts (the REST/WS contract source of truth);
// type-only import, so there's no runtime cycle with api.ts's `import type { ... } from "./types"`.
import type { MessageSource } from "./api";

// "always_task" persists to the owning automation's task record (standing scoped
// approval, UX-DECISIONS §25) — offered only on automation-run approval cards, in-app.
export type ApprovalDecision =
  | "once"
  | "deny"
  | "always_tool"
  | "always_command"
  | "always_domain"
  | "always_task"
  | "readonly_session";

export interface TodoItem {
  content: string;
  status: "pending" | "in_progress" | "done";
}

// Per-round-trip token counts, as attached by the server to assistant messages and
// the assistant_message event (`{model, input, output, cache_read, cache_write}`).
// Absent on older servers and on backends that don't report usage.
export interface TurnUsage {
  model?: string | null;
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
}

// Per-session accumulation, keyed by model id (multiple models when the user
// switched mid-session). `context` = the latest round-trip's prompt-side total —
// what currently occupies the active model's context window.
export interface SessionUsage {
  byModel: Record<string, TurnUsage>;
  context: number;
}

export interface SessionInfo {
  session_id: string;
  title?: string;
  workspace: string;
  agent: string;
  model: string;
  mode: string;
  updated_at: string | null;
  messages: number;
  pinned?: boolean;
  archived?: boolean;
  // Inbox items awaiting this session (the amber attention count that bubbles up the sidebar).
  attention?: number;
  // working = in-flight turn; sleeping = a self-wake is pending; idle = neither. A count-less dot.
  liveness?: "working" | "sleeping" | "idle";
  // Channels this session listens to (inbound subscriptions).
  subscriptions?: string[];
  // §31: set when the session was spawned by a platform mention rather than the user —
  // machine key ("slack") + display label ("#general · T0ABCD"). Drives the sidebar's
  // "From Slack" group and the row's platform icon.
  origin?: string;
  origin_label?: string;
}

// Attachments (images, PDFs, text files) sent with a user message.
export interface Attachment {
  kind: "image" | "text" | "pdf";
  name: string;
  mime?: string;
  data_url?: string; // images + PDFs
  text?: string; // text files
}

// Transcript items
// `ts` = unix seconds (the server's canonical-message stamp; live items stamp locally).
// Optional: sessions saved before the server stamped timestamps have none.
export type Item =
  | { kind: "user"; text: string; attachments?: Attachment[]; ts?: number }
  // A connector-delivered inbound message (Slack/Salesforce/…), rendered as a structured card
  // (ConnectorMessageCard) instead of a plain user bubble. Generalizes to any connector via the
  // registry — no per-connector special-casing.
  | { kind: "connector"; source: MessageSource }
  | { kind: "assistant"; text: string; ts?: number; reasoning?: string }
  // A generated review-report card (编辑专家「生成审阅报告」产物），内嵌在会话里，
  // 重开软件后仍在。引擎发 LLM 前会剥离 `report`/`report_name` sidecar 字段。
  | { kind: "report"; name: string; report: any; caption?: string; ts?: number } // eslint-disable-line @typescript-eslint/no-explicit-any -- typed as ReviewReport at the consumer (ReportViewer)
  // `hidden` = results the user's privacy filters removed before the agent saw them
  // (from the tool message's `_display` sidecar; the agent-visible content has no trace).
  // `standingRule` = the task-scoped rule that auto-allowed this call ("tool → target").
  | { kind: "tool"; id: string; name: string; args: any; status: string; preview?: string; hidden?: number; standingRule?: string; reviewerReason?: string; allowAnyway?: boolean; approvalOrigin?: string; approvalNote?: string; approvalGrant?: string } // eslint-disable-line @typescript-eslint/no-explicit-any -- args are tool-specific and consumed with runtime checks
  | {
      kind: "approval";
      name: string;
      args: any; // eslint-disable-line @typescript-eslint/no-explicit-any -- args are tool-specific and consumed with runtime checks
      reason: string;
      category?: string;
      // The exact target a standing rule could pin (server-computed) — with a run
      // context, the card offers "Allow every time" (§25).
      standingTarget?: string;
      // web_search only: the LIVE configured provider name, resolved server-side.
      searchProvider?: string;
      // set when the action would run a file the agent itself created/downloaded this session.
      provenance?: string;
      // the Auto-Approve reviewer answered `unsure` and raised this card.
      reviewerUnsure?: string;
      // server-classified: this shell command only reads locally.
      readonlyOk?: boolean;
      resolved?: ApprovalDecision;
    }
  | {
      kind: "dirreq";
      reason: string;
      path?: string;
      writable?: boolean;
      primary?: boolean;
      resolved?: "granted" | "denied";
    }
  | {
      kind: "planreq";
      plan: string;
      resolved?: "approved" | "rejected";
    }
  | {
      // The staffing gate (agent teams): a lead proposes its worker roster.
      kind: "teamreq";
      members: { persona: string; name?: string; model?: string; reason?: string }[];
      enable_chat?: boolean;
      note?: string;
      resolved?: "approved" | "rejected";
    }
  | {
      // A live ask_user prompt (attended sessions answer inline; unattended ones route to the Inbox).
      kind: "question";
      question: string;
      options?: string[];
      allow_text?: boolean;
      multi?: boolean;
      resolved?: string;
    }
  | { kind: "notice"; tone: "info" | "warn"; text: string; retriable?: boolean; title?: string; server?: string; detail?: string }
  // 工具安装请求（request_tool）：agent 找不到某 CLI 时向用户申请安装；declining 是正常结果。
  | {
      kind: "toolreq";
      tool: string;
      reason: string;
      installable?: boolean;
      version?: string;
      summary?: string;
      source?: string;
      resolved?: "installed" | "skipped";
    }
  // 任务分解闸门（agent teams）：lead 提议工作项；approval 后在 board 上创建。
  | {
      kind: "itemsreq";
      items: { title: string; criteria: string; description?: string }[];
      note?: string;
      resolved?: "approved" | "rejected";
    }
  // 记忆保存提示（MEMORY-SPEC §5.1）：内联在对话里，可 Undo。
  | { kind: "memory"; id: number; text: string; previous?: string; undone?: boolean }
  // 创作灵感卡片：普通聊天里聊写作时，小桔基于认知库主动递（不持久化进会话历史）。
  | { kind: "inspire"; text: string; refs?: string[]; ts?: number }
  // R13 意图讨论态消息：小桔产出的 draft 递到聊天，附「定稿/再改/不要」三按钮
  // （opening 阶段只显示文字不带按钮）。来自 WS assistant_message.meta.origin === "intent_handoff"。
  | {
      kind: "intent_discuss";
      text: string;
      intent_id: string;
      round: number;
      phase: "opening" | "draft";
      resolved?: "finalized" | "cancelled" | "revising";
      ts?: number;
    };

export interface GroupedQuestion {
  question: string;
  header?: string;
  options?: QuestionOption[];
  allow_text?: boolean;
  multi?: boolean;
}

export type QuestionOption =
  | string
  | { label: string; description?: string; recommended?: boolean; preview?: string };
