/**
 * DeskMateProactiveInline — 活跃会话里的相关性注入（§11.4② 触发入口）。
 *
 * 仅当 agent === "deskmate" 且会话非 idle（App 已在此条件下挂载）时渲染。
 * 把最近对话作为 session_context 传给后端 POST /handoff，后端按相关性取最相关的
 * 主动产出 1–2 条；零关联则不渲染（静默，不阻塞聊天）。
 *
 * 瘦身卡三态（2026-11-03 改版，mock：ui-mocks/主动想法卡_瘦身三态_2026-11-03.html）：
 * ① 收起＝一行提示（「我有 N 件事想跟你对一下」）；
 * ② 展开＝每条一行摘要 + #id + 龄期角标；
 * ③ 点开单条＝详情限高 180px 内部滚动（她的理解/依据/原文全文都只活在这一层），
 *    下一步 S 步骤做成 chips（点击 prefill 指令进输入框），动作缩成右下小按钮。
 * 关键红线：**不再渲染后端拼好的长文 handoff.text**——两千字的条目曾经双份刷屏，
 * 全文从此只存在于详情滚动区与信箱里；任何一层都不允许占满全屏（卡体限高 40vh）。
 *
 * 「聊这个」（§11.16 层三）＝让她说：条目作为她的助手消息落本会话（server 端
 * compose + append + broadcast，短版开场，2026-11-03），不再把全文塞进消息流。
 * 「稍后」＝ack snoozed（3 天退避后自动复活轻递，修⑥）。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  getDeskmateHandoffContext,
  ackDeskmateHandoff,
  type HandoffItem,
  type NextStep,
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

const shortId = (id: string) => "#" + (id.split(":").pop()?.slice(0, 6) ?? "");

/** 一行摘要：取 content 首句（。！？/换行截断），上限 64 字。 */
function summarize(content: string): string {
  const first = (content || "").split(/[\n]/)[0].trim();
  const m = /^([\s\S]{1,64}?)(?:[。！？]|$)/.exec(first);
  const s = (m?.[1] ?? first).trim();
  return first.length > s.length || content.length > first.length ? `${s}…` : s;
}

/** 龄期（天）：created_at ISO → 与今天差多少天；解析失败返回 null。 */
function ageDays(createdAt: string): number | null {
  const t = Date.parse(createdAt || "");
  if (Number.isNaN(t)) return null;
  return Math.floor((Date.now() - t) / 86400000);
}

/** content 里的第一部作品名（《…》），用于头部「都跟《X》有关」。 */
function bookOf(content: string): string | null {
  const m = /《([^》]+)》/.exec(content || "");
  return m ? m[1] : null;
}

/** 当日展开偏好：今天第一次展开后，同日内的重挂载/重拉保持展开。 */
const OPEN_KEY = "dm:thought-open";
function today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function readOpenPref(): boolean {
  try {
    const raw = window.localStorage.getItem(OPEN_KEY);
    if (!raw) return false;
    const p = JSON.parse(raw) as { date?: string; open?: boolean };
    return p.date === today() && !!p.open;
  } catch {
    return false;
  }
}
function writeOpenPref(open: boolean): void {
  try {
    window.localStorage.setItem(OPEN_KEY, JSON.stringify({ date: today(), open }));
  } catch {
    /* 隐私模式等——本次会话内仍然生效 */
  }
}

