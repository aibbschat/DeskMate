/**
 * 小桔日记文件的条目切分 —— 桌面 MomentFeed 与手机端共用一份。
 *
 * 从 `components/deskmate/MomentFeed.tsx` 原样提取（2026-10-05，手机端 M3）。之所以必须共享：
 * 日记是**文件**（`~/.deskmate/diary/YYYY-MM-DD.md`），一天里可能连记几篇，每篇要独立成卡
 * 才能各自🩷/留言/归档。这个切分规则一旦两端各写一份，就会出现「桌面分成 3 篇、手机分成
 * 2 篇」的漂移——而 🩷/留言是按 entry id（`xj-<eid>`）挂的，漂了就读不到旧留言。
 *
 * 格式约定（后端 DiaryWriter 写、这里读）：
 *   # 当天标题                                  → dayTitle
 *   ## 18:42 <!--xj:stable-eid-->               → 一条（eid 可选，兼容存量老文件）
 *   正文…（任意行，直到下一个 ## 或文件尾）
 */

export type DiaryEntry = { time: string; eid: string | null; seq: number; body: string };

export function splitDiaryEntries(text: string): { dayTitle: string; entries: DiaryEntry[] } {
  const lines = text.split("\n");
  let dayTitle = "";
  const entries: DiaryEntry[] = [];
  let curTime: string | null = null;
  let curEid: string | null = null;
  let curLines: string[] = [];
  let seq = 0;
  const push = () => {
    if (curTime !== null || curLines.length > 0) {
      entries.push({ time: curTime ?? "", eid: curEid, seq, body: curLines.join("\n").trim() });
    }
    curTime = null;
    curEid = null;
    curLines = [];
  };
  for (const line of lines) {
    if (line.startsWith("# ")) {
      dayTitle = line.slice(2).trim();
    } else {
      // 匹配 "## HH:MM <!--xj:...-->"，eid 可选（兼容未补丁的老文件）
      const m = line.match(/^##\s+(.*?)\s*(?:<!--xj:([0-9A-Za-z-]+)-->)?\s*$/);
      if (m) {
        push();
        curTime = m[1].trim();
        curEid = m[2] ?? null;
        seq += 1;
      } else {
        curLines.push(line);
      }
    }
  }
  push();
  return { dayTitle, entries };
}

/**
 * 一条日记的稳定 id：优先用后端给的 eid（重排/增删都不变），老文件没 eid 时退回
 * `日期#序号`（与桌面 MomentFeed 同规则，否则存量留言会读不到）。
 */
export function diaryEntryId(date: string, entry: DiaryEntry, index: number): string {
  return entry.eid ? `xj-${entry.eid}` : `xj-${date}#${index}`;
}
