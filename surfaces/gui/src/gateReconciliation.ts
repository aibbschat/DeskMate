// 审批门对账：把「卡还在、事已经过去了」的僵尸卡收掉。
//
// 上游同名模块（andrewyng/openworker surfaces/gui/src/gateReconciliation.ts）。这里保留它
// 的两条铁律，并补了两种我们自己踩过的形态：
//
//   1. **完成即权威**（retireFinishedGate）：工具已经跑完/被拒，那张门卡就不该还能点。
//      绝不按标题、名单、"最后一个提议"去匹配 —— 新一轮的闸门可能长得一模一样。
//   2. **缺席不是证据**（reconcileResolvedGates）：信箱的 pending 列表里没有，不代表已解决
//      （proposal 事件可能先到、Inbox 行还没落库）。只有"确实读到 resolved"才能撤卡。
//   3. （新增）**重放去重**（dedupeGates）：断线重连/会话回放会把同一张卡再推一遍，
//      之前会得到两张一模一样的可点卡。同一个 toolCallId 只留第一张。
//   4. （新增）**轮次结束即失效**（expireOpenGates）：一轮结束还没人回答的门卡，后端已经
//      不再等它，留着只会让人点了没反应。
import type { InboxItem } from "./api";
import type { Item } from "./types";

/** 门卡家族：会阻塞在一处等人点头的那几种 item。 */
const GATE_KINDS = new Set<Item["kind"]>(["approval", "teamreq", "itemsreq", "dirreq", "planreq", "toolreq"]);

function isGate(item: Item): boolean {
  return GATE_KINDS.has(item.kind);
}

function isResolved(item: Item): boolean {
  return "resolved" in item && !!item.resolved;
}

function toolCallIdOf(item: Item): string | undefined {
  return "toolCallId" in item ? item.toolCallId : undefined;
}

/** 有没有"还没回答、且带 id"的门卡 —— 轮询兜底只在有卡时才多拉一次 resolved 列表。 */
export function hasOpenGates(items: Item[]): boolean {
  return items.some((item) => isGate(item) && !isResolved(item) && !!toolCallIdOf(item));
}

/** 完成即权威：name + toolCallId 精确命中才撤。给不出 id 就什么都不做。
 *
 *  只撤**还没回答**的卡：已经答过的那张是历史（Transcript 里带着"已批准/已拒绝"的 chip），
 *  把它删掉会让这一轮凭空少一段记录。上游的版本连历史一起删，我们这里不跟。 */
export function retireFinishedGate(items: Item[], name: string, toolCallId?: string): Item[] {
  if (!toolCallId) return items;
  const kind = name === "propose_team" ? "teamreq" : name === "propose_work_items" ? "itemsreq" : null;
  const remaining = items.filter(
    (item) =>
      !(
        isGate(item) &&
        !isResolved(item) &&
        ((kind && item.kind === kind) || (item.kind === "approval" && item.name === name)) &&
        toolCallIdOf(item) === toolCallId
      ),
  );
  return remaining.length === items.length ? items : remaining;
}

/** 轮询兜底：只有 Inbox 明确说 resolved 才撤卡。 */
export function reconcileResolvedGates(items: Item[], inbox: InboxItem[]): Item[] {
  const resolved = new Set(
    inbox.filter((i) => i.state === "resolved" && i.tool_call_id).map((i) => i.tool_call_id as string),
  );
  if (!resolved.size) return items;
  const remaining = items.filter((item) => {
    if (!isGate(item)) return true;
    const id = toolCallIdOf(item);
    return !(id && resolved.has(id));
  });
  // 这条对账跑在 10s 轮询里：没撤掉任何卡时必须返回**同一个数组**，否则每次轮询都会
  // 触发一次整块 transcript 重渲染。
  return remaining.length === items.length ? items : remaining;
}

/** 重放去重：同一个 toolCallId 只保留第一张未回答的门卡（已回答的历史卡不动）。 */
export function dedupeGates(items: Item[]): Item[] {
  const seen = new Set<string>();
  let dropped = false;
  const out = items.filter((item) => {
    if (!isGate(item) || isResolved(item)) return true;
    const id = toolCallIdOf(item);
    if (!id) return true;
    if (seen.has(id)) {
      dropped = true;
      return false;
    }
    seen.add(id);
    return true;
  });
  return dropped ? out : items;
}

/** 一轮结束还没有答案的门卡：标记为过期（expired），保留历史但不能再点。 */
export function expireOpenGates(items: Item[]): Item[] {
  let changed = false;
  const out = items.map((item) => {
    if (!isGate(item) || isResolved(item)) return item;
    if (item.kind === "approval") {
      changed = true;
      return { ...item, resolved: "expired" as const };
    }
    if (item.kind === "dirreq") {
      changed = true;
      return { ...item, resolved: "denied" as const };
    }
    if (item.kind === "planreq") {
      changed = true;
      return { ...item, resolved: "rejected" as const };
    }
    if (item.kind === "teamreq" || item.kind === "itemsreq") {
      changed = true;
      return { ...item, resolved: "rejected" as const };
    }
    if (item.kind === "toolreq") {
      changed = true;
      return { ...item, resolved: "skipped" as const };
    }
    return item;
  });
  return changed ? out : items;
}
