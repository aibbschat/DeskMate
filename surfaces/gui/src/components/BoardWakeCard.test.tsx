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