export function DeskMateProactiveInline({
  items,
  onSpeak,
  onRunStep,
  onOpenMailbox,
}: {
  items: Item[];
  /** 「聊这个」让她说：条目作为她的助手消息落会话；返回 false 时调用方自行降级。 */
  onSpeak?: (item: HandoffItem) => Promise<boolean>;
  /** 点下一步 chip：把可执行指令 prefill 进输入框（开不开工由用户回车定夺）。 */
  onRunStep?: (step: NextStep) => void;
  /** 卡脚「去信箱看全部」：切到信箱 surface。 */
  onOpenMailbox?: () => void;
}) {
  const [handoff, setHandoff] = useState<{ items: HandoffItem[]; nextSteps: NextStep[] } | null>(null);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState<boolean>(readOpenPref);
  const [openIds, setOpenIds] = useState<Set<string>>(new Set());
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
          if (h && h.items.length > 0)
            setHandoff({ items: h.items, nextSteps: h.next_steps ?? [] });
          else setHandoff(null);
        })
        .catch(() => {});
    }, 1200);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [items]);

  const visible = useMemo(() => {
    if (!handoff) return [] as HandoffItem[];
    return handoff.items.filter((it) => !dismissed.has(it.id));
  }, [handoff, dismissed]);

  if (visible.length === 0) return null;

  // 头部一句话：条数 + 共同作品（全部条目提到同一本《…》才收敛，否则泛说）。
  const books = visible.map((it) => bookOf(it.content));
  const common = books.every((b) => b && b === books[0]) ? (books[0] as string) : null;
  const headLine = `我有 ${visible.length} 件事想跟你对一下${common ? `，都跟《${common}》有关` : ""}`;

  const toggle = () => {
    setOpen((o) => {
      writeOpenPref(!o);
      return !o;
    });
  };
  const toggleItem = (id: string) =>
    setOpenIds((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  const snooze = async (id: string) => {
    await ackDeskmateHandoff(id, "snoozed");
    setDismissed((s) => new Set(s).add(id));
  };

  return (
    <div className="dm-thought">
      <div className="dm-thought-head" onClick={toggle} role="button" tabIndex={0}>
        <span className="dm-thought-spark" aria-hidden>✦</span>
        <span className="dm-thought-line">
          {open ? headLine : `${headLine} —— 点开看看`}
        </span>
        <span className={"dm-thought-chev" + (open ? " open" : "")} aria-hidden>▶</span>
      </div>

      {open && (
        <div className="dm-thought-body">
          <ul className="dm-thought-list">
            {visible.map((it) => {
              const isOpen = openIds.has(it.id);
              const age = ageDays(it.created_at);
              const steps = (handoff?.nextSteps ?? []).filter((s) => s.item_id === it.id);
              return (
                <li key={it.id} className={"dm-thought-item" + (isOpen ? " open" : "")}>
                  <div className="dm-thought-row" onClick={() => toggleItem(it.id)} role="button" tabIndex={0}>
                    <span className="dm-thought-id">{shortId(it.id)}</span>
                    <span className="dm-thought-sum">{summarize(it.content)}</span>
                    {age !== null && age > 3 && (
                      <span className="dm-thought-age" title={age > 7 ? "超期：建议重新评估或放弃" : undefined}>
                        已等 {age} 天{age > 7 ? " · 超期" : ""}
                      </span>
                    )}
                    <span className="dm-thought-chev" aria-hidden>▶</span>
                  </div>

                  {isOpen && (
                    <div className="dm-thought-detail">
                      <div className="dm-thought-detail-inner">
                        {it.understanding ? (
                          <p><b>她的理解：</b>{it.understanding}</p>
                        ) : null}
                        {it.evidence ? (
                          <p><b>依据：</b>{it.evidence}</p>
                        ) : null}
                        <p>{it.content}</p>
                      </div>
                      {steps.length > 0 && (
                        <div className="dm-thought-steps">
                          {steps.map((s) => (
                            <button
                              key={s.step_key + s.item_id}
                              type="button"
                              className="dm-thought-step"
                              title={s.instruction}
                              onClick={(e) => {
                                e.stopPropagation();
                                onRunStep?.(s);
                              }}
                            >
                              {s.step_no ? `${s.step_no} · ` : ""}{s.title}
                            </button>
                          ))}
                        </div>
                      )}
                      <div className="dm-thought-actions">
                        <button
                          type="button"
                          className="dm-thought-later"
                          onClick={(e) => {
                            e.stopPropagation();
                            void snooze(it.id);
                          }}
                        >
                          稍后
                        </button>
                        <button
                          type="button"
                          className="dm-thought-chat"
                          onClick={(e) => {
                            e.stopPropagation();
                            if (!onSpeak) return;
                            // 让她说：端点成功 → 她的短版开场经 WS 落消息流；失败静默（不替她吹牛）。
                            void onSpeak(it).then((ok) => {
                              if (ok) setDismissed((s) => new Set(s).add(it.id));
                            });
                          }}
                        >
                          聊这个
                        </button>
                      </div>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>

          <div className="dm-thought-foot">
            <button type="button" className="dm-thought-more" onClick={() => onOpenMailbox?.()}>
              去信箱看全部 →
            </button>
            <button
              type="button"
              className="dm-thought-mute"
              onClick={() => {
                // 「这阵子都别递了」＝全部 snoozed（3 天退避后自动复活，不是永久拉黑）。
                visible.forEach((it) => void snooze(it.id));
              }}
            >
              这阵子都别递了
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
