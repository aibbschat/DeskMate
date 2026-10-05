/**
 * 审批措辞的中文层（只给手机端）。
 *
 * 为什么不复用桌面 `humanize.ts` 的 `humanizeApprovalTitle` / `approvalActionLabels`：
 * 它们是**硬编码英文**（humanize.ts:110-146 的 "Write "/"Edit "/"Run a command"，
 * ApprovalCard.tsx:37-41 的 "Allow once"/"Deny"）。桌面中文界面里现在就挂着这些英文按钮
 * （上游 OpenWorker 的遗留，已单独报给萧哥）。手机上的「等她点头」是要在地铁上零点五秒
 * 看懂并决定的，不能是英文。
 *
 * 这里只做措辞，不改变任何决策语义：按钮仍然发 WS 的 once/deny、REST 的 allow/deny。
 */

const base = (p: unknown): string => {
  const s = String(p ?? "").replace(/\\/g, "/");
  const parts = s.split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : "这个文件";
};

/** 工具名 → 中文一句话。返回空串表示走 default 兜底。 */
export function askPhrase(name: string, argsRaw: unknown): string {
  const a = (argsRaw && typeof argsRaw === "object" ? argsRaw : {}) as Record<string, unknown>;
  switch (name) {
    case "write_file":
      return `新建/覆盖 ${base(a.path)}`;
    case "replace_in_file":
    case "apply_patch":
    case "apply_unified_diff":
      return `改 ${base(a.path)}`;
    case "read_file":
    case "list_files":
    case "glob":
    case "grep":
      return `读 ${a.path ? base(a.path) : "本地文件"}`;
    case "run_shell": {
      const desc = typeof a.description === "string" && a.description.trim() ? a.description.trim() : "";
      return desc ? `跑一条命令 · ${desc}` : "跑一条命令";
    }
    case "send_message":
      return `发消息给 ${a.target ? String(a.target) : "某人"}`;
    case "send_file":
      return `把文件发给 ${a.target ? String(a.target) : "某人"}`;
    case "create_scheduled_task":
      return a.title ? `建一个定时任务「${String(a.title).slice(0, 40)}」` : "建一个定时任务";
    case "save_skill":
      return a.name ? `把「${String(a.name)}」加进你的技能` : "加一个新技能";
    case "web_search":
      return `上网搜「${String(a.query ?? "").slice(0, 40) || "一些内容"}」`;
    case "fetch_url":
    case "web_fetch":
      return `访问 ${a.url ? String(a.url).slice(0, 48) : "一个网页"}`;
    case "delete_file":
    case "move_file":
      return `动到文件 ${base(a.path)}（这类操作请看清路径再点）`;
    default:
      if (String(name || "").startsWith("browser_")) return "操作浏览器";
      return name ? `用一下 ${name}` : "做一件事";
  }
}

/** 从 args 里挑一行给人当证据（路径/命令/目标），没有就空。 */
export function askEvidence(argsRaw: unknown): string {
  const a = (argsRaw && typeof argsRaw === "object" ? argsRaw : {}) as Record<string, unknown>;
  for (const k of ["path", "url", "target", "command", "pattern", "query"]) {
    const v = a[k];
    if (typeof v === "string" && v.trim()) return v.length > 120 ? v.slice(0, 120) + "…" : v;
  }
  return "";
}

/**
 * 收件箱条目的标题中文化。
 *
 * 后端 `inbox.add_approval` 写死的标题是英文（`coworker/inbox.py:396`
 * `title=f"Run \`{tool_name}\`?"`），手机端不能直接把它端给用户。
 * 认得出来的形态就翻成中文；认不出来的一律原样保留（绝不猜，猜错比英文更糟）。
 */
export function inboxTitleZh(kind: string, title: string): string {
  const t = String(title || "");
  if (kind === "approval") {
    const m = t.match(/^Run\s*`([^`]+)`\?$/i) || t.match(/^Run\s*`([^`]+)`/i);
    if (m) return askPhrase(m[1], {});
    const s = t.match(/^Send\s+`([^`]+)`/i);
    if (s) return `发一下 ${base(s[1])}`;
  }
  return t;
}
