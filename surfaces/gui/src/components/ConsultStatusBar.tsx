// ConsultStatusBar — 专家咨询的后台任务状态条（2026-11-17）。
//
// 为什么需要它：咨询从「同步等结果」改成「后台跑」之后，小桔派完活就立刻回话，专家还要
// 跑几分钟。如果不把这段时间露出来，用户看到的就是「她说请了专家，然后没有下文」——
// 空等比慢更难受。所以这里常驻一条细行：「专家后台任务 · 人物专家 正在读《夜不收》· 已 3:12」。
//
// 数据有两个来源，同一个状态文件：
//   1) WS 事件 deskmate:consult（派发/完成时实时推送）；
//   2) GET /v1/deskmate/consult/runs（刷新页面、切回会话时恢复）。
// 已完成的条目留一小会儿就消失，不占据版面。

import { useEffect, useState } from "react";
import { getConsultRuns, type ConsultExpertProgress, type ConsultRunView } from "../api";

/** 完成态显示多久后收起（毫秒）。 */
const REVEAL_DONE_MS = 20_000;

/** 秒 → mm:ss / h:mm:ss。 */
function clock(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return `${h ? `${h}:` : ""}${mm}:${String(s).padStart(2, "0")}`;
}

/** 累计读入字数 → 「9.6 万字」/「3200 字」。 */
function amount(chars?: number): string {
  const n = Number(chars ?? 0);
  if (!n) return "";
  return n >= 10000 ? `${(n / 10000).toFixed(1)} 万字` : `${n} 字`;
}

/**
 * 一位专家此刻在做什么。**只翻译后端记录的事实**（哪个工具在跑、哪一章、读了几次），
 * 不做「已完成 37%」这类推算——百分比一旦是猜的，用户就会据此判断「快了」。
 */
function expertState(p: ConsultExpertProgress | undefined, book: string): string {
  const chapter = (p?.chapter || "").trim();
  const reads = Number(p?.reads ?? 0);
  if (p?.busy === "read_book") return chapter ? `正在读「${chapter}」` : "正在通读全书";
  if (p?.busy === "list_books") return "正在翻书架";
  if (reads > 0) {
    const sum = amount(p?.chars);
    return `已读 ${reads} 次${sum ? `（${sum}）` : ""}，正在梳理`;
  }
  // 还没动工具：只能说「在看这本书」，别说「在读」——读没读我们还不知道。
  return book ? `正在看《${book}》` : "正在看稿";
}

/** 专家 id 与显示名按位置对齐（后端两个数组同序）。 */
function expertPairs(run: ConsultRunView): { id: string; name: string }[] {
  const ids = run.experts || [];
  const names = run.expert_names || [];
  const n = Math.max(ids.length, names.length);
  return Array.from({ length: n }, (_, i) => ({
    id: String(ids[i] ?? `#${i}`),
    name: String(names[i] ?? "专家"),
  }));
}

function label(run: ConsultRunView, now: number): string {
  const names = (run.expert_names || []).join("、") || "专家";
  const book = (run.book || "").trim();
  // running 的 elapsed 必须**现算**：服务端下发时的 elapsed 是派发瞬间的 0，
  // 每秒 tick 只负责重渲染——若直接读字段，秒表会永远停在 0:00。
  const elapsed =
    run.status === "running"
      ? Math.max(0, now - Number(run.started_at || now))
      : Number(run.elapsed ?? 0);
  switch (run.status) {
    case "running": {
      const pairs = expertPairs(run);
      // 单专家：直接说他此刻在干什么（读第几章 / 读了几次）。
      // 多专家：各自状态放到下面一行分别显示，这里只报总账。
      if (pairs.length === 1) {
        return `${names} ${expertState(run.progress?.[pairs[0].id], book)} · 已 ${clock(elapsed)}`;
      }
      const doing = book ? `正在读《${book}》` : "正在看稿";
      return `${names} ${doing} · 已 ${clock(elapsed)}`;
    }
    case "done":
      return `${names} 已交付 · 用时 ${clock(elapsed)} · 她马上会来汇报`;
    case "timeout":
      return `${names} 超时，只交出了已产出的部分`;
    case "failed":
      return `${names} 调用失败${run.error ? `：${run.error}` : ""}`;
    case "interrupted":
      return `${names} 中断（进程重启），未产出结果`;
    default:
      return `${names} · ${run.status}`;
  }
}

