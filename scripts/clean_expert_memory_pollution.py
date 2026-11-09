"""清理专家/工人 1v1 会话对小桔记忆的污染（pair 匹配；--apply 才动）。"""
import argparse, json, pathlib, shutil, sqlite3, re
from datetime import datetime

HOME = pathlib.Path.home()
COMPANION = {"deskmate", "cowork"}
DB = HOME / ".config/coworker/coworker.db"
SESS = HOME / ".deskmate/sessions"
OBS = HOME / ".deskmate/agency/xiaosheng_observations.jsonl"
USER = HOME / ".deskmate/USER.md"
MEM = HOME / ".deskmate/MEMORY.md"
EXTRA_MD = {"1794128260978114000.md"}
EXTRA_OBS_PREFIX = "你不要读正文以外的其他东西"

def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); con.row_factory = sqlite3.Row
    pairs = set()
    sessions = []
    for row in con.execute("select session_id, agent, title, updated_at from sessions"):
        if row["agent"] in COMPANION or str(row["session_id"]).startswith("__run__"):
            continue
        sessions.append(dict(row))
        conv = HOME / ".config/coworker/conversations" / (str(row["session_id"]) + ".jsonl")
        if not conv.exists():
            continue
        user = None
        for ln in conv.read_text(encoding="utf-8", errors="ignore").splitlines():
            try: m = json.loads(ln)
            except Exception: continue
            role, c = m.get("role"), norm(m.get("content"))
            if role == "user" and c:
                user = c
            elif role == "assistant" and c and user:
                pairs.add((user[:80], c[:80]))
                user = None
    print(f"非 companion session: {len(sessions)}；可用 (user,assistant) 对: {len(pairs)}")

    md_hits = []
    for p in SESS.glob("*.md"):
        try: text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError: continue
        mu = re.search(r"## User\s*\n(.+?)(?:\n\s*## Assistant|\Z)", text, re.S)
        ma = re.search(r"## Assistant\s*\n(.+)", text, re.S)
        if not mu or not ma: continue
        u, a = norm(mu.group(1)), norm(ma.group(1))
        if (u[:80], a[:80]) in pairs:
            md_hits.append(p)
    for p in SESS.glob("*.md"):
        if p.name in EXTRA_MD and p not in md_hits:
            md_hits.append(p)
    print(f"sessions/*.md 命中: {len(md_hits)}")
    for p in md_hits: print("   -", p.name)

    obs_lines = OBS.read_text(encoding="utf-8", errors="ignore").splitlines() if OBS.exists() else []
    obs_hits = []
    for ln in obs_lines:
        try: d = json.loads(ln)
        except Exception: continue
        body = norm(d.get("body"))
        if "（小桔本轮回应）" not in body: continue
        head, tail = body.split("（小桔本轮回应）", 1)
        if (head.strip()[:80], tail.strip()[:80]) in pairs or head.strip().startswith(EXTRA_OBS_PREFIX):
            obs_hits.append(ln)
    print(f"xiaosheng_observations 命中: {len(obs_hits)}")

    user_lines = USER.read_text(encoding="utf-8", errors="ignore").splitlines() if USER.exists() else []
    suspects = []
    for i, line in enumerate(user_lines):
        s = line.strip()
        if s.startswith("2026-11-09:") and any(k in s for k in ("写东西的顺序", "影视改编", "自称“写手”", "表达的观点")):
            suspects.append((i, line))
    print(f"USER.md 可疑行: {len(suspects)}")
    for _, l in suspects: print("   -", l[:120])

    mem_lines = MEM.read_text(encoding="utf-8", errors="ignore").splitlines() if MEM.exists() else []
    mem_suspects = []
    for i, line in enumerate(mem_lines):
        s = line.strip()
        if s.startswith("2026-11-09: 萧盛名下还有几个书名") or s.startswith("2026-11-09: 萧盛未写完的《国子监秘闻录》"):
            mem_suspects.append((i, line))
    print(f"MEMORY.md 可疑行: {len(mem_suspects)}")
    for _, l in mem_suspects: print("   -", l[:120])

    if not args.apply:
        print("\n(dry-run；加 --apply 执行)")
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    q = HOME / ".deskmate" / "_quarantine" / f"expert_memory_{stamp}"
    q.mkdir(parents=True, exist_ok=True)
    for p in md_hits:
        shutil.move(str(p), str(q / p.name))
    if obs_hits:
        shutil.copy2(OBS, q / "xiaosheng_observations.jsonl.before")
        hit = set(obs_hits)
        kept = [ln for ln in obs_lines if ln not in hit]
        OBS.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        (q / "xiaosheng_observations_removed.jsonl").write_text("\n".join(obs_hits) + "\n", encoding="utf-8")
    if suspects:
        shutil.copy2(USER, q / "USER.md.before")
        drop = {i for i, _ in suspects}
        USER.write_text("\n".join(l for i, l in enumerate(user_lines) if i not in drop) + "\n", encoding="utf-8")
    if mem_suspects:
        shutil.copy2(MEM, q / "MEMORY.md.before")
        drop2 = {i for i, _ in mem_suspects}
        MEM.write_text("\n".join(l for i, l in enumerate(mem_lines) if i not in drop2) + "\n", encoding="utf-8")
    print(f"\n已清理到 {q}；md={len(md_hits)} obs={len(obs_hits)} user_lines={len(suspects)} mem_lines={len(mem_suspects)}")

if __name__ == "__main__":
    main()
