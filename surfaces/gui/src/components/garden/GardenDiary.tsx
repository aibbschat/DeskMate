/**
 * GardenDiary — 树屋书房（蓝图第二层 · 树屋书房·日记，最私密）。
 *
 * 你和小桔共写的日记都收在树屋里：时间线（共写、可🩷、可留言）｜小桔的记录（只读）。
 * 树屋氛围由花园底座承载；回响批注留在日记下（P1 花朵回响已接入花朵 tip）。
 */

import { useState } from "react";
import { DiaryPanel } from "../deskmate/DiaryPanel";
import { MomentFeed } from "../deskmate/MomentFeed";

export function GardenDiary() {
  const [view, setView] = useState<"timeline" | "archive">("timeline");

  return (
    <div className="garden-diary">
      <div className="garden-space-head">
        <div className="garden-space-title">📖 树屋书房</div>
        <div className="garden-space-sub">你和小桔共写的日记，都收在这间树屋里。最私密，也最暖。</div>
      </div>

      <div className="dm-diary-tabs" style={{ marginBottom: 12 }}>
        <button
          type="button"
          className={view === "timeline" ? "active" : ""}
          onClick={() => setView("timeline")}
        >
          时间线
        </button>
        <button
          type="button"
          className={view === "archive" ? "active" : ""}
          onClick={() => setView("archive")}
        >
          小桔的记录
        </button>
      </div>
      <div className="dm-diary-tabs-hint">
        时间线＝你和小桔共写、可🩷可留言；小桔的记录＝小桔自己写的每日随笔（只读查阅）。
      </div>

      {view === "timeline" ? <MomentFeed /> : <DiaryPanel />}
    </div>
  );
}
