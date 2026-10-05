/**
 * 收件箱 REST 的 resolution 词表（**与 WS 的那套不一样**，搞错会静默变成"拒绝"）。
 *
 * 实证来源（2026-10-05 逐条核对，不是推断）：
 *  - `coworker/inbox.py:393-405` `inbox_approver`：`"always"→ALWAYS_TOOL`、
 *    `"allow"→ONCE`、**其它一律 DENY**。所以手机端若把 WS 的 `"once"` 发过来，
 *    服务端不认识 → 判成拒绝，而且不报错——这是最坏的那种 bug。
 *  - 桌面 `components/InboxItemCard.tsx:347-416` 实际发的就是 `allow` / `always_task` /
 *    `deny` / `JSON.stringify({granted,path,writable})` / `JSON.stringify({approved,mode|feedback})`
 *    / `seen`。手机端与桌面必须同源，故这里单列一份并配单测钉住。
 */
import type { InboxItem } from "../api";

export type ApprovalChoice = "allow" | "deny" | "always_task";

export const approvalResolution = (choice: ApprovalChoice): string => choice;

export function directoryResolution(item: InboxItem, granted: boolean): string {
  if (!granted) return JSON.stringify({ granted: false });
  const data = item.data || {};
  return JSON.stringify({
    granted: true,
    path: String(data.path || ""),
    writable: data.primary ? true : !!data.writable,
  });
}

export const planResolution = (approved: boolean): string =>
  approved
    ? JSON.stringify({ approved: true, mode: "interactive" })
    : JSON.stringify({ approved: false, feedback: "" });

export const notificationResolution = (): string => "seen";

/** 提问：单选就是答案字符串；分组提问是 JSON（键为 header 或问题） */
export const questionResolution = (answer: string): string => answer;

/** 这条该显示哪些按钮（手机端刻意不给大范围放行） */
export function actionsFor(item: InboxItem): Array<{ key: string; label: string; primary?: boolean }> {
  switch (item.kind) {
    case "approval":
      return [
        { key: "allow", label: "允许一次", primary: true },
        { key: "deny", label: "拒绝" },
      ];
    case "directory":
      return [
        { key: "grant", label: item.data?.primary ? "设为工作区" : "允许访问", primary: true },
        { key: "deny", label: "不给" },
      ];
    case "plan":
      return [
        { key: "yes", label: "照这个做", primary: true },
        { key: "no", label: "先别" },
      ];
    case "notification":
      return [{ key: "seen", label: "知道了", primary: true }];
    case "question":
      return (item.options || []).map((o, i) => ({
        key: `q${i}`,
        label: typeof o === "string" ? o : o.label,
        primary: i === 0,
      }));
    default:
      return [];
  }
}

/** 把一次点击翻译成要 POST 的 resolution 字符串 */
export function resolutionFor(item: InboxItem, key: string): string | null {
  if (item.kind === "approval") {
    if (key === "allow") return approvalResolution("allow");
    if (key === "deny") return approvalResolution("deny");
    return null;
  }
  if (item.kind === "directory") return directoryResolution(item, key === "grant");
  if (item.kind === "plan") return planResolution(key === "yes");
  if (item.kind === "notification") return notificationResolution();
  if (item.kind === "question") {
    const opts = item.options || [];
    const idx = Number(key.replace("q", ""));
    const o = opts[idx];
    if (o === undefined) return null;
    return questionResolution(typeof o === "string" ? o : o.label);
  }
  return null;
}
