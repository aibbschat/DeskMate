/**
 * DeskMateProactiveInline — 活跃会话里的相关性注入（§11.4② 触发入口，本轮新增）。
 *
 * 仅当 agent === "deskmate" 且会话非 idle（App 已在此条件下挂载）时渲染。
 * 把最近对话作为 session_context 传给后端 POST /handoff，后端按 bigram 重叠取最相关的
 * 主动产出 1–2 条；零关联则不渲染（静默，不阻塞聊天）。
 *
 * 形态同 DeskHome 的对接卡片：每条带证据 + 理解，可「聊这个」、「稍后」退避。
 * 「聊这个」（§11.16 层三）＝让她说：条目作为她的助手消息落本会话（server 端
 * compose + append + broadcast），不再把她的句子塞进用户输入框。
 * 非浮层、是活跃会话消息流下方的一条轻提示卡片——聊到某主题才递对应待办（根治「按时间念台账＝骚扰」）。
 */

import { useEffect, useRef, useState } from "react";
import {
  getDeskmateHandoffContext,
  ackDeskmateHandoff,
  type HandoffItem,
} from "../../api";
import type { Item } from "../../types";

// 取最近 N 条 user/assistant 文本拼成会话上下文（上限保护，避免把整段长稿塞进请求）。
function buildSessionContext(items: Item[], maxTurns = 8, cap = 2000): string {
  const turns = items
    .filter((it) => (it.kind === "user" || it.kind === "assistant") && "text" in it && it.text)
    .map((it) => (it as { text: string }).text)
    .slice(-maxTurns)
    .join("\n");
  return turns.length > cap ? turns.slice(-cap) : turns;
}

export function DeskMateProactiveInline({
  items,
  onSpeak,
}: {
  items: Item[];
  /** 「聊这个」让她说：条目作为她的助手消息落会话；返回 false 时调用方自行降级。 */
  onSpeak?: (item: HandoffItem) => Promise<boolean>;
}) {
  const [handoff, setHandoff] = useState<{ text: string; items: HandoffItem[] } | null>(null);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 会话消息变化 → 防抖 1.2s 后拉一次相关性注入（只递今日未递过的相关新条目）。
  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    const ctx = buildSessionContext(items);
    if (!ctx.trim()) {
      setHandoff(null);
      return;
    }
    timer.current = setTimeout(() => {
      getDeskmateHandoffContext(ctx)
        .then((h) => {
          if (h && h.items.length > 0) setHandoff(h);
          else setHandoff(null);
        })
        .catch(() => {});
    }, 1200);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [items]);

  const visibleItems = handoff
    ? handoff.items.filter((it) => !dismissed.has(it.id))
    : [];

  if (visibleItems.length === 0) return null;

  return (
    <div className="dm-handoff dm-handoff-inline">
      <p className="dm-handoff-text">
        {handoff!.text || "聊到这个，正好有几件事跟你对得上："}
      </p>
      <ul className="dm-handoff-list">
        {visibleItems.map((it) => (
          <li key={it.id} className="dm-handoff-item">
            <div className="dm-handoff-content">
              <span className="dm-handoff-id">#{it.id.split(":").pop()?.slice(0, 6)}</span>
              {it.content}
            </div>
            {it.evidence ? (
              <div className="dm-handoff-evidence">依据：{it.evidence}</div>
            ) : null}
            {it.understanding ? (
              <div className="dm-handoff-understanding">她的理解：{it.understanding}</div>
            ) : null}
            <div className="dm-handoff-actions">
              <button
                type="button"
                className="dm-handoff-chat"
                onClick={() => {
                  if (!onSpeak) return;
                  // 让她说：端点成功 → 她的消息经 WS 落消息流；失败静默（不替她吹牛）。
                  void onSpeak(it);
                }}
              >
                聊这个
              </button>
              <button
                type="button"
                className="dm-handoff-later"
                onClick={async () => {
                  await ackDeskmateHandoff(it.id, "snoozed");
                  setDismissed((s) => new Set(s).add(it.id));
                }}
              >
                稍后
              </button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
