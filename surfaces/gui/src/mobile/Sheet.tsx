/**
 * 顶部下拉抽屉：待答（收件箱）/ 会话切换 / 连接自检。
 *
 * 抽屉而不是新屏：手机上「替她点一下」不该离开对话现场（D2 已拍：两屏 + 抽屉）。
 */
import { useEffect, useState } from "react";
import { getSessions, httpBase } from "../api";
import { hasRuntimeToken } from "../runtimeReady";
import type { InboxItem } from "../api";
import type { SessionInfo } from "../types";
import { actionsFor, resolutionFor } from "./inboxResolve";
import { inboxTitleZh } from "./askPhrase";
import { INSTALL_STATE_LABEL } from "./install";
import { useInstall } from "./useInstall";
import { pickSession } from "./session";
import type { BootFault } from "./useChat";

export type SheetKind = "none" | "inbox" | "sessions" | "debug";

interface SheetProps {
  kind: SheetKind;
  title: string;
  sub?: string;
  onClose: () => void;
  children: React.ReactNode;
}

export function Sheet({ kind, title, sub, onClose, children }: SheetProps) {
  const open = kind !== "none";
  return (
    <>
      <div className={"m-scrim" + (open ? " on" : "")} onClick={onClose} />
      <div className={"m-sheet" + (open ? " on" : "")} role="dialog" aria-hidden={!open}>
        <div className="m-sheet-grab" />
        <div className="m-sheet-h">
          <div>
            <div className="m-sheet-t">{title}</div>
            {sub && <div className="m-sheet-s">{sub}</div>}
          </div>
          <button type="button" className="m-sheet-x" onClick={onClose} aria-label="收起">
            ✕
          </button>
        </div>
        <div className="m-sheet-b">{children}</div>
      </div>
    </>
  );
}

export function InboxList({
  items,
  onResolve,
}: {
  items: InboxItem[];
  onResolve: (id: string, resolution: string) => void;
}) {
  const [free, setFree] = useState<Record<string, string>>({});
  if (!items.length)
    return <div className="m-empty-in">没有要她等的了。她跑任务时留下的「等你点头」都会出现在这里。</div>;
  return (
    <>
      {items.map((it) => {
        const acts = actionsFor(it);
        return (
          <div className="m-ask" key={it.id}>
            <div className="m-ask-k">{KIND_LABEL[it.kind] || it.kind}</div>
            <div className="m-ask-t">{inboxTitleZh(it.kind, it.title)}</div>
            {!!it.body && <div className="m-ask-b">{it.body}</div>}
            {!!it.session_title && <div className="m-ask-s">来自「{it.session_title}」</div>}
            <div className="m-ask-btns">
              {acts.map((a) => (
                <button
                  key={a.key}
                  type="button"
                  className={"btn" + (a.primary ? " pri" : "")}
                  onClick={() => {
                    const r = resolutionFor(it, a.key);
                    if (r !== null) onResolve(it.id, r);
                  }}
                >
                  {a.label}
                </button>
              ))}
            </div>
            {it.kind === "question" && it.allow_text !== false && (
              <div className="m-ask-free">
                <input
                  className="m-cm-in"
                  placeholder="或者自己回一句…"
                  value={free[it.id] || ""}
                  onChange={(e) => setFree((f) => ({ ...f, [it.id]: e.target.value }))}
                />
                <button
                  type="button"
                  className="btn"
                  disabled={!(free[it.id] || "").trim()}
                  onClick={() => (free[it.id] || "").trim() && onResolve(it.id, (free[it.id] || "").trim())}
                >
                  发送
                </button>
              </div>
            )}
          </div>
        );
      })}
      <div className="m-note">
        这里答的是「收件箱」，不需要手机一直连着；同一件事桌面也点了的话，先到先得、不会出错
      </div>
    </>
  );
}

const KIND_LABEL: Record<string, string> = {
  approval: "她想动手",
  question: "她在问你",
  directory: "她想访问文件夹",
  plan: "她提了个计划",
  notification: "她想说一声",
};

