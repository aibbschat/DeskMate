"""日历事件读取 — 通过 AppleScript 读取 macOS Calendar.app 事件。"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class CalendarEvent:
    summary: str      # 事件标题
    start: str        # 开始时间（ISO 格式）
    end: str          # 结束时间
    location: str     # 地点
    notes: str        # 备注（截断到 200 字）
    calendar: str     # 所属日历名


def get_calendar_events(days: int = 1, max_events: int = 20) -> list[CalendarEvent]:
    """获取未来 N 天的日历事件。仅 macOS。"""
    start_date = datetime.now().strftime("%Y-%m-%d")
    end_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

    script = f'''
    set startDate to date "{start_date}"
    set endDate to date "{end_date}"

    tell application "Calendar"
        set output to ""
        set eventCount to 0
        repeat with cal in calendars
            set calName to name of cal
            try
                set calEvents to (every event of cal whose start date >= startDate and start date < endDate)
                repeat with evt in calEvents
                    if eventCount >= {max_events} then exit repeat
                    set evtSummary to summary of evt
                    set evtStart to (start date of evt) as text
                    set evtEnd to (end date of evt) as text
                    try
                        set evtLocation to location of evt
                    on error
                        set evtLocation to ""
                    end try
                    try
                        set evtNotes to notes of evt
                        if length of evtNotes > 200 then
                            set evtNotes to text 1 thru 200 of evtNotes
                        end if
                    on error
                        set evtNotes to ""
                    end try
                    set output to output & evtSummary & "|||" & evtStart & "|||" & evtEnd & "|||" & evtLocation & "|||" & evtNotes & "|||" & calName & "\\n"
                    set eventCount to eventCount + 1
                end repeat
            end try
            if eventCount >= {max_events} then exit repeat
        end repeat
        return output
    end tell
    '''

    result = _run_osascript(script)
    if not result:
        return []

    events: list[CalendarEvent] = []
    for line in result.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|||")
        if len(parts) < 6:
            continue
        events.append(CalendarEvent(
            summary=parts[0].strip(),
            start=parts[1].strip(),
            end=parts[2].strip(),
            location=parts[3].strip(),
            notes=parts[4].strip(),
            calendar=parts[5].strip(),
        ))
    return events


def _run_osascript(script: str) -> str | None:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None
