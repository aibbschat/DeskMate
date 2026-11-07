import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApprovalDecision, Item } from "../types";
import { shortArgs } from "./ApprovalCard";
import { humanizeAsk, humanizeTool, type HumanLine } from "../humanize";
import { Markdown, stripEmote } from "./Markdown";
import { renderEmoteInline } from "./deskmate/emotes/Emote";
import { ConnectorMessageCard } from "./ConnectorMessageCard";
import { BoardWakeCard } from "./BoardWakeCard";
import { ReportViewer } from "./deskmate/ReportViewer";
import { Icon } from "./Icon";
import { type Expert } from "./deskmate/experts";
import XiaojuAvatar from "./XiaojuAvatar";
import { VoiceBubble } from "./VoiceBubble";
import { useTurnStyle } from "../turnStyle";

// Long user pastes swallow the transcript (owner ask 2026-07-30): clamp past a generous
// threshold with a more…/less… toggle. Normal typed messages never see the control; the
// full text still drives copy (BubbleMeta) and is what the model received.
const USER_CLAMP_CHARS = 1200;

// 若硬切点在表情 token 中间（如切到 `![招手问好](emote:wa`），裸协议文本会漏出来。
// 这个正则匹配「尾巴上未闭合的表情 token 起点」，clamp 时回退到该起点，保证表情整取或整弃。
const EMOTE_PARTIAL_RE =
  /(?:\[\[emote:[a-zA-Z0-9_-]*|!\[[^\]]*\]\(emote:[a-zA-Z0-9_-]*|!\[[^\]]*\(emote:[a-zA-Z0-9_-]*|!\[[^\]]*)$/;

function clampUserText(text: string): string {
  if (text.length <= USER_CLAMP_CHARS) return text;
  let head = text.slice(0, USER_CLAMP_CHARS);
  const pm = head.match(EMOTE_PARTIAL_RE);
  if (pm && pm.index !== undefined) head = head.slice(0, pm.index);
  return head.trimEnd() + "…";
}

function ClampedUserText({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  if (text.length <= USER_CLAMP_CHARS) return <>{renderEmoteInline(text)}</>;
  return (
    <>
      {open ? renderEmoteInline(text) : renderEmoteInline(clampUserText(text))}
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="block ml-auto mt-1.5 text-[12.5px] font-medium opacity-75 hover:opacity-100"
      >
        {open ? "less…" : "more…"}
      </button>
    </>
  );
}

// Hover affordances for a message bubble (FB-005): copy the raw text + the message's time.
// Lives in a ZERO-HEIGHT strip under the bubble (absolute, inside the transcript's 20px gap)
// so revealing it on group-hover never shifts the layout. `ts` is unix seconds — canonical
// messages carry it, pre-stamp history doesn't, so the time simply omits itself when absent.
function BubbleMeta({
  text,
  ts,
  align,
  onRemember,
}: {
  text: string;
  ts?: number;
  align: "left" | "right";
  onRemember?: () => void;
}) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const when = typeof ts === "number" ? new Date(ts * 1000) : null;
  const copy = () => {
    // "Copied" only after the write actually lands — WebKit can reject outside a
    // trusted gesture, and claiming success on a silent no-op would gaslight the user.
    // Strip the [[emote:NAME]] protocol markers so the copied plain text stays clean.
    navigator.clipboard
      ?.writeText(stripEmote(text))
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      })
      .catch(() => {});
  };
  return (
    <div className="relative h-0 select-none">
      <div
        className={
          "bubble-meta absolute top-1 flex items-center gap-1.5 text-[10.5px] leading-none text-faint whitespace-nowrap opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition-opacity " +
          (align === "right" ? "right-0" : "left-0")
        }
      >
        <button
          className="flex items-center cursor-pointer hover:text-muted"
          data-testid="bubble-copy"
          title={t("chat.copyTitle")}
          onClick={copy}
        >
          {copied ? t("chat.copied") : <Icon name="copy" size={11} />}
        </button>
        {onRemember && (
          <button
            className="flex items-center cursor-pointer hover:text-muted"
            data-testid="bubble-remember"
            title="记住这个（写入主动台账，琢磨完开聊时递回）"
            onClick={onRemember}
          >
            记住这个
          </button>
        )}
        {when && (
          <span data-testid="bubble-ts" title={when.toLocaleString()}>
            {when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}
          </span>
        )}
      </div>
    </div>
  );
}

// Reasoning-model thinking text (model-layer roadmap item 4): a quiet disclosure —
// collapsed by default, the trace one click away. `live` = still streaming (pulsing label);
// App renders that variant above the transcript, this one rides a finalized assistant item.
export function ThinkingBlock({ text, live }: { text: string; live?: boolean }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <div className="thinking">
      <button
        className="thinking-head"
        onClick={() => setOpen((v) => !v)}
        data-testid="thinking-toggle"
      >
        <Icon name="chevronDown" size={12} className={"thinking-caret" + (open ? " open" : "")} />
        <span className={live ? "thinking-live" : undefined}>
          {live ? t("chat.thinking") : t("chat.thoughtProcess")}
        </span>
      </button>
      {open && (
        <div className="thinking-body" data-testid="thinking-body">
          {text}
        </div>
      )}
    </div>
  );
}

