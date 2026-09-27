/**
 * AgencyPanel — L2 自主层「小桔在忙」面板。
 *
 * 左侧：小桔坐在桌前的两帧动画（打字 ↔ 思考，淡入淡出循环）。
 * 右侧：半透明全息屏，实时展示自主行为记录 + 高风险提议卡（待裁决）。
 * 低频轮询（30s）即可——自主行为本就不频繁；失败静默，不阻断首页。
 */

import { useEffect, useState } from "react";
import {
  getDeskmateAgencyTasks,
  resolveDeskmateAgencyProposal,
  type AgencyTask,
} from "../../api";
import xiaojuTyping from "../../assets/xiaoju-busy-typing-transparent.png";
import xiaojuThinking from "../../assets/xiaoju-busy-thinking-transparent.png";

const RISK_LABEL: Record<string, string> = {
  world_search: "主动信息摄入",
  external_digest: "外部信息消化",
  observation_diary: "观察日记",
  worldview_reflection: "世界观回望",
  ai_circle_ingestion: "AI 圈摄入",
  xiaosheng_ingestion: "萧盛影响摄入",
  ai_circle_share: "AI 圈自主发声",
  ai_circle_column: "AI 圈公开手记",
  ai_circle_reply: "AI 圈真实回复",
  external_review: "外部信息裁决",
  memory_subject_conflict: "记忆主体冲突",
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
      {/* 左侧：小桔忙碌动画 */}
      <div className="agency-xiaoju">
        <div className="agency-xiaoju-stage">
          <img
            src={xiaojuTyping}
            className="agency-xiaoju-frame agency-xiaoju-typing"
            alt="小桔打字中"
            draggable={false}
          />
          <img
            src={xiaojuThinking}
            className="agency-xiaoju-frame agency-xiaoju-thinking"
            alt="小桔思考中"
            draggable={false}
          />
        </div>
        <div className="agency-xiaoju-label">
          <span className="agency-xiaoju-label-dot" />
          小桔在忙
        </div>
      </div>

      {/* 右侧：全息屏 */}
      <div className="agency-holoscreen">
        <div className="holo-header">
          <span className="holo-title">自主层 · 实时</span>
          <span className="holo-live">
            <span className="holo-live-dot" />
            LIVE
          </span>
        </div>

        {/* 高风险提议卡（待裁决） */}
        {pending.length > 0 && (
          <div className="holo-proposals">
            {pending.map((p) => (
              <div key={p.id} className="holo-proposal" data-testid="agency-proposal">
                <div className="holo-proposal-title">
                  <span className="holo-proposal-icon">!</span>
                  {labelOf(p.action_type)} · 需你确认
                </div>
                <div className="holo-proposal-desc">{p.summary || p.detail}</div>
                <div className="holo-proposal-actions">
                  <button
                    className="holo-btn holo-btn-go"
                    disabled={resolving === p.id}
                    onClick={() => resolve(p.id, "executed")}
                  >
                    执行
                  </button>
                  <button
                    className="holo-btn"
                    disabled={resolving === p.id}
                    onClick={() => resolve(p.id, "rejected")}
                  >
                    不用
                  </button>
                  <button
                    className="holo-btn holo-btn-never"
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

        {/* 最近自主行为 */}
        {recent.length === 0 && pending.length === 0 ? (
          <div className="holo-empty">小桔暂时在发呆……</div>
        ) : (
          <ul className="holo-list">
            {recent.map((t) => (
              <li key={t.id} className="holo-item">
                <span className="holo-item-dot" />
                <span className="holo-item-main">
                  <span className="holo-item-label">{labelOf(t.action_type)}</span>
                  {t.topic && <span className="holo-item-topic">{t.topic}</span>}
                  <span className="holo-item-sum">{t.summary}</span>
                </span>
                <span className="holo-item-ts">{fmtTs(t.ts)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
