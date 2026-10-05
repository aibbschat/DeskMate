/**
 * 手机端对话事件 reducer（纯函数，可单测）。
 *
 * 为什么单独成文件：桌面端把这套 switch 内联在 App.tsx 的 socket 回调里（App.tsx:702-949），
 * 依赖十几个 ref。手机端只需要其中与「渲染一条会话」有关的部分，且必须能脱离 React 测。
 *
 * 三条硬规则（设计稿 §三.6）：
 *  1. 以 `turn_done` 为「她这轮真结束」的权威信号，不是 `turn_end`（后者只带状态）。
 *  2. 未识别事件一律忽略、绝不抛错——服务端实际还会发 `tool_requested` / `team_proposed`
 *     / `items_proposed`，它们**不在 types.ts 的 EventType 联合里**，遇到必须活下来。
 *  3. 流式文本只进 `streaming`，落成气泡要等 `assistant_message`（提前 promote 会让每句
 *     旁白闪成一条完整回复，桌面 07-13 已踩过）。
 */
import type { Item, WsEvent } from "../types";

export interface ChatState {
  items: Item[];
  /** 本轮未定稿的流式文本 */
  streaming: string;
  /** 本轮未定稿的思考流（reasoning_delta 累加） */
  reasoning: string;
  running: boolean;
  connected: boolean;
  /** 服务端 ready 里报的当前模型（手机端只读展示，不提供切换） */
  model: string;
  /** 本轮工具调用计数（给「她在用工具」状态条；turn_done 清零） */
  toolCount: number;
  /** 断线码（1008=凭据，其它=网络）；null 表示没故障 */
  fault: number | null;
}

export const emptyChat = (): ChatState => ({
  items: [],
  streaming: "",
  reasoning: "",
  running: false,
  connected: false,
  model: "",
  toolCount: 0,
  fault: null,
});

let seq = 0;
const uid = () => `m${Date.now().toString(36)}${(seq++).toString(36)}`;

const notice = (tone: "info" | "warn", text: string, retriable = false): Item =>
  tone === "warn"
    ? { kind: "notice", tone, text, ...(retriable ? { retriable: true } : {}) }
    : { kind: "notice", tone, text };

/** 把最后一条同名、且还没结束的工具行标成完成态（对齐桌面 updateLastTool 的行为）。 */
function finishTool(items: Item[], name: string, status: string, preview?: string, hidden?: number): Item[] {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "tool" && it.name === name && it.status !== "ok" && it.status !== "error") {
      const next = [...items];
      next[i] = {
        ...it,
        status: status || "ok",
        ...(preview ? { preview } : {}),
        ...(hidden ? { hidden } : {}),
      };
      return next;
    }
  }
  // 没找到对应行（例如断线重连后只收到 finished）→ 补一条已完结行，绝不丢信息。
  return [...items, { kind: "tool", id: uid(), name, args: {}, status: status || "ok", ...(preview ? { preview } : {}) }];
}

/**
 * 应用一条 WS 事件。返回新状态（不修改入参）。
 * `now` 可注入以便测试里断言 ts。
 */