type ToolItem = Extract<Item, { kind: "tool" }>;
type ApprovalItem = Extract<Item, { kind: "approval" }>;
type AssistantItem = Extract<Item, { kind: "assistant" }>;
type TurnItem = ToolItem | ApprovalItem | AssistantItem;

// TurnGroup (§33, absorbs §7's StepGroup): the whole user-message → final-answer span collapses
// as ONE disclosure — "N steps" — with the agent's narration (assistant text followed by more
// activity in the same turn) and humanized one-line steps interleaved inside. The final assistant
// text renders as a normal bubble OUTSIDE the group (see the flush logic in Transcript below).
// Approvals fold into their tool's row as a chip; an approval with no executed call (typically
// declined) keeps its own "Wanted to …" row. Raw args+result stay one click away per row.

type TurnRow =
  | { type: "narr"; text: string }
  | { type: "step"; tool: ToolItem; approval?: ApprovalItem }
  | { type: "ask"; approval: ApprovalItem };

function buildRows(items: TurnItem[]): TurnRow[] {
  // First pass: tool rows in order; then pair each resolved approval with the nearest
  // same-name tool that doesn't have one yet (approvals may stream before or after their call).
  const rows: TurnRow[] = items
    .filter((it): it is ToolItem | AssistantItem => it.kind !== "approval")
    // Thinking-only assistant items (no text) carry nothing narratable — skip the row.
    .filter((it) => it.kind !== "assistant" || it.text)
    .map((it) =>
      it.kind === "assistant" ? { type: "narr" as const, text: it.text } : { type: "step" as const, tool: it },
    );
  const approvals = items.filter((it): it is ApprovalItem => it.kind === "approval");
  for (const ap of approvals) {
    const at = items.indexOf(ap);
    let bestRow: Extract<TurnRow, { type: "step" }> | null = null;
    let bestDist = Infinity;
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      if (it.kind !== "tool" || it.name !== ap.name) continue;
      const row = rows.find((r) => r.type === "step" && r.tool === it) as
        | Extract<TurnRow, { type: "step" }>
        | undefined;
      if (!row || row.approval) continue;
      const dist = Math.abs(i - at);
      if (dist < bestDist) {
        bestRow = row;
        bestDist = dist;
      }
    }
    if (bestRow && ap.resolved !== "deny") bestRow.approval = ap;
    else {
      // No executed call to attach to (or it was declined) — the ask keeps its own row,
      // placed where the approval sat in the stream.
      const after = items.slice(0, at).filter((it) => it.kind !== "approval").length;
      rows.splice(after, 0, { type: "ask", approval: ap });
    }
  }
  return rows;
}

function approvalChip(resolved: ApprovalDecision | undefined, t: (k: string, opts?: Record<string, unknown>) => string) {
  if (resolved === "expired")
    return (
      <span className="text-[10.5px] px-1.5 rounded-full bg-paper text-faint shrink-0" title={t("chat.expiredTitle")}>
        {t("chat.expired")}
      </span>
    );
  if (resolved === "deny")
    return <span className="text-[10.5px] px-1.5 rounded-full bg-dangerSoft text-danger shrink-0">{t("chat.declined")}</span>;
  return (
    <span
      className="text-[10.5px] px-1.5 rounded-full bg-okSoft text-ok shrink-0"
      title={resolved ? t("chat.approvedTitle", { rule: resolved.replace(/_/g, " ") }) : t("chat.approved")}
    >
      {t("chat.approved")}
    </span>
  );
}

