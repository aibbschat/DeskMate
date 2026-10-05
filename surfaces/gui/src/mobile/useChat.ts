/**
 * 手机端对话状态（一个 hook 管住：选会话 → 拉历史 → 建 socket → 收发 → 审批 → 收件箱）。
 *
 * 三条来自设计稿 §三 的硬约束：
 *  - 冷启动**先续聊**最近一条 deskmate 会话（session_id 是客户端铸造的，每次新建会在
 *    Recents 里长草）；
 *  - 收发只有 WS，没有 REST 兜底 → 保活/重连/补发全部交给 `api.ts` 的 `Session`
 *    （2026-10-05 起它自带 25s ping + 指数退避 + CLOSED 入 outbox），手机端不另写一套；
 *  - 审批优先走 REST 收件箱（`getInbox` / `resolveInboxItem`），这样手机**不必常驻 socket**
 *    也能替她点头，切回前台拉一次即可。
 *
 * D8 已拍：手机端**不做**主动性轮询（`/proactive/tick` 不在这里出现）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Session,
  authedFetch,
  getInbox,
  getSessionMessages,
  httpBase,
  resolveInboxItem,
} from "../api";
import type { InboxItem } from "../api";
import type { ApprovalDecision, SessionInfo, WsEvent } from "../types";
import { itemsFromMessages } from "../itemsFromMessages";
import { applyEvent, emptyChat, resolveLast, withLocalUser, type ChatState } from "./reducer";
import { classify401, newSessionId, pickSession, type AuthFault } from "./session";

export type BootFault = AuthFault;

/** 冷启动：挑一条可续的会话；REST 失败时区分「凭据不对」与「连不上服务器」。 */
async function bootSession(): Promise<{ id: string; title: string; fault: BootFault }> {
  let res: Response;
  try {
    res = await authedFetch(`${httpBase()}/v1/sessions`);
  } catch {
    return { id: newSessionId(), title: "", fault: "net" };
  }
  // 401/403 单独判：不判的话 401 的空 body 会被当成「没有会话」，于是手机会默默新建一条
  // 会话、再被 WS 以 1008 拒掉，用户看到的是「她不理我」而不是「凭据不对」。
  // 而且要看 Www-Authenticate 分清是站点口令还是应用 token（两者处置完全不同）。
  if (res.status === 401 || res.status === 403) return { id: newSessionId(), title: "", fault: classify401(res) };
  if (!res.ok) return { id: newSessionId(), title: "", fault: "net" };
  let list: SessionInfo[] = [];
  try {
    list = ((await res.json()).sessions ?? []) as SessionInfo[];
  } catch {
    list = [];
  }
  const pick = pickSession(list);
  return pick ? { id: pick.session_id, title: pick.title || "", fault: null } : { id: newSessionId(), title: "", fault: null };
}

