"""
数据抢救脚本：从 session 记录里提取每日读书/进度跟踪的历史数据，
用统一的 auto_tasks 格式写入。

运行：.venv/bin/python scripts/rescue_auto_tasks.py
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from deskmate.auto_tasks import register_task, record_task_result, get_recorder
from deskmate.auto_tasks.models import Insight

SESSIONS_DIR = Path(os.path.expanduser("~/.deskmate/sessions"))


def parse_session(filepath: Path) -> dict:
    """解析一个 session 文件，提取任务类型、日期、Assistant 回复。"""
    content = filepath.read_text(encoding="utf-8", errors="ignore")
    ts = int(filepath.stem)
    from datetime import datetime
    date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

    # 判断任务类型
    if "每日读书：萧盛已出版作品" in content:
        task_type = "daily_reading"
    elif "进度跟踪：国子监秘闻录" in content:
        task_type = "progress_tracking"
    else:
        return {}

    # 提取 Assistant 回复（最后一个 ## Assistant 块）
    assistant_blocks = re.findall(r"## Assistant\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    assistant_reply = assistant_blocks[-1].strip() if assistant_blocks else ""

    return {
        "task_type": task_type,
        "date": date,
        "ts": ts,
        "reply": assistant_reply,
        "filepath": filepath,
    }


def extract_reading_info(reply: str) -> dict:
    """从读书任务回复里提取书名、阅读状态、心得。"""
    info = {"target": "", "action": "continue", "summary": "", "insights": [], "detail": ""}

    # 提取书名
    book_match = re.search(r"(书\d+[《「][^》」]+[》」]|[《「][^》」]+[》」])", reply)
    if book_match:
        info["target"] = book_match.group(1)

    # 判断动作
    if "读完" in reply or "全部读完" in reply or "已全部读完" in reply:
        info["action"] = "finish"
    elif "开始读" in reply or "起读" in reply:
        info["action"] = "start"

    # 提取摘要（前 200 字）
    summary_match = re.search(r"今日总结[：:]\n?(.*?)(?=\n-|\Z)", reply, re.DOTALL)
    if summary_match:
        info["summary"] = summary_match.group(1).strip()[:200]
    else:
        # 取回复的前 150 字
        info["summary"] = reply[:150].replace("\n", " ").strip()

    # 提取洞察
    insight_patterns = [
        (r"风格[：:](.*?)(?=\n-|\n[A-Z]|\Z)", ["风格"]),
        (r"人物塑造[：:](.*?)(?=\n-|\n[A-Z]|\Z)", ["人物塑造"]),
        (r"叙事结构[：:](.*?)(?=\n-|\n[A-Z]|\Z)", ["叙事结构"]),
        (r"与已读作品关联[：:](.*?)(?=\n-|\n[A-Z]|\Z)", ["关联"]),
    ]
    for pattern, tags in insight_patterns:
        m = re.search(pattern, reply, re.DOTALL)
        if m:
            text = m.group(1).strip()[:300]
            if text:
                info["insights"].append(Insight(
                    text=text,
                    tags=tags,
                    target=info["target"],
                    push_to_vault=True,
                    push_to_style=("风格" in tags),
                ))

    info["detail"] = reply
    return info


def extract_tracking_info(reply: str) -> dict:
    """从进度跟踪回复里提取进度、伏笔、人物状态。"""
    info = {"target": "国子监秘闻录", "action": "analyze", "summary": "", "insights": [], "detail": ""}

    # 提取摘要
    info["summary"] = reply[:200].replace("\n", " ").strip()

    # 提取进度
    progress_match = re.search(r"正文[：:](.*?)(?=\n-|\n[A-Z]|\Z)", reply, re.DOTALL)
    if progress_match:
        info["insights"].append(Insight(
            text=f"正文进度：{progress_match.group(1).strip()[:200]}",
            tags=["进度"],
            target="国子监秘闻录",
            push_to_vault=True,
        ))

    # 提取伏笔
    foreshadow_match = re.search(r"伏笔[：:](.*?)(?=\n-|\n[A-Z]|\Z)", reply, re.DOTALL)
    if foreshadow_match:
        info["insights"].append(Insight(
            text=f"伏笔状态：{foreshadow_match.group(1).strip()[:200]}",
            tags=["伏笔"],
            target="国子监秘闻录",
            push_to_vault=True,
        ))

    # 提取人物
    char_match = re.search(r"人物[：:](.*?)(?=\n-|\n[A-Z]|\Z)", reply, re.DOTALL)
    if char_match:
        info["insights"].append(Insight(
            text=f"人物状态：{char_match.group(1).strip()[:200]}",
            tags=["人物"],
            target="国子监秘闻录",
            push_to_vault=True,
        ))

    info["detail"] = reply
    return info


def main():
    # 注册任务
    register_task("daily_reading", "每日读书", category="works",
                  description="每日阅读萧盛已出版作品，沉淀风格/人物/叙事观察")
    register_task("progress_tracking", "进度跟踪", category="works",
                  description="每日跟进正在写的书，检查进度/伏笔/人物出场")

    # 扫描所有 session
    rescued = 0
    for session_file in sorted(SESSIONS_DIR.glob("*.md")):
        parsed = parse_session(session_file)
        if not parsed:
            continue

        if parsed["task_type"] == "daily_reading":
            info = extract_reading_info(parsed["reply"])
        else:
            info = extract_tracking_info(parsed["reply"])

        if not info["target"]:
            info["target"] = "未知作品"

        # 用 session 的实际日期（覆盖 recorder 默认的今天）
        # 注意：recorder 会用今天的日期，我们需要手动写 timeline
        # 这里直接调用 recorder，但日期用 session 的日期
        from deskmate.auto_tasks.recorder import get_recorder
        from deskmate.auto_tasks.models import TaskRecord
        import time, json
        from dataclasses import asdict

        recorder = get_recorder()
        registry = recorder.__class__.__mro__[0]  # 不直接用，保持简单

        # 手动构造记录并写入 timeline（用 session 的实际日期）
        record = TaskRecord(
            task_type=parsed["task_type"],
            task_name="每日读书" if parsed["task_type"] == "daily_reading" else "进度跟踪",
            target=info["target"],
            action=info["action"],
            date=parsed["date"],
            ts=parsed["ts"],
            summary=info["summary"],
            insights=info["insights"],
            tags=[],
            detail_file=None,
            extra={"rescued_from_session": str(session_file.name)},
        )

        # 写 timeline
        with open(recorder.timeline_file, "a", encoding="utf-8") as f:
            f.write(record.to_jsonl() + "\n")

        # 写详情文件
        if info["detail"]:
            type_details_dir = recorder.details_dir / parsed["task_type"]
            type_details_dir.mkdir(exist_ok=True)
            fname = f"{parsed['date']}_{info['target'][:30]}.md"
            fpath = type_details_dir / fname
            fpath.write_text(info["detail"], encoding="utf-8")
            record.detail_file = f"{parsed['task_type']}/{fname}"
            # 重写 timeline 这一条（带上 detail_file）
            # 简单做法：读全部、替换最后一条、重写
            lines = recorder.timeline_file.read_text(encoding="utf-8").strip().split("\n")
            lines[-1] = record.to_jsonl()
            recorder.timeline_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # 写洞察
        if info["insights"]:
            type_insights_dir = recorder.insights_dir / parsed["task_type"]
            type_insights_dir.mkdir(exist_ok=True)
            insights_file = type_insights_dir / f"{parsed['date']}.jsonl"
            with open(insights_file, "a", encoding="utf-8") as f:
                for ins in info["insights"]:
                    f.write(json.dumps(ins.__dict__, ensure_ascii=False) + "\n")

        rescued += 1
        print(f"  抢救: {parsed['date']} {record.task_name} | {info['target']} | {info['action']} | {len(info['insights'])} insights")

    print(f"\n共抢救 {rescued} 条记录")

    # 验证
    stats = get_recorder().get_stats()
    print(f"统计: {stats['total_records']} 条记录, {stats['unique_targets']} 个对象")
    print(f"分类: {stats['categories']}")


if __name__ == "__main__":
    main()