function tone(status: string): string {
  if (status === "running") return "text-muted";
  if (status === "done") return "text-muted";
  return "text-warnInk";
}

export function ConsultStatusBar({ sessionId }: { sessionId: string }) {
  const [runs, setRuns] = useState<Record<string, ConsultRunView>>({});
  const [, setNowTick] = useState(0);

  // 挂载 / 切会话：把「刷新前就在跑」的任务捞回来（WS 只在连着的时候有用）。
  useEffect(() => {
    let alive = true;
    if (!sessionId) return;
    getConsultRuns(sessionId)
      .then((data) => {
        if (!alive || !data) return;
        setRuns((prev) => {
          const next = { ...prev };
          for (const r of data.active) next[r.id] = r;
          return next;
        });
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [sessionId]);

  // 实时事件（App.tsx 把 WS 的 deskmate_consult_* 转成这个 DOM 事件）。
  useEffect(() => {
    const onConsult = (ev: Event) => {
      const detail = (ev as CustomEvent).detail as (ConsultRunView & { phase?: string }) | null;
      if (!detail?.id) return;
      setRuns((prev) => ({ ...prev, [detail.id]: { ...prev[detail.id], ...detail } }));
    };
    window.addEventListener("deskmate:consult", onConsult as EventListener);
    return () => window.removeEventListener("deskmate:consult", onConsult as EventListener);
  }, []);

  // 每秒一次：running 的计时往前走；完成态留 REVEAL_DONE_MS 后收起。
  // 只有「有任务」时才挂 interval —— 空组件不该在后台每秒 setState 一次。
  const busy = Object.values(runs).some((r) => r.status === "running" || !r.finished_at);
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(() => {
      const now = Date.now() / 1000;
      setRuns((prev) => {
        let changed = false;
        const next: Record<string, ConsultRunView> = {};
        for (const [id, run] of Object.entries(prev)) {
          const finished = Number(run.finished_at ?? 0);
          if (run.status !== "running" && finished && (now - finished) * 1000 > REVEAL_DONE_MS) {
            changed = true;
            continue;
          }
          next[id] = run;
        }
        return changed ? next : prev;
      });
      setNowTick((n) => n + 1);
    }, 1000);
    return () => clearInterval(timer);
  }, [busy]);

  const list = Object.values(runs).sort((a, b) => {
    const rank = (r: ConsultRunView) => (r.status === "running" ? 0 : 1);
    return rank(a) - rank(b) || (a.started_at || 0) - (b.started_at || 0);
  });
  if (!list.length) return null;
  const now = Date.now() / 1000;

  // 对齐契约：这条是 Composer 的兄弟节点，而 Composer 的外层同样是**全宽**容器，
  // 靠内部的 `max-w-3xl mx-auto` 自己居中（见 Composer.tsx 的 composer-wrap + 内层）。
  // 这里不复制这一层，就会贴着窗口最左边、压在壁纸插画上（2026-11-17 实盘如此）。
  // px-6 与 composer-wrap 的左右内边距同值，窄窗下才和输入框同边。
  return (
    <div className="px-6">
      <div className="mx-auto flex max-w-3xl flex-col gap-1 pb-1.5 text-meta" data-testid="consult-status">
        {list.map((run, i) => {
          const pairs = run.status === "running" ? expertPairs(run) : [];
          return (
            <div key={run.id} className="flex flex-col gap-0.5">
              <div
                className={`flex items-center gap-2 ${tone(run.status)}`}
                data-testid={`consult-${run.status}`}
                title={run.excerpt || run.book || ""}
              >
                {run.status === "running" ? (
                  <span className="waiting-spinner" />
                ) : (
                  <span
                    aria-hidden
                    className="inline-block w-1.5 h-1.5 rounded-full flex-none"
                    style={{ background: run.status === "done" ? "var(--ok)" : "var(--warn-ink)" }}
                  />
                )}
                <span>
                  {i === 0 ? "专家后台任务 · " : ""}
                  {label(run, now)}
                </span>
              </div>
              {/* 多位专家时各自在干什么分开说——合成一句话只会剩个「正在读稿」。 */}
              {pairs.length > 1 &&
                pairs.map((p) => (
                  <div
                    key={p.id}
                    className="pl-5 text-muted"
                    data-testid={`consult-progress-${p.id}`}
                  >
                    {p.name} {expertState(run.progress?.[p.id], (run.book || "").trim())}
                  </div>
                ))}
            </div>
          );
        })}
      </div>
    </div>
  );
}
