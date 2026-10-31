// BoardWakeCard — a board wake in the lead's transcript, collapsed to ONE line by
// default (owner ruling 2026-08-16): most of the time the user just wants the
// feel that something is happening. Click to expand into per-event rows; long
// hand-off comments hide behind a per-row "show hand-off". NOT the connector
// card: a connector message is a foreign message, a board wake is a report —
// different shape, different affordances (they only share the visual family).
import { useState } from "react";
import type { BoardWakeRow, MessageSource } from "../api";
import { Icon } from "./Icon";

function summarize(rows: BoardWakeRow[]): { text: string; attention: boolean } {
  const counts: Record<string, number> = {};
  const bump = (key: string) => (counts[key] = (counts[key] || 0) + 1);
  for (const row of rows) {
    if (row.kind === "moved" && row.to === "review") bump("review");
    else if (row.kind === "moved" && row.to === "blocked") bump("blocked");
    else if (row.kind === "moved" && row.to === "canceled") bump("canceled");
    else if (row.kind === "moved") bump("move");
    else if (row.kind === "filed") bump("filing");
    else if (row.kind === "claimed") bump("claim");
    else if (row.kind === "assigned") bump("assignment");
    else if (row.kind === "comment") bump("comment");
    else if (row.kind === "chat") bump("chat message");
    else if (row.kind === "waiting") {
      // §11.6: a parked worker call. Only the lead can answer it — for anyone else
      // it is a status line, not an action, so it must not read as their decision.
      if (readerDecides(row)) bump("decision");
      else bump("waiting");
    }
  }
  const parts = Object.entries(counts).map(
    ([label, n]) => `${n} ${label}${n === 1 ? "" : "s"}`
  );
  // reviews/blocked/pending decisions demand a decision — those tint the
  // collapsed line amber. A decision is the loudest of them: name it first.
  const attention =
    (counts.decision || 0) + (counts.review || 0) + (counts.blocked || 0) > 0;
  return { text: parts.join(", ") || "update", attention };
}

// Does this parked-call row ask the READER to decide? Rows written before
// 2026-10-31 carry no flag; a wait-only batch never wakes a worker by design
// (the lead is the authority), so an unflagged row is always a lead's row —
// defaulting to true keeps already-delivered wakes from flipping to the
// informational wording on the next render.
function readerDecides(row: BoardWakeRow): boolean {
  return row.decision !== false;
}

function rowText(row: BoardWakeRow): string {
  const item = row.item != null ? `#${row.item}` : "";
  const title = row.title ? ` ${row.title}` : "";
  switch (row.kind) {
    case "moved":
      return `${item}${title} → ${row.to} by ${row.actor}`;
    case "filed":
      return `${row.actor} filed ${item}${title}`;
    case "claimed":
      return `${row.actor} claimed ${item}${title}`;
    case "assigned":
      return `${item}${title} assigned to you`;
    case "comment":
      return `${row.actor} commented on ${item}${title}`;
    case "chat":
      return `# team chat — ${row.actor}`;
    case "waiting": {
      // §11.6 — a worker parked on a tool call. The lead is the approval authority
      // (its decision asks the human); for everyone else this is informational.
      const what = row.tool || "a tool call";
      const who = row.actor || "A worker";
      return readerDecides(row)
        ? `${who} is waiting for your decision on ${what}`
        : `${who} is waiting for the lead or user on ${what}`;
    }
    default:
      // An unrecognized kind must never render as a blank row (a WORKER_WAITING
      // row with no item/title produced exactly that). Fall back to something
      // readable, then to the kind itself, then to the source text's own label.
      return `${item}${title}`.trim() || `${row.actor || ""} ${row.kind}`.trim();
  }
}

function stateDot(row: BoardWakeRow): string {
  // A parked worker call is a decision awaiting someone — amber like review, but
  // only when the reader is the one who can answer it.
  if (row.kind === "waiting")
    return readerDecides(row) ? "board-dot review" : "board-dot idle";
  if (row.kind === "moved" && row.to === "review") return "board-dot review";
  if (row.kind === "moved" && row.to === "blocked") return "board-dot blocked";
  if (row.kind === "moved" && row.to === "done") return "board-dot done";
  if (row.kind === "claimed" || row.kind === "assigned") return "board-dot work";
  return "board-dot idle";
}

export function BoardWakeCard({ source }: { source: MessageSource }) {
  const [open, setOpen] = useState(false);
  const [openNotes, setOpenNotes] = useState<Record<number, boolean>>({});
  const rows = source.board?.rows || [];
  const { text, attention } = summarize(rows);
  return (
    <div
      className={"boardwake" + (attention ? " attention" : "")}
      data-testid="boardwake-card"
    >
      <button
        className="boardwake-head"
        data-testid="boardwake-toggle"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <Icon name="table" size={14} />
        <span className="boardwake-title">Board wake</span>
        <span className="boardwake-summary">{text}</span>
        <span className="spacer" />
        <span className={"boardwake-chevron" + (open ? " open" : "")}>
          <Icon name="chevronDown" size={13} />
        </span>
      </button>
      {open && (
        <div className="boardwake-body" data-testid="boardwake-body">
          {rows.map((row, i) => (
            <div className="boardwake-row" key={i}>
              <span className={stateDot(row)} />
              <span className="boardwake-row-main">
                <span className="boardwake-row-text">{rowText(row)}</span>
                {row.note &&
                  (openNotes[i] ? (
                    <span className="boardwake-note">{row.note}</span>
                  ) : (
                    <button
                      className="boardwake-note-toggle"
                      onClick={() => setOpenNotes((s) => ({ ...s, [i]: true }))}
                    >
                      {row.kind === "chat" || row.kind === "comment"
                        ? "show message"
                        : row.kind === "waiting"
                          ? "show request"
                          : "show hand-off"}
                    </button>
                  ))}
              </span>
            </div>
          ))}
          {rows.length === 0 && (
            <div className="boardwake-note">{source.text}</div>
          )}
        </div>
      )}
    </div>
  );
}