export function SessionsList({ current, onPick }: { current: string; onPick: (id: string, title: string) => void }) {
  const [list, setList] = useState<SessionInfo[] | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true;
    getSessions()
      .then((s) => alive && setList(s))
      .catch((e) => alive && setErr(String(e?.message || e)));
    return () => {
      alive = false;
    };
  }, []);
  if (err) return <div className="m-empty-in">读不到会话列表：{err}</div>;
  if (!list) return <div className="m-empty-in">在读…</div>;
  const mine = list.filter((s) => s.agent === "deskmate" && !s.archived);
  if (!mine.length) return <div className="m-empty-in">还没有聊过的会话</div>;
  return (
    <>
      <div className="m-card">
        {mine.map((s) => (
          <button
            key={s.session_id}
            type="button"
            className={"m-srow" + (s.session_id === current ? " on" : "")}
            onClick={() => onPick(s.session_id, s.title || "")}
          >
            <span className="m-srow-b">
              <span className="m-srow-n">{s.title || "未命名会话"}</span>
              <span className="m-srow-s">
                {s.messages} 条 · {fmtAgo(s.updated_at)}
                {!!s.attention && ` · ${s.attention} 件等你`}
              </span>
            </span>
            {s.session_id === current && <span className="m-srow-cur">在这</span>}
          </button>
        ))}
      </div>
      <div className="m-note">
        冷启动默认回到最近一条（{pickSession(mine)?.session_id || "—"}）。会话是新的小桔，不会替你开一堆空壳
      </div>
    </>
  );
}

export function DebugPanel({
  sessionId,
  connected,
  running,
  fault,
  items,
  inbox,
}: {
  sessionId: string;
  connected: boolean;
  running: boolean;
  fault: BootFault;
  items: number;
  inbox: number;
}) {
  const [probe, setProbe] = useState("…");
  const install = useInstall();
  const wait = (globalThis as any).__COWORKER_CFG_WAIT__ as string | undefined;
  useEffect(() => {
    let alive = true;
    fetch(`${httpBase()}/v1/health`)
      .then((r) => (alive ? setProbe(`${r.status} ${r.ok ? "ok" : "异常"}`) : null))
      .catch((e) => alive && setProbe("不可达：" + String(e?.message || e)));
    return () => {
      alive = false;
    };
  }, []);
  return (
    <div className="m-card">
      <KV k="接口地址" v={httpBase()} />
      <KV k="同源 token" v={hasRuntimeToken() ? "已取到（不进构建产物）" : "没有 —— 部署缺 runtime-config.json"} />
      {/* 首屏为什么可能先红一下：花园的请求必须等这一句有结论才放行（见 src/runtimeReady.ts） */}
      <KV k="配置落地" v={wait === "timeout" ? "超时先放行（网络慢，可能首屏读晚一次）" : wait === "settled" ? "在请求之前" : wait === "no-gate" ? "本地/桌面壳直注入" : "…"} />
      <KV k="GET /v1/health" v={probe} />
      <KV k="会话 socket" v={connected ? "已连（含 25s 保活 + 自动重连）" : "未连（会自动重试）"} />
      <KV k="当前会话" v={sessionId || "—"} />
      <KV k="本轮" v={running ? "她还在跑" : "待机"} />
      <KV k="消息条数" v={String(items)} />
      <KV k="待答" v={String(inbox)} />
      <KV k="装到主屏" v={INSTALL_STATE_LABEL[install.state] || install.state} />
      <KV k="manifest" v={install.probe ? `${install.probe.ok ? "✓" : "✗"} ${install.probe.note}` : "在读…"} />
      {install.probe && !install.probe.ok && (
        <div className="m-note">安卓不会出现「安装应用」就是这一行的问题：Chromium 只认 JSON 类的 Content-Type</div>
      )}
      {fault === "site" && <div className="m-note">站点口令没过：在浏览器里输一次 xj.aibbs.chat 的账号密码，再点上面的重试</div>}
      {fault === "token" && <div className="m-note">应用 token 不对：服务器 runtime-config.json 与 /etc/openworker/env 不一致，需重新部署前端</div>}
      {fault === "net" && <div className="m-note">连不上服务器：检查云端与隧道</div>}
      <div className="m-note">这一页只是自检，日常不用看；它存在的意义是出问题时能一眼分清「网络」还是「凭据」</div>
    </div>
  );
}

function KV({ k, v }: { k: string; v: string }) {
  return (
    <div className="m-kv">
      <span className="m-kv-k">{k}</span>
      <span className="m-kv-v">{v}</span>
    </div>
  );
}

function fmtAgo(iso: string | null): string {
  if (!iso) return "很久以前";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return String(iso).slice(0, 10);
  const min = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (min < 60) return `${min} 分钟前`;
  const h = Math.round(min / 60);
  if (h < 24) return `${h} 小时前`;
  return `${Math.round(h / 24)} 天前`;
}
