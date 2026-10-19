/**
 * DeskHome — 桌搭模式空会话首页。
 *
 * 仅当 agent === "deskmate" 且会话为空（idle）时由 App.tsx 渲染。
 *
 * 首页只留「小桔在忙」。主动产出的待办不在这里挂清单，也不挂「去信箱」条幅
 * （萧哥 10-09：那条幅没必要）——信箱的入口就是侧栏那一行 ✉ 带未读数。
 *
 * 开聊即递（2026-10-19 修复首页主动开场白断链）：挂载时拉一次主动产出，有货就
 * **让她先开口**——条目作为她的助手消息落进本会话（speak 端点 append + broadcast，
 * WS 即时渲染），用户顺着接。不弹卡片等用户点、不往输入框塞话。
 *
 * 防重复三层：
 * ① 后端 build_handoff_opening 只把「今日未递过」标为 fresh（delivered_at=今日的
 *    返回快照里就带 today 前缀）——今天已递过的（含 inline 路径说过的）不复述；
 * ② localStorage 按日记她开口说过的 id——重挂载/重启不重复；
 * ③ 模块级 claim——StrictMode 双挂载/并发不重复说。
 * 失败静默：条目还在台账，聊到相关主题时走 inline 相关性路径补递。
 */

import { useEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import { AgencyPanel } from "./AgencyPanel";
import {
  getDeskmateHandoff,
  speakDeskmateHandoff,
  type HandoffItem,
} from "../../api";

/** 她今日已开口说过的条目 id（localStorage，按日过期）。 */
const SPOKEN_KEY = "dm:handoff-spoken";

/** 模块级 claim：同一 session 的条目只允许被说一次（跨挂载/StrictMode 双挂载有效）。 */
const claimedBySession = new Map<string, Set<string>>();

function _today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate(),
  ).padStart(2, "0")}`;
}

function _spokenToday(): Set<string> {
  try {
    const raw = window.localStorage.getItem(SPOKEN_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw) as { date?: string; ids?: unknown };
    if (parsed.date !== _today() || !Array.isArray(parsed.ids)) return new Set();
    return new Set(parsed.ids.filter((v): v is string => typeof v === "string"));
  } catch {
    return new Set(); // 存储损坏/隐私模式 → 当作没说（最坏多说一次）
  }
}

function _markSpoken(id: string): void {
  try {
    const ids = [..._spokenToday(), id];
    window.localStorage.setItem(SPOKEN_KEY, JSON.stringify({ date: _today(), ids }));
  } catch {
    /* 存储失败不阻断——下次挂载可能重复说一次，可接受 */
  }
}

/** 认领本 session 未说过的条目；返回本次真正要说的（其余已被认领 → 跳过）。 */
function _claim(sessionId: string, items: HandoffItem[]): HandoffItem[] {
  let set = claimedBySession.get(sessionId);
  if (!set) {
    set = new Set();
    claimedBySession.set(sessionId, set);
  }
  const fresh: HandoffItem[] = [];
  for (const it of items) {
    if (!set.has(it.id)) {
      set.add(it.id);
      fresh.push(it);
    }
  }
  return fresh;
}

function _release(sessionId: string, id: string): void {
  claimedBySession.get(sessionId)?.delete(id); // 说失败 → 放回，允许重试
}

export function DeskHome({
  sessionId,
  onIntentExecuted,
}: {
  sessionId: string;
  onIntentExecuted?: (intentId: string) => void;
}) {
  const { t, i18n } = useTranslation();

  // 每次回到首页（组件重新挂载）随机抽一条问候，制造新鲜感。
  const greeting = useMemo(() => {
    const list = t("deskmate.greetings", { returnObjects: true }) as unknown as string[];
    if (!Array.isArray(list) || list.length === 0) return "";
    return list[Math.floor(Math.random() * list.length)];
  }, [t, i18n.language]);

  // 开聊即递：挂载即拉一次主动产出，让她先开口（详见文件头注释）。
  const inFlight = useRef(false);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      let opening: Awaited<ReturnType<typeof getDeskmateHandoff>>;
      try {
        opening = await getDeskmateHandoff();
      } catch {
        return; // 网络/后端不可用 → 静默，不阻塞首页
      }
      if (cancelled || !opening || opening.items.length === 0) return;
      const today = _today();
      // 她今天还没说过的： delivered_at 为空或非今日（后端已按 created_at 倒序 +
      // 日上限封顶；今日已递过的——包括 inline 路径说的——跳过，不追重）。
      const todo = _claim(
        sessionId,
        opening.items.filter(
          (it) =>
            !_spokenToday().has(it.id) &&
            (!it.delivered_at || !it.delivered_at.startsWith(today)),
        ),
      );
      if (todo.length === 0 || inFlight.current) return;
      inFlight.current = true;
      try {
        // 顺序说（保持时间序）；每条落账成功才记 localStorage。
        // 不因组件卸载中断——她的消息落进会话是目的，用户回来时正好接着看。
        for (const it of todo) {
          const ok = await speakDeskmateHandoff(sessionId, it.id);
          if (ok) _markSpoken(it.id);
          else _release(sessionId, it.id);
        }
      } finally {
        inFlight.current = false;
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  return (
    <div className="hero">
      <h1 className="dm-home-greeting">
        <span className="mark">✦</span>
        {greeting}
      </h1>

      {/* L2 自主层「小桔在忙」——常驻展示自主行为 + 高风险提议卡待裁决 */}
      <AgencyPanel onJumpToChat={onIntentExecuted} />
    </div>
  );
}
