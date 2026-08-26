// Maps the raw transcript from GET /v1/sessions/{id}/messages into the GUI's `Item[]` model.
// Extracted from App.tsx so it can be unit-tested without standing up the whole app.
//
// A connector-delivered user message carries a structured `source` sidecar (§3.1); when present it
// becomes a `connector` item (rendered as ConnectorMessageCard) instead of a plain user bubble. This
// generalizes to any connector via the registry — no Slack special-casing here.

import type { ConversationMessage } from "./api";
import type { Attachment, Item } from "./types";

export function itemsFromMessages(messages: ConversationMessage[]): Item[] {
  const items: Item[] = [];
  // Index tool results by tool_call_id so replayed tool rows can show their output
  // (the live view gets this from `tool_finished` events; on replay it's the `role:"tool"` msgs).
  const results: Record<string, string> = {};
  // `_display` sidecar on a tool message = user-facing metadata the agent never saw
  // (e.g. how many hits the privacy filters hid) — surfaces on the tool card.
  const hiddenCounts: Record<string, number> = {};
  for (const m of messages || []) {
    if (m.role === "tool" && m.tool_call_id) {
      results[m.tool_call_id] =
        typeof m.content === "string" ? m.content : JSON.stringify(m.content);
      const hidden = Number(m._display?.hidden_by_filters || 0);
      if (hidden > 0) hiddenCounts[m.tool_call_id] = hidden;
    }
  }
  for (const m of messages || []) {
    if (m.role === "user") {
      // Connector message → structured card; the framed `content` stays for the model, but display
      // renders from the source sidecar.
      if (m.source?.connector) {
        items.push({ kind: "connector", source: m.source });
        continue;
      }
      const user = userItemFromContent(m.content);
      // `attachments` (chat file chips) is a display-only sidecar — if the server put
      // one on the message, surface it as a chip. Engine strips it before the LLM call.
      if (Array.isArray(m.attachments)) {
        for (const a of m.attachments) {
          if (a && a.kind && a.name) {
            user.attachments = user.attachments || [];
            user.attachments.push({
              kind: a.kind === "image" || a.kind === "pdf" ? a.kind : "text",
              name: String(a.name),
              ...(a.text ? { text: String(a.text) } : {}),
            });
          }
        }
      }
      // `ts` (unix seconds) is the server's canonical-message stamp; older sessions have none.
      if (typeof m.ts === "number") user.ts = m.ts;
      if (user.text || user.attachments?.length) items.push(user);
    } else if (m.role === "assistant") {
      // A generated review-report card: render the rich ReportViewer instead of plain text.
      // The `report`/`report_name` sidecars are stripped before the LLM sees this message.
      // `report` 正常是嵌套对象；个别情况下可能是 JSON 字符串（旧存储/异常），这里兼容解析；
      // 解析失败则退化为普通助手文本，绝不抛错（否则 ErrorBoundary 会用红屏替换整个 App）。
      if (m.report && m.report_name) {
        let report: any = m.report;
        if (typeof report === "string") {
          try {
            report = JSON.parse(report);
          } catch {
            report = null;
          }
        }
        if (report && typeof report === "object") {
          items.push({
            kind: "report",
            name: String(m.report_name),
            report,
            ...(typeof m.content === "string" && m.content ? { caption: m.content } : {}),
            ...(typeof m.ts === "number" ? { ts: m.ts } : {}),
          });
          continue;
        }
      }
      if (m.content || m.reasoning)
        items.push({
          kind: "assistant",
          text: m.content || "",
          ...(typeof m.ts === "number" ? { ts: m.ts } : {}),
          ...(m.reasoning ? { reasoning: m.reasoning } : {}),
        });
      for (const tc of m.tool_calls || []) {
        let args: any = {};
        try {
          args = JSON.parse(tc.function?.arguments || "{}");
        } catch {
          args = {};
        }
        const preview = results[tc.id];
        const hidden = hiddenCounts[tc.id];
        items.push({
          kind: "tool",
          id: tc.id,
          name: tc.function?.name,
          args,
          status: "ok",
          preview,
          ...(hidden ? { hidden } : {}),
        });
      }
    } else if (m.role === "notice") {
      // Persisted markers (engine `_append_notice`): error/interrupted/model-switch survive
      // reload exactly like the live view rendered them. An error notice is retriable —
      // the Transcript only offers the button when it's the transcript tail.
      items.push(
        m.kind === "interrupted"
          ? { kind: "notice", tone: "warn", text: "Interrupted." }
          : m.kind === "model_switch"
            ? { kind: "notice", tone: "info", text: m.text || "Model switched" }
            : m.kind === "compacted"
              ? // The subtle "compacted here" divider (OPE-27) — the transcript itself is intact.
                { kind: "notice", tone: "info", text: m.text || "Context compacted" }
              : m.kind === "mcp_error"
                ? // A configured MCP server failed to start for this session — informational,
                  // NOT retriable (retry re-runs the model turn, which can't fix a dead server).
                  // Renders as one quiet line + disclosure. Legacy notices (persisted before
                  // the `server` field existed) recover the name from their own text, so old
                  // transcripts collapse too instead of keeping the wall of stderr.
                  mcpNoticeItem(m)
                : m.kind === "project_presence"
                  ? // Grant-time pointer: the granted folder already has memory/board —
                    // informational, one quiet line.
                    { kind: "notice", tone: "info", text: m.text || "" }
                  : m.kind === "reviewer_paused"
                    ? // §8.4 breaker: auto-approve paused itself for the rest of the turn.
                      { kind: "notice", tone: "info", text: m.text || "Auto-approve paused for the rest of this turn." }
                    : m.kind === "mode_notice"
                      ? // The once-per-session Auto-Approve explainer, in place forever.
                        { kind: "notice", tone: "info", title: (m as any).title || "Auto-approve is on.", text: m.text || "" }
                      : m.kind === "mode_switch"
                        ? { kind: "notice", tone: "info", text: m.text || "" }
                        : { kind: "notice", tone: "warn", text: "Error: " + (m.text || "unknown"), retriable: true },
      );
    }
    // system messages are omitted; tool-result messages are folded into the tool row above
  }
  return items;
}

function mcpNoticeItem(m: ConversationMessage): Item {
  const text = typeof m.text === "string" ? m.text : "";
  const server =
    (m.server && String(m.server)) || (text.match(/MCP server [“"]([^”"]+)[”"]/) || [])[1];
  if (!server)
    return { kind: "notice", tone: "warn", text: text || "An MCP server failed to start" };
  // The old format appended a plain-text Settings pointer — the button replaces it.
  const detail = text.replace(/\s*—\s*see Settings ▸ Connectors\s*$/u, "");
  return {
    kind: "notice",
    tone: "warn",
    text: `MCP server “${server}” didn’t start — its tools are unavailable here`,
    server,
    detail: detail || undefined,
  };
}

export function userItemFromContent(content: any): Extract<Item, { kind: "user" }> {
  if (typeof content === "string") return { kind: "user", text: content };
  if (!Array.isArray(content)) return { kind: "user", text: "" };

  const text: string[] = [];
  const attachments: Attachment[] = [];
  for (const part of content) {
    if (!part || typeof part !== "object") continue;
    if (part.type === "text" && part.text) {
      text.push(String(part.text));
    } else if (part.type === "image_url") {
      const url = part.image_url?.url;
      if (typeof url === "string" && url.startsWith("data:image/")) {
        attachments.push({ kind: "image", name: "image", data_url: url });
      }
    }
  }
  return { kind: "user", text: text.join("\n\n"), attachments };
}