function LineText({ line }: { line: HumanLine }) {
  return (
    <span className="min-w-0 text-ui leading-relaxed">
      <span className="text-muted">{line.pre}</span>
      {line.obj && <span className="text-ink">{line.obj}</span>}
      {line.post && <span className="text-muted">{line.post}</span>}
    </span>
  );
}

function StepRow({
  tool,
  approval,
  onAllowAnyway,
}: {
  tool: ToolItem;
  approval?: ApprovalItem;
  onAllowAnyway?: (name: string, args: any) => void;
}) {
  const { t } = useTranslation();
  const [raw, setRaw] = useState(false);
  // §8.4 "Allow anyway": one-shot exact-action override on a reviewer-denied tool.
  const [overrideSent, setOverrideSent] = useState(false);
  const running = tool.status === "…";
  const failed = tool.status !== "ok" && !running;
  return (
    <div>
      <div className="group flex items-baseline gap-2 px-2 py-0.5 rounded-lg hover:bg-paper" data-testid="turn-step">
        <span className={"w-3.5 text-center text-[10px] shrink-0 " + (failed ? "text-danger" : running ? "text-accent" : "text-ok")}>
          {running ? <span className="spinner" data-testid="step-running" /> : "●"}
        </span>
        <LineText line={humanizeTool(tool.name, tool.args)} />
        {approval && approvalChip(approval.resolved, t)}
        {!!tool.standingRule && (
          <span
            className="text-[10.5px] px-1.5 rounded-full bg-tealSoft text-tealInk shrink-0"
            data-testid="tool-standing-rule"
            title={t("chat.autoAllowedTitle", { rule: tool.standingRule })}
          >
            {t("chat.autoAllowed")}
          </span>
        )}
        {!!tool.hidden && (
          <span
            className="text-label text-warnInk shrink-0"
            data-testid="tool-hidden-count"
            title={t("chat.hiddenTitle")}
          >
            {t("chat.hiddenCount", { count: tool.hidden })}
          </span>
        )}
        {failed && <span className="text-label text-danger shrink-0">{tool.status}</span>}
        {!running && (
          <button
            className="ml-auto shrink-0 text-label text-faint opacity-0 group-hover:opacity-100 cursor-pointer"
            onClick={() => setRaw((v) => !v)}
          >
            {t("chat.raw")}
          </button>
        )}
      </div>
      {raw && (
        <pre className="ml-8 mr-2 my-1 px-2.5 py-1.5 rounded-lg border border-line bg-paper font-mono text-[11.5px] leading-relaxed text-muted whitespace-pre-wrap break-words max-h-56 overflow-auto">
          {`${tool.name}  ${shortArgs(tool.args)}`}
          {tool.preview ? `\n→ ${tool.preview.length > 1500 ? tool.preview.slice(0, 1500) + "\n…" : tool.preview}` : ""}
        </pre>
      )}
      {tool.status === "denied" && tool.reviewerReason && (
        <div
          className="ml-8 mr-2 my-1 px-3 py-2 rounded-lg border border-line bg-dangerSoft/40"
          data-testid="reviewer-deny-card"
        >
          <div className="text-label font-medium text-danger">Blocked by the reviewer</div>
          <div className="text-meta text-ink mt-0.5">{tool.reviewerReason}</div>
          <div className="text-label text-faint mt-1">
            The agent was told only that it was blocked — not why — so it can&rsquo;t argue
            its way past this. If the action is actually fine, you can run it as proposed:
          </div>
          {tool.allowAnyway && onAllowAnyway && !overrideSent && (
            <button
              className="mt-1.5 px-2.5 py-1 rounded-lg border border-line bg-panel text-meta text-ink hover:bg-paper"
              data-testid="reviewer-allow-anyway"
              onClick={() => {
                setOverrideSent(true);
                onAllowAnyway(tool.name, tool.args);
              }}
            >
              Allow anyway
            </button>
          )}
          {overrideSent && (
            <div className="mt-1.5 text-label text-ok" data-testid="reviewer-override-sent">
              Approved — the agent will retry this exact action.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function TurnGroup({
  items,
  live,
  streamingText,
  onAllowAnyway,
}: {
  items: TurnItem[];
  live?: boolean;
  // Sub-threshold streamed text belongs to THIS group (§33 ref #3): collapsed → it rides
  // the header as the live line; expanded → the small quiet line under the steps.
  streamingText?: string;
  // §8.4 "Allow anyway" on a reviewer-denied tool: one-shot exact-action override.
  onAllowAnyway?: (name: string, args: any) => void;
}) {
  const { t } = useTranslation();
  // 折叠卡片（cards）＝「动态可见→收敛」：运行中自动展开让每步思考实时可看，
  // 跑完自动收起只留「N 步」+答案（萧盛 2026-11-03：旧版运行中也强制收起＝每步思考不可见，
  // 是"没治好"的根）。用户手动开合（userToggle）始终优先于自动态。
  const rows = buildRows(items);
  const tools = items.filter((it): it is ToolItem => it.kind === "tool");
  const running = live || tools.some((t) => t.status === "…");
  const [userToggle, setUserToggle] = useState<boolean | null>(null);
  const open = userToggle ?? running;
  const [style] = useTurnStyle();
  const [bubbleOpen, setBubbleOpen] = useState(false);
  const lastNarr = [...items].reverse().find((it): it is AssistantItem => it.kind === "assistant");
  const liveLine = streamingText || lastNarr?.text || "";

  const nSteps = rows.filter((r) => r.type !== "narr").length;
  const declined = items.filter((it) => it.kind === "approval" && it.resolved === "deny").length;
  const hiddenTotal = tools.reduce((n, t) => n + (t.hidden || 0), 0);
  const stepsLabel = t("chat.steps", { nSteps });

  // 显示风格分支（本 fork 新增）：时间线 / 气泡展开态 → 时间线步骤流；气泡收起态 → 极简气泡；
  // 折叠卡片 → 沿用官方 stepgroup（现状）。三种风格每步都复用 StepRow 的「raw」点开看详情，不造假。
  const expanded = style === "timeline" || (style === "bubble" && bubbleOpen);
  if (expanded) {
    return (
      <div className="turn-timeline my-1" data-testid={style === "timeline" ? "turn-timeline" : "turn-bubble-expanded"}>
        {running && (
          <div className="flex items-center gap-1.5 text-[12.5px] text-accent mb-1">
            <span className="spinner" />
            <span>{t("chat.runningSteps", { stepsLabel })}</span>
            {liveLine && <span className="truncate text-muted max-w-[40ch]">· {liveLine}</span>}
          </div>
        )}
        <TurnSteps rows={rows} onAllowAnyway={onAllowAnyway} streamingText={streamingText} border />
      </div>
    );
  }
  if (style === "bubble" && !bubbleOpen) {
    return (
      <div className="my-1" data-testid="turn-bubble">
        <button
          type="button"
          className="inline-flex items-center gap-2 max-w-full rounded-2xl bg-accentSoft border border-accent/30 px-3.5 py-2 text-ui text-accent hover:bg-accent/10"
          onClick={() => setBubbleOpen(true)}
          data-testid="turn-bubble-open"
        >
          {running && <span className="spinner" />}
          <span className="font-medium whitespace-nowrap">{running ? t("chat.runningSteps", { stepsLabel }) : stepsLabel}</span>
          {running && liveLine && (
            <span className="truncate text-ink/80 max-w-[36ch]">· {liveLine}</span>
          )}
        </button>
      </div>
    );
  }

  return (
    <details className="stepgroup" open={open}>
      <summary
        className="stepgroup-head flex items-center gap-2 py-0.5 cursor-pointer select-none text-[12.5px] text-faint hover:text-muted"
        onClick={(e) => {
          e.preventDefault(); // drive open/closed from state, not the native toggle
          setUserToggle(!open);
        }}
      >
        <span className={"chev inline-block transition-transform" + (open ? " rotate-90" : "")}>›</span>
        <span>
          <span>{running ? t("chat.runningSteps", { stepsLabel }) : stepsLabel}</span>
          {declined > 0 && (
            <>
              {" · "}
              <span className="text-danger" data-testid="stepgroup-declined">
                {t("chat.declinedCount", { count: declined })}
              </span>
            </>
          )}
          {hiddenTotal > 0 && (
            <>
              {" · "}
              <span className="text-warnInk" data-testid="stepgroup-hidden">
                {t("chat.hiddenByFilters", { count: hiddenTotal })}
              </span>
            </>
          )}
        </span>
        {running && !open && liveLine && (
          <span className="min-w-0 flex-1 truncate" data-testid="turn-live-line">
            · {liveLine}
          </span>
        )}
      </summary>
      {open && (
        <TurnSteps rows={rows} onAllowAnyway={onAllowAnyway} streamingText={streamingText} border />
      )}
    </details>
  );
}

// 步骤列表（时间线 / 气泡展开态 / 折叠卡片展开态 共用）：每条 step 复用 StepRow，
// 自带「raw」点开看工具名 + 入参 + 结果预览，保证执行过程真实可查、不造假。
function TurnSteps({
  rows,
  onAllowAnyway,
  streamingText,
  border,
}: {
  rows: TurnRow[];
  onAllowAnyway?: (name: string, args: any) => void;
  streamingText?: string;
  border?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div className={"flex flex-col gap-0.5" + (border ? " ml-1.5 mt-1 pl-2 border-l-2 border-line" : "")}>
      {rows.map((row, i) =>
        row.type === "narr" ? (
          <div className="turn-narr px-2 py-1 text-ui text-muted max-w-[60ch]" key={i} data-testid="turn-narration">
            <Markdown text={row.text} />
          </div>
        ) : row.type === "ask" ? (
          <div className="flex items-baseline gap-2 px-2 py-0.5" key={i} data-testid="turn-ask">
            <span className={"w-3.5 text-center text-[10px] shrink-0 " + (row.approval.resolved === "deny" ? "text-danger" : "text-ok")}>●</span>
            <LineText line={humanizeAsk(row.approval.name, row.approval.args)} />
            {approvalChip(row.approval.resolved, t)}
          </div>
        ) : (
          <StepRow tool={row.tool} approval={row.approval} onAllowAnyway={onAllowAnyway} key={i} />
        ),
      )}
      {streamingText && (
        <div className="turn-narr px-2 py-1 text-ui text-muted max-w-[60ch]" data-testid="turn-live-stream">
          <Markdown text={streamingText} />
          <span className="stream-cursor">▍</span>
        </div>
      )}
    </div>
  );
}

interface Props {
  items: Item[];
  onApprove: (decision: ApprovalDecision) => void;
  // The session's live flag. While true, the FINAL run's trailing assistant text is still
  // narration (status), not the answer — promoting it early made each line flash as a full
  // ASSISTANT bubble and then vanish into the group when the next tool call arrived
  // (owner report 2026-07-13). The answer bubble appears once, when the turn ends.
  running?: boolean;
  // Sub-threshold streamed text (streamGate mode "quiet") — handed to the live turn group.
  streamingText?: string;
  // Re-run the failed turn (no new user message). Offered only on a retriable notice that
  // is the transcript tail of an idle session — anywhere else the error is history.
  onRetry?: () => void;
  /** 当前激活的专家（专家模式）；为 null 时回复角色显示「助手」。 */
  expert?: Expert | null;
  /** 官方 v0.2.0 自动审批审阅者「允许 anyway」覆写回调；保留 prop 以便官方 reviewer 测试
   *  通过类型检查。本 fork 的审阅者闸门 UI 接入后再接该回调。 */
  onAllowAnyway?: (tool: string, args: any) => void;
  /** 记忆保存提示的 Undo 回调（MEMORY-SPEC §5.1）；保留 prop 以便官方 memory 测试通过类型检查。 */
  onUndoMemory?: (id: number, previous?: string) => void;
  /** 「记住这个」→ 写主动产出台账（mobile_voice，修⑧）；不传则不渲染按钮（桌面零变化）。 */
  onRememberItem?: (text: string) => void;
  // R13 意图讨论态：draft 阶段的三按钮回调。不传则只显示文字不带按钮（兼容老调用方）。
  onFinalizeIntent?: (intentId: string) => void;
  onReviseIntent?: (intentId: string, round: number) => void;
  onCancelIntent?: (intentId: string) => void;
}

// The transcript index whose notice gets the Retry button: the tail error notice, looking
// through info notices after it (model switches must not consume the retry — switching
// models and THEN retrying is the intended recovery path). -1 when the tail is anything else.
export function retryAnchor(items: Item[]): number {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind !== "notice") return -1;
    if (it.retriable) return i;
    if (it.tone !== "info") return -1;
  }
  return -1;
}

export function Transcript({
  items,
  running,
  streamingText,
  onRetry,
  onAllowAnyway,
  onUndoMemory,
  onRememberItem,
  onFinalizeIntent,
  onReviseIntent,
  onCancelIntent,
  expert,
}: Props) {
  // §33 grouping: a turn = the maximal run of assistant/tool/resolved-approval items between
  // breakers (user, connector, notices, plan/dir requests…). Trailing assistant texts are the
  // ANSWER and render as bubbles after the group; interior assistant texts are narration and
  // stay inside. A run with no activity at all is just bubbles (unchanged chat behavior).
  const blocks: Array<{ turn: TurnItem[]; live?: boolean } | { item: Item; i: number }> = [];
  let run: TurnItem[] = [];
  const flush = (live = false) => {
    if (!run.length) return;
    const turn = [...run];
    run = [];
    const answers: AssistantItem[] = [];
    // A live run with tool activity keeps its trailing text inside as the status line;
    // a live run with NO activity is a plain streaming reply — bubbles, as ever.
    const keepTrailing = live && turn.some((it) => it.kind !== "assistant");
    if (!keepTrailing)
      while (turn.length && turn[turn.length - 1].kind === "assistant")
        answers.unshift(turn.pop() as AssistantItem);
    if (turn.some((it) => it.kind !== "assistant")) blocks.push({ turn, live });
    else turn.forEach((t) => blocks.push({ item: t, i: -1 }));
    answers.forEach((a) => blocks.push({ item: a, i: -1 }));
  };
  items.forEach((item, i) => {
    if (item.kind === "tool" || item.kind === "assistant" || (item.kind === "approval" && item.resolved))
      run.push(item);
    else if (
      // PENDING interactive items render elsewhere (approval/question → composer head) and
      // nothing here — if they broke the run, the trailing narration would flash into an
      // answer bubble exactly while the user is being asked to decide.
      (item.kind === "approval" || item.kind === "dirreq" || item.kind === "planreq" || item.kind === "question") &&
      !item.resolved
    ) {
      return;
    } else {
      flush();
      blocks.push({ item, i });
    }
  });
  flush(!!running);

  const { t } = useTranslation();
  const lastTurnIndex = blocks.reduce((acc, b, i) => ("turn" in b ? i : acc), -1);
  return (
    <div className="transcript">
      {blocks.map((block, bi) => {
        if ("turn" in block)
          return (
            <TurnGroup
              items={block.turn}
              live={block.live}
              streamingText={block.live && bi === lastTurnIndex ? streamingText : undefined}
              onAllowAnyway={onAllowAnyway}
              key={bi}
            />
          );
        const { item } = block;
        switch (item.kind) {
          case "connector":
            // 板消息走结构化 BoardWakeCard（后端 _board_source 已带 source.board.rows；
            // 否则退化成散文整块，正是之前"事件进度卡片粗糙"的根）。其它 connector（Slack 等）
            // 仍走通用 ConnectorMessageCard。
            if (item.source?.board) return <BoardWakeCard source={item.source} key={bi} />;
            return <ConnectorMessageCard source={item.source} key={bi} />;
          case "user":
            return (
              <div className="group self-end max-w-[78%] flex flex-col items-end" key={bi}>
                <div className="bubble-user px-3.5 py-2.5 rounded-[14px_14px_4px_14px] bg-solid text-onSolid text-chat leading-relaxed whitespace-pre-wrap">
                  {item.attachments && item.attachments.length > 0 && (
                    <div className="bubble-attachments">
                      {item.attachments.map((a, i) =>
                        a.kind === "image" ? (
                          <img key={i} className="msg-img" src={a.data_url} alt={a.name} />
                        ) : (
                          <span key={i} className="msg-file">📄 {a.name}</span>
                        ),
                      )}
                    </div>
                  )}
                  <ClampedUserText text={item.text} />
                </div>
                <BubbleMeta
                  text={item.text}
                  ts={item.ts}
                  align="right"
                  onRemember={
                    onRememberItem ? () => onRememberItem(stripEmote(item.text)) : undefined
                  }
                />
              </div>
            );
          case "assistant":
            // Thinking-only item (stopped mid-reasoning): just the disclosure, no bubble.
            if (!item.text && item.reasoning)
              return (
                <div key={bi}>
                  <ThinkingBlock text={item.reasoning} />
                </div>
              );
            return (
              <div className="group bubble-assistant" key={bi}>
                <div className="who">
                  <XiaojuAvatar size={44} className="who-avatar" />
                  <span>{expert?.displayName ?? t("chat.assistant")}</span>
                </div>
                {item.reasoning && <ThinkingBlock text={item.reasoning} />}
                <Markdown text={item.text} />
                <BubbleMeta
                  text={item.text}
                  ts={item.ts}
                  align="left"
                  onRemember={
                    onRememberItem ? () => onRememberItem(stripEmote(item.text)) : undefined
                  }
                />
              </div>
            );
          case "voice":
            // 语音气泡：说「发条语音」时出现，点一下才播。外壳与 assistant 一致（头像+名字）。
            return (
              <div className="group bubble-assistant" key={bi}>
                <div className="who">
                  <XiaojuAvatar size={44} className="who-avatar" />
                  <span>{expert?.displayName ?? t("chat.assistant")}</span>
                </div>
                <VoiceBubble item={item} />
                <BubbleMeta text={item.text} ts={item.ts} align="left" />
              </div>
            );
          case "report":
            // 编辑专家「生成审阅报告」产物：内嵌在会话中的报告卡片（重开软件后仍在）。
            return (
              <div className="group bubble-assistant" key={bi}>
                <div className="who">
                  <XiaojuAvatar size={44} className="who-avatar" />
                  <span>{expert?.displayName ?? t("chat.assistant")}</span>
                </div>
                {item.caption && <p className="report-caption">{item.caption}</p>}
                <ReportViewer report={item.report} name={item.name} />
                <BubbleMeta
                  text={item.caption || item.name}
                  ts={item.ts}
                  align="left"
                />
              </div>
            );
          case "dirreq":
            if (!item.resolved) return null;
            return (
              <div className="approval-inline" key={bi}>
                <span className={"status " + (item.resolved === "granted" ? "ok" : "denied")}>
                  {item.resolved === "granted" ? "✓" : "✕"}
                </span>
                <span>{item.resolved === "granted" ? t("chat.grantedFolderAccess") : t("chat.declinedFolderAccess")}</span>
                {item.path && <span className="dim">{item.path}</span>}
              </div>
            );
          case "planreq":
            if (!item.resolved) return null; // pending plan renders in the composer head
            return (
              <div className="bubble-assistant" key={bi}>
                <div className="who">{t("chat.proposedPlan")}</div>
                <Markdown text={item.plan} />
                <div className="approval-inline">
                  <span className={"status " + (item.resolved === "approved" ? "ok" : "denied")}>
                    {item.resolved === "approved" ? "✓" : "✕"}
                  </span>
                  <span>{item.resolved === "approved" ? t("chat.planApproved") : t("chat.sentBackWithFeedback")}</span>
                </div>
              </div>
            );
          case "notice":
            // A titled notice is prose (the Auto-Approve banner), not a status line —
            // render the title plus one <p> per blank-line-separated paragraph.
            if (item.title) {
              return (
                <div
                  className={"notice notice-block " + (item.tone === "warn" ? "warn" : "")}
                  key={bi}
                  data-testid="mode-notice"
                >
                  <div className="notice-title">{item.title}</div>
                  {String(item.text || "")
                    .split("\n\n")
                    .map((para, i) => (
                      <p key={i}>{para}</p>
                    ))}
                </div>
              );
            }
            return (
              <div
                className={
                  "notice " +
                  (item.tone === "warn" ? "warn " : "") +
                  (item.compact ? "notice-compact" : "")
                }
                key={bi}
                data-testid={item.compact ? "compact-divider" : undefined}
              >
                {item.text}
                {item.retriable && !running && onRetry && block.i === retryAnchor(items) && (
                  <button className="btn ml-2" data-testid="notice-retry" onClick={onRetry}>
                    {t("chat.retry")}
                  </button>
                )}
              </div>
            );
          // §5.1 save notice: quiet, inline, and it STAYS — the user reads it in place
          // and can undo whenever they get to it.
          case "memory":
            return (
              <div
                className="notice flex items-center gap-2 text-left"
                data-testid="memory-notice"
                key={bi}
              >
                {item.undone ? (
                  <span data-testid="memory-notice-undone">
                    {item.previous ? "Okay — put back the way it was." : "Okay — forgotten."}
                  </span>
                ) : (
                  <>
                    <span className="min-w-0">
                      <span className="font-medium">
                        {item.previous ? "I've updated what I remember" : "I'll remember that"}
                      </span>
                      {item.text ? <span className="text-muted"> — {item.text}</span> : null}
                    </span>
                    {onUndoMemory && (
                      <button
                        className="btn ml-auto shrink-0"
                        data-testid="memory-notice-undo"
                        onClick={() => onUndoMemory(item.id, item.previous)}
                      >
                        Undo
                      </button>
                    )}
                  </>
                )}
              </div>
            );
          case "inspire":
            return (
              <div
                className="group bubble-assistant inspire-card"
                key={bi}
              >
                <div className="who">
                  <XiaojuAvatar size={44} className="who-avatar" />
                  <span>💡 小桔的灵感</span>
                </div>
                <Markdown text={item.text} />
                {item.refs && item.refs.length > 0 && (
                  <div className="mt-1.5 text-muted text-[12px]">
                    <span style={{ fontWeight: 600 }}>锚定她观察到的真实世界：</span>
                    {item.refs.map((r, i) => (
                      <span key={i} className="inspire-ref">
                        {r}
                      </span>
                    ))}
                  </div>
                )}
                <BubbleMeta
                  text={item.text}
                  ts={item.ts}
                  align="left"
                  onRemember={
                    onRememberItem ? () => onRememberItem(stripEmote(item.text)) : undefined
                  }
                />
              </div>
            );
          case "advice":
            return (
              <div
                className="group bubble-assistant"
                key={bi}
                style={{ background: "rgba(138,99,210,0.06)", border: "1px solid rgba(138,99,210,0.18)" }}
              >
                <div className="who">
                  <XiaojuAvatar size={44} className="who-avatar" />
                  <span>🗣 小桔的看法</span>
                </div>
                <Markdown text={item.text} />
                <BubbleMeta
                  text={item.text}
                  ts={item.ts}
                  align="left"
                  onRemember={
                    onRememberItem ? () => onRememberItem(stripEmote(item.text)) : undefined
                  }
                />
              </div>
            );
          case "intent_discuss": {
            // R13 意图讨论态：与普通 assistant 气泡同构（无色块、无 emoji——2026-10-08
            // 萧哥反馈「样式太突兀太粗糙，要跟现有样式融合」）。
            // draft=带「定稿/再改/不要」三按钮；opening/final=纯文字。
            // resolved 非空 = 已结束，按钮消失，状态走 approval-inline 小行（与 planreq 同款）。
            const resolved = item.resolved;
            const showButtons = item.phase === "draft" && !resolved && (onFinalizeIntent || onReviseIntent || onCancelIntent);
            return (
              <div
                className="group bubble-assistant"
                key={bi}
                data-testid="intent-discuss-card"
              >
                <div className="who">
                  <XiaojuAvatar size={44} className="who-avatar" />
                  <span>
                    {item.phase === "draft"
                      ? `小桔的产出${item.round > 0 ? ` · 第 ${item.round} 版` : ""}`
                      : expert?.displayName ?? t("chat.assistant")}
                  </span>
                </div>
                <Markdown text={item.text} />
                {showButtons && (
                  <div
                    style={{
                      marginTop: 8,
                      display: "flex",
                      gap: 8,
                      flexWrap: "wrap",
                    }}
                  >
                    {onFinalizeIntent && (
                      <button
                        className="btn"
                        data-testid="intent-discuss-finalize"
                        onClick={() => onFinalizeIntent(item.intent_id)}
                      >
                        定稿
                      </button>
                    )}
                    {onReviseIntent && (
                      <button
                        className="btn"
                        data-testid="intent-discuss-revise"
                        onClick={() => onReviseIntent(item.intent_id, item.round)}
                      >
                        再改
                      </button>
                    )}
                    {onCancelIntent && (
                      <button
                        className="btn"
                        data-testid="intent-discuss-cancel"
                        onClick={() => onCancelIntent(item.intent_id)}
                      >
                        不要
                      </button>
                    )}
                  </div>
                )}
                {resolved && (
                  <div className="approval-inline">
                    <span className={"status " + (resolved === "cancelled" ? "denied" : "ok")}>
                      {resolved === "cancelled" ? "✕" : "✓"}
                    </span>
                    <span>
                      {resolved === "finalized"
                        ? "已定稿"
                        : resolved === "cancelled"
                          ? "已取消"
                          : "按你的意见改中"}
                    </span>
                  </div>
                )}
                <BubbleMeta
                  text={item.text}
                  ts={item.ts}
                  align="left"
                />
              </div>
            );
          }
          default:
            return null;
        }
      })}
    </div>
  );
}