export function applyEvent(s: ChatState, ev: WsEvent, now: number = Date.now() / 1000): ChatState {
  const d: any = (ev && (ev.data as any)) || {};
  switch (ev?.type) {
    case "ready":
      return { ...s, connected: true, fault: null, model: d.model || s.model };

    case "turn_start": {
      let items = s.items;
      // 后台投递的轮次（频道消息 / 自唤醒 / 重启续跑）没有本地 send()，触发消息不在 items 里 → 补上。
      // 前台轮次已在 send() 时追加过，按文本去重防双份。
      if (d.source?.connector) {
        const last = items[items.length - 1];
        if (!(last && last.kind === "connector" && last.source?.ts === d.source.ts)) {
          items = [...items, { kind: "connector", source: d.source }];
        }
      } else if (typeof d.input === "string" && d.input) {
        const last = items[items.length - 1];
        if (!(last && last.kind === "user" && last.text === d.input)) {
          items = [...items, { kind: "user", text: d.input, ts: now }];
        }
      }
      return { ...s, items, running: true, streaming: "", reasoning: "", toolCount: 0 };
    }

    case "assistant_delta":
      return { ...s, streaming: s.streaming + (d.text || "") };

    case "reasoning_delta":
      return { ...s, reasoning: s.reasoning + (d.text || "") };

    case "assistant_message": {
      const reasoning = d.reasoning || s.reasoning;
      const items =
        d.report && d.report_name
          ? [
              ...s.items,
              {
                kind: "report" as const,
                name: String(d.report_name),
                report: typeof d.report === "string" ? safeParse(d.report) : d.report,
                ...(d.text ? { caption: String(d.text) } : {}),
                ts: now,
              } as Item,
            ]
          : d.text || reasoning
            ? [...s.items, { kind: "assistant" as const, text: d.text || "", ts: now, ...(reasoning ? { reasoning } : {}) } as Item]
            : s.items;
      return { ...s, items, streaming: "", reasoning: "" };
    }

    case "tool_proposed":
      return {
        ...s,
        toolCount: s.toolCount + 1,
        items: [...s.items, { kind: "tool", id: uid(), name: d.name, args: d.arguments || {}, status: "…" }],
      };

    case "tool_started":
      return s; // 桌面用它驱动桌宠气泡；手机端不渲染中间态

    case "tool_finished":
      return {
        ...s,
        items: finishTool(
          s.items,
          d.name,
          d.status,
          d.result_preview || d.reason,
          Number(d.display?.hidden_by_filters || 0) || undefined,
        ),
      };

    case "permission_required":
      return {
        ...s,
        items: [
          ...s.items,
          {
            kind: "approval",
            name: d.name,
            args: d.arguments || {},
            reason: d.reason || "",
            category: d.category,
            standingTarget: d.standing_target || undefined,
          },
        ],
      };

    case "directory_requested":
      return { ...s, items: [...s.items, { kind: "dirreq", reason: d.reason || "", path: d.path || "", writable: !!d.writable }] };

    case "plan_proposed":
      return { ...s, items: [...s.items, { kind: "planreq", plan: d.plan || "" }] };

    case "question_requested":
      return {
        ...s,
        items: [
          ...s.items,
          { kind: "question", question: d.question || "", options: d.options || [], allow_text: d.allow_text !== false, multi: !!d.multi },
        ],
      };

    case "turn_end":
      return d.status === "max_iterations_exceeded"
        ? { ...s, items: [...s.items, notice("warn", "已停止：达到最大迭代次数")] }
        : s;

    case "turn_done":
      // 权威收尾：清 running、清本轮流式残留。
      return { ...s, running: false, streaming: "", reasoning: "", toolCount: 0 };

    case "model_changed":
      return { ...s, model: d.model || s.model, items: [...s.items, notice("info", d.text || "模型已切换")] };

    case "compacting":
      return { ...s, items: [...s.items, notice("info", "正在压缩上下文…")] };

    case "compacted":
      return { ...s, items: [...s.items, notice("info", d.text || "上下文已压缩")] };

    case "interrupted":
      return { ...s, running: false, items: flushPartial(s, notice("warn", "已中断")) };

    case "error":
      return { ...s, running: false, items: flushPartial(s, notice("warn", "出错：" + (d.error || "未知"), true)) };

    case "input_rejected":
      return { ...s, items: [...s.items, notice("warn", d.error || "这条消息被拒绝了")] };

    // 未识别事件（含 tool_requested / team_proposed / items_proposed / deskmate_* / inbound /
    // iteration_end / ping 回包）一律忽略 —— 手机端绝不能因为服务端多吐一种事件就崩。
    default:
      return s;
  }
}

/** 断线/出错时把已流出的半截文本落成一条气泡，别让它凭空消失。 */
function flushPartial(s: ChatState, tail: Item): Item[] {
  const items = s.streaming || s.reasoning
    ? [...s.items, { kind: "assistant" as const, text: s.streaming, ts: Date.now() / 1000 } as Item]
    : s.items;
  return [...items, tail];
}

function safeParse(text: string): any {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** 发送一条消息：本地先上屏（乐观渲染），socket 的排队/补发交给 Session 自己（api.ts）。 */
export function withLocalUser(s: ChatState, text: string, now: number = Date.now() / 1000): ChatState {
  const t = text.trim();
  if (!t) return s;
  return { ...s, items: [...s.items, { kind: "user", text: t, ts: now }] };
}

/**
 * 把最后一条未处理的某类交互标记成已回答（桌面 App.tsx:2094-2136 同语义，这里做成纯函数好测）。
 * Transcript 只在 `resolved` 存在时才把审批配对到工具行上，所以答完必须回写，否则卡片会一直挂着。
 */
export function resolveLast(items: Item[], kind: "approval", value: string): Item[];
export function resolveLast(items: Item[], kind: "dirreq" | "planreq", value: "granted" | "denied" | "approved" | "rejected"): Item[];
export function resolveLast(items: Item[], kind: "question", value: string): Item[];
export function resolveLast(items: Item[], kind: Item["kind"], value: string): Item[] {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === kind && !(it as any).resolved) {
      const next = [...items];
      // 只有 approval/dirreq/planreq/question 四类会走到这里（调用方按 kind 限定），
      // 它们都带 resolved 字段；联合类型本身看不出来，故此处显式收敛一次。
      next[i] = { ...(it as any), resolved: value } as Item;
      return next;
    }
  }
  return items;
}

/** 当前挂在输入区上方的那条待答交互（审批 / 目录授权 / 计划 / 提问）。 */
export function pendingInteractive(items: Item[]): Item | null {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if ((it.kind === "approval" || it.kind === "dirreq" || it.kind === "planreq" || it.kind === "question") && !(it as any).resolved)
      return it;
  }
  return null;
}
