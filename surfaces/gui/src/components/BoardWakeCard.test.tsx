import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { BoardWakeCard } from "./BoardWakeCard";

// Board deliveries carry a structured `board.rows` payload (server _board_source).
// This card must render THOSE — not re-parse prose — so the event progress is a real
// structured list, not a fake. Covers the owner's "no fake/placeholder UI" rule.
const source = {
  connector: "board",
  kind: "channel",
  channel_id: "space",
  channel_name: "Team board",
  sender_id: "board",
  sender_name: "Board",
  ts: 0,
  text: "fallback text",
  board: {
    rows: [
      { item: 3, title: "Research", actor: "gouzhang", kind: "moved", to: "in_progress" },
      { item: 5, title: "Draft", actor: "li", kind: "moved", to: "review" },
      { item: 7, title: "Blocked item", actor: "wang", kind: "moved", to: "blocked" },
      { item: 9, title: "Filed", actor: "zhao", kind: "filed" },
      { item: 11, title: "Claimed", actor: "qian", kind: "claimed" },
      { item: 13, title: "Comment", actor: "sun", kind: "comment", note: "hand-off note" },
    ],
  },
} as any;

afterEach(cleanup);

describe("BoardWakeCard — structured event report", () => {
  it("collapses to one line by default, expands to per-event rows on click", () => {
    const { container } = render(<BoardWakeCard source={source} />);
    // collapsed: header present, body hidden
    expect(screen.getByTestId("boardwake-card")).toBeTruthy();
    expect(screen.queryByTestId("boardwake-body")).toBeNull();
    // attention flag set (review + blocked present)
    expect(container.querySelector(".boardwake.attention")).toBeTruthy();

    fireEvent.click(screen.getByTestId("boardwake-toggle"));
    expect(screen.getByTestId("boardwake-body")).toBeTruthy();
    // each structured row text renders (not the raw prose fallback)
    expect(screen.getByText(/#3 Research → in_progress by gouzhang/)).toBeTruthy();
    expect(screen.getByText(/zhao filed #9 Filed/)).toBeTruthy();
    expect(screen.getByText(/qian claimed #11 Claimed/)).toBeTruthy();
  });

  it("maps each event kind to the correct status dot class", () => {
    const { container } = render(<BoardWakeCard source={source} />);
    fireEvent.click(screen.getByTestId("boardwake-toggle"));
    expect(container.querySelector(".board-dot.review")).toBeTruthy();
    expect(container.querySelector(".board-dot.blocked")).toBeTruthy();
    expect(container.querySelector(".board-dot.work")).toBeTruthy(); // in_progress → work
    expect(container.querySelector(".board-dot.idle")).toBeTruthy(); // filed/claimed → idle
  });

  it("falls back to source.text when there are no rows (never a blank card)", () => {
    const empty = { ...source, board: { rows: [] } };
    render(<BoardWakeCard source={empty} />);
    fireEvent.click(screen.getByTestId("boardwake-toggle"));
    expect(screen.getByText("fallback text")).toBeTruthy();
  });
});

// §11.6 — a worker parked on a tool call reaches the lead as a WORKER_WAITING
// digest row. These rows carry NO item/title (a session is not a task identity),
// so an unhandled kind used to render as a bullet with no text at all — the card
// looked styled but empty. Every row must render readable text.
describe("BoardWakeCard — worker waiting rows", () => {
  const waiting = {
    ...source,
    board: {
      rows: [
        {
          item: null,
          title: "",
          actor: "kanyu",
          kind: "waiting",
          tool: "web_search",
          prompt_id: "f30f5d85",
          decision: true,
          note: "requires approval\ncontent: query=夜不收 守军编制",
        },
        {
          item: null,
          title: "",
          actor: "gouzhang",
          kind: "waiting",
          tool: "write_file",
          prompt_id: "14b217f9",
          decision: false,
        },
      ],
    },
  } as any;

  it("renders every waiting row with text (never a blank bullet)", () => {
    render(<BoardWakeCard source={waiting} />);
    fireEvent.click(screen.getByTestId("boardwake-toggle"));
    expect(
      screen.getByText("kanyu is waiting for your decision on web_search")
    ).toBeTruthy();
    expect(
      screen.getByText("gouzhang is waiting for the lead or user on write_file")
    ).toBeTruthy();
    // every rendered row has non-empty text
    for (const row of document.querySelectorAll(".boardwake-row-text")) {
      expect((row.textContent || "").trim().length).toBeGreaterThan(0);
    }
  });

  it("summarizes a decision as an attention-worthy decision, not a bare 'update'", () => {
    const { container } = render(<BoardWakeCard source={waiting} />);
    expect(screen.getByText(/1 decision/)).toBeTruthy();
    expect(container.querySelector(".boardwake.attention")).toBeTruthy();
  });

  it("reveals what the worker asked for behind 'show request'", () => {
    render(<BoardWakeCard source={waiting} />);
    fireEvent.click(screen.getByTestId("boardwake-toggle"));
    expect(screen.queryByText(/requires approval/)).toBeNull();
    fireEvent.click(screen.getByText("show request"));
    expect(screen.getByText(/requires approval/)).toBeTruthy();
  });

  it("a row written before the flag existed still reads as the lead's decision", () => {
    // Wakes already sitting in the transcript (2026-10-31 00:58) predate the
    // `decision` field; they must not flip to the informational wording.
    const legacy = {
      ...source,
      board: {
        rows: [{ item: null, title: "", actor: "kanyu", kind: "waiting", tool: "web_search" }],
      },
    } as any;
    render(<BoardWakeCard source={legacy} />);
    fireEvent.click(screen.getByTestId("boardwake-toggle"));
    expect(
      screen.getByText("kanyu is waiting for your decision on web_search")
    ).toBeTruthy();
  });

  it("an unknown kind with no item/title still renders text, never blank", () => {
    const odd = {
      ...source,
      board: { rows: [{ item: null, title: "", actor: "maya", kind: "something_new" }] },
    } as any;
    render(<BoardWakeCard source={odd} />);
    fireEvent.click(screen.getByTestId("boardwake-toggle"));
    const text = document.querySelector(".boardwake-row-text");
    expect((text?.textContent || "").trim()).toBe("maya something_new");
  });
});
