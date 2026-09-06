/**
 * AgencyPanel — L2 自主层「小桔在忙」任务面板。
 *
 * 常驻展示小桔最近的自主行为（低风险已执行 / 高风险待你裁决的提议卡）。
 * 高风险提议卡提供 [执行][不用][以后别提] 三按钮，走 propose/resolve。
 *
 * 低频轮询（30s）即可——自主行为本就不频繁；失败静默，不阻断首页。
 */

import { useEffect, useState } from "react";
import {
  getDeskmateAgencyTasks,
  resolveDeskmateAgencyProposal,
  type AgencyTask,
} from "../../api";
import { Icon } from "../Icon";

const RISK_LABEL: Record<string, string> = {
  world_search: "主动信息摄入",
  external_digest: "外部信息消化",
  external_review: "外部信息裁决",
  rss_digest: "RSS 简报",
  note_organize: "笔记整理",
  write_file: "写文件",
  send_message: "发消息",
  delete: "删除",
};

const labelOf = (t: string) => RISK_LABEL[t] || t;

function fmtTs(ts: string): string {
  // 2026-09-06T11:41:08 → 09-06 11:41
  const m = ts.match(/(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!m) return ts;
  return `${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
}

export function AgencyPanel() {
  const [tasks, setTasks] = useState<AgencyTask[]>([]);
  const [pending, setPending] = useState<AgencyTask[]>([]);
  const [resolving, setResolving] = useState<string | null>(null);

  const load = async () => {
    const data = await getDeskmateAgencyTasks(20);
    setTasks(data.tasks || []);
    setPending(data.pending_proposals || []);
  };

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 30_000);
    return () => clearInterval(t);
  }, []);

  const resolve = async (id: string, decision: "executed" | "rejected" | "never") => {
    setResolving(id);
    try {
      await resolveDeskmateAgencyProposal(id, decision);
      await load();
    } finally {
      setResolving(null);
    }
  };

  const recent = tasks.filter((t) => t.status === "done").slice(0, 6);

  return (
    <div className="agency-panel" data-testid="agency-panel">
      <div className="agency-panel-head">
        <Icon name="sparkle" size={15} />
        <span>小桔在忙</span>
        <span className="agency-panel-sub">自主层</span>
      </div>

      {/* 高风险提议卡（待裁决） */}
      {pending.length > 0 && (
        <div className="agency-proposals">
          {pending.map((p) => (
            <div key={p.id} className="agency-proposal" data-testid="agency-proposal">
              <div className="agency-proposal-title">
                {labelOf(p.action_type)} · 需你确认
              </div>
              <div className="agency-proposal-desc">{p.summary || p.detail}</div>
              <div className="agency-proposal-actions">
                <button
                  className="agency-btn agency-btn-go"
                  disabled={resolving === p.id}
                  onClick={() => resolve(p.id, "executed")}
                >
                  执行
                </button>
                <button
                  className="agency-btn"
                  disabled={resolving === p.id}
                  onClick={() => resolve(p.id, "rejected")}
                >
                  不用
                </button>
                <button
                  className="agency-btn agency-btn-never"
                  disabled={resolving === p.id}
                  onClick={() => resolve(p.id, "never")}
                >
                  以后别提
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 最近自主行为（低风险已执行） */}
      {recent.length === 0 && pending.length === 0 ? (
        <div className="agency-empty">暂无自主行为记录</div>
      ) : (
        <ul className="agency-list">
          {recent.map((t) => (
            <li key={t.id} className="agency-item">
              <span className="agency-dot" />
              <span className="agency-item-main">
                <span className="agency-item-label">{labelOf(t.action_type)}</span>
                {t.topic && <span className="agency-item-topic">{t.topic}</span>}
                <span className="agency-item-sum">{t.summary}</span>
              </span>
              <span className="agency-item-ts">{fmtTs(t.ts)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
