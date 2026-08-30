/**
 * SyncCard — 记忆同步状态卡（P2-7，本地优先 + 可选云同步）。
 *
 * 把「多端懂你延续」做成可感可看（8.0 红线）：显示启用状态 / 上次同步 / 可同步条目数，
 * 提供「立即同步」与「启用/停用」。同步目录默认读后端配置或环境变量 DESKMATE_SYNC_DIR
 * （指向 iCloud Drive / Dropbox / USB 即真实跨设备，零凭证）。API 失败静默降级。
 */

import { useCallback, useEffect, useState } from "react";
import { authedFetch, httpBase } from "../../api";
import { Icon } from "../Icon";

interface SyncStatus {
  enabled: boolean;
  backend: string;
  device_id: string;
  last_sync: string | null;
  local_files: number;
  remote_files: number;
  sync_dir: string | null;
}

interface SyncResult {
  ok?: boolean;
  pushed?: number;
  pulled?: number;
  skipped?: number;
  conflicts?: string[];
  errors?: string[];
}

const FALLBACK: SyncStatus = {
  enabled: false,
  backend: "noop",
  device_id: "",
  last_sync: null,
  local_files: 0,
  remote_files: 0,
  sync_dir: null,
};

function fmt(ts: string | null): string {
  return ts ? ts.replace("T", " ") : "尚未同步";
}

export function SyncCard() {
  const [status, setStatus] = useState<SyncStatus | null>(null);
  const [dir, setDir] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<SyncResult | null>(null);

  const refresh = useCallback(() => {
    authedFetch(`${httpBase()}/v1/deskmate/sync`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && typeof d === "object") {
          setStatus(d as SyncStatus);
          if (d.sync_dir) setDir(d.sync_dir);
        } else {
          setStatus(FALLBACK);
        }
      })
      .catch(() => setStatus(FALLBACK));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const onConfigure = (enabled: boolean) => {
    setBusy(true);
    setResult(null);
    authedFetch(`${httpBase()}/v1/deskmate/sync/configure`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled, dir: dir.trim() || undefined }),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d) setStatus(d as SyncStatus);
        refresh();
      })
      .catch(() => {/* 静默降级 */})
      .finally(() => setBusy(false));
  };

  const onSync = () => {
    setBusy(true);
    setResult(null);
    authedFetch(`${httpBase()}/v1/deskmate/sync/trigger`, { method: "POST" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setResult((d as SyncResult) ?? { errors: ["sync failed"] }))
      .catch(() => setResult({ errors: ["网络错误"] }))
      .finally(() => {
        setBusy(false);
        refresh();
      });
  };

  const s = status ?? FALLBACK;

  return (
    <div className="dm-sync-card">
      <div className="dm-sync-head">
        <Icon name="refresh" size={18} />
        <div>
          <div className="dm-sync-title">记忆同步</div>
          <div className="dm-sync-sub">
            {s.enabled ? "已启用 · 本地优先" : "未启用 · 仅本机"}
          </div>
        </div>
        <span className={`dm-sync-dot ${s.enabled ? "on" : "off"}`} />
      </div>

      <div className="dm-sync-stats">
        <div><span>可同步</span><b>{s.local_files}</b></div>
        <div><span>已上传</span><b>{s.remote_files}</b></div>
        <div><span>上次</span><b>{fmt(s.last_sync)}</b></div>
      </div>

      <label className="dm-sync-field">
        <span>同步目录（指向 iCloud/Dropbox/USB 即跨设备）</span>
        <input
          value={dir}
          placeholder="如 /Users/you/Library/Mobile Documents/com~apple~CloudDocs"
          onChange={(e) => setDir(e.target.value)}
        />
      </label>

      <div className="dm-sync-actions">
        {s.enabled ? (
          <>
            <button className="dm-sync-btn primary" onClick={onSync} disabled={busy} type="button">
              {busy ? "同步中…" : "立即同步"}
            </button>
            <button className="dm-sync-btn" onClick={() => onConfigure(false)} disabled={busy} type="button">
              停用
            </button>
          </>
        ) : (
          <button className="dm-sync-btn primary" onClick={() => onConfigure(true)} disabled={busy} type="button">
            启用同步
          </button>
        )}
      </div>

      {result && (
        <div className="dm-sync-result">
          {result.errors && result.errors.length > 0 ? (
            <span className="dm-sync-err">同步出错：{result.errors.join("；")}</span>
          ) : (
            <span>
              已上传 {result.pushed ?? 0} · 拉取 {result.pulled ?? 0} · 跳过 {result.skipped ?? 0}
              {result.conflicts && result.conflicts.length > 0
                ? ` · 冲突 ${result.conflicts.length} 项（以最新为准）`
                : ""}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