export function useChat() {
  const [state, setState] = useState<ChatState>(emptyChat);
  const [sessionId, setSessionId] = useState("");
  const [title, setTitle] = useState("");
  const [fault, setFault] = useState<BootFault>(null);
  const [booting, setBooting] = useState(true);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [inbox, setInbox] = useState<InboxItem[]>([]);
  const sessRef = useRef<Session | null>(null);

  // ---- 冷启动选会话（失败可重试，绝不一次 401 就永久锁死）----
  // 真机踩过的坑：standalone 启动时站点口令还没提交，第一次 /v1/sessions 必然 401，
  // 旧实现把 fault 定住、socket 又因 fault 直接不拨 → 界面永远停在「凭据不对」，
  // 而用户其实只要输一次口令就行。现在：有界自动重试 + 回到前台立刻重试 + 手动重试。
  const [bootTick, setBootTick] = useState(0);
  const tries = useRef(0);
  useEffect(() => {
    let alive = true;
    bootSession().then((r) => {
      if (!alive) return;
      setSessionId(r.id);
      setTitle(r.title);
      setFault(r.fault);
      setBooting(false);
      if (r.fault && tries.current < 6) {
        tries.current += 1;
        window.setTimeout(() => alive && setBootTick((n) => n + 1), Math.min(1500 * 2 ** tries.current, 20000));
      }
    });
    return () => {
      alive = false;
    };
  }, [bootTick]);
  useEffect(() => {
    const onVis = () => {
      if (document.visibilityState === "visible") {
        tries.current = 0;
        setBootTick((n) => n + 1);
      }
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);
  const retryBoot = useCallback(() => {
    tries.current = 0;
    setBooting(true);
    setBootTick((n) => n + 1);
  }, []);

  // ---- 历史（REST 兜底：GET /v1/sessions/{id}/messages）----
  useEffect(() => {
    if (!sessionId) return;
    let alive = true;
    setHistoryLoading(true);
    getSessionMessages(sessionId)
      .then((msgs) => {
        if (!alive) return;
        setState((s) => ({ ...s, items: itemsFromMessages(msgs) }));
      })
      .catch(() => {
        /* 新会话服务端还没有记录 → 保持空，不是错误 */
      })
      .finally(() => alive && setHistoryLoading(false));
    return () => {
      alive = false;
    };
  }, [sessionId]);

  // ---- socket（保活/重连/补发都在 Session 里）----
  useEffect(() => {
    if (!sessionId || fault === "site" || fault === "token") return;
    const session = new Session(sessionId, "", "deskmate", {
      onEvent: (ev: WsEvent) => setState((s) => applyEvent(s, ev)),
      onOpen: () => setState((s) => ({ ...s, connected: true })),
      onClose: () => setState((s) => ({ ...s, connected: false })),
    });
    sessRef.current = session;
    return () => {
      session.close();
      sessRef.current = null;
    };
  }, [sessionId, fault]);

  // ---- 收件箱：挂载 + 切回前台 + 每轮结束 + 答完一条 时刷新（不做常驻轮询）----
  const refreshInbox = useCallback(async () => {
    try {
      const items = await getInbox(undefined, "pending");
      setInbox(Array.isArray(items) ? items : []);
    } catch {
      /* 网络故障时保留上一次的结果，不清空成「没有要点的」 */
    }
  }, []);
  useEffect(() => {
    void refreshInbox();
    const onVis = () => document.visibilityState === "visible" && void refreshInbox();
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [refreshInbox]);
  const running = state.running;
  useEffect(() => {
    if (!running) void refreshInbox(); // 刚跑完一轮：她可能又留了新的 ask
  }, [running, refreshInbox]);

  // ---- 动作 ----
  const send = useCallback((text: string, attachments?: unknown[]) => {
    const t = text.trim();
    if (!t) return;
    setState((s) => withLocalUser(s, t));
    sessRef.current?.userMessage(t, attachments);
  }, []);

  const approve = useCallback((decision: ApprovalDecision) => {
    sessRef.current?.approve(decision);
    setState((s) => ({ ...s, items: resolveLast(s.items, "approval", decision) }));
  }, []);

  const answerQuestion = useCallback((answer: string) => {
    sessRef.current?.respondQuestion(answer);
    setState((s) => ({ ...s, items: resolveLast(s.items, "question", answer) }));
  }, []);

  const respondDir = useCallback((granted: boolean) => {
    sessRef.current?.respondDirectory(granted);
    setState((s) => ({ ...s, items: resolveLast(s.items, "dirreq", granted ? "granted" : "denied") }));
  }, []);

  const respondPlan = useCallback((approved: boolean) => {
    sessRef.current?.respondPlan(approved);
    setState((s) => ({ ...s, items: resolveLast(s.items, "planreq", approved ? "approved" : "rejected") }));
  }, []);

  const retry = useCallback(() => sessRef.current?.retry(), []);
  const interrupt = useCallback(() => sessRef.current?.interrupt(), []);

  /** 收件箱里答一条（REST，幂等、先到先得）；答完立刻刷新计数 */
  const resolveAsk = useCallback(
    async (id: string, resolution: string) => {
      try {
        await resolveInboxItem(id, resolution);
      } finally {
        void refreshInbox();
      }
    },
    [refreshInbox],
  );

  /** 换一条会话续聊（会话切换抽屉用） */
  const openSession = useCallback((id: string, name: string) => {
    setState({ ...emptyChat(), connected: true });
    setSessionId(id);
    setTitle(name);
    setFault(null);
  }, []);

  return {
    state,
    sessionId,
    title,
    fault,
    booting,
    historyLoading,
    inbox,
    refreshInbox,
    resolveAsk,
    send,
    approve,
    answerQuestion,
    respondDir,
    respondPlan,
    retry,
    interrupt,
    openSession,
    retryBoot,
    /** socket 是否真的活着（决定发送键与断线条） */
    live: state.connected,
  };
}
