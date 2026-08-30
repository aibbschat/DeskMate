/**
 * InsightPanel — 「它在学什么」：行为规律（P2-3）+ 后台整理（P2-1）。
 *
 * 三维审计（2026-08-30）发现：这两项后端就绪、端点也在，但**前端 0 消费**——
 * 「越用越懂你」最高阶的能力（从行为推断偏好）用户完全看不见，记忆自我进化
 * 也无从感知。本卡片把两者摆到桌宠首页，补上「看得到」这一环。
 *
 * 设计原则（8.0 红线 + 诚实）：
 * - 数据不足就明说「还在积累」，**不编造规律、不编造整理数字**
 * - API 失败静默降级，显示兜底状态，不报错打扰用户
 * - 暖米白 + 暖橙，与 PositioningCard / SyncCard 同一温度
 */

import { useEffect, useState } from "react";
import { authedFetch, httpBase } from "../../api";
import { Icon } from "../Icon";

interface BehaviorData {
  window_days: number;
  event_count: number;
  kinds?: string[];
  patterns: string[];
  context: string;
}

interface DreamData {
  last_run: string | null;
  due: boolean;
  merged: number;
  contradictions_resolved: number;
  stale_superseded: number;
  files_touched?: string[];
  report_path?: string;
}

export function InsightPanel() {
  const [beh, setBeh] = useState<BehaviorData | null>(null);
  const [dream, setDream] = useState<DreamData | null>(null);

  useEffect(() => {
    authedFetch(`${httpBase()}/v1/deskmate/behavior-patterns`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) =>
        setBeh(d && typeof d.event_count === "number" ? (d as BehaviorData) : null),
      )
      .catch(() => setBeh(null));

    authedFetch(`${httpBase()}/v1/deskmate/memory/dream`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) =>
        setDream(d && typeof d.due === "boolean" ? (d as DreamData) : null),
      )
      .catch(() => setDream(null));
  }, []);

  const patterns = beh?.patterns ?? [];

  return (
    <div className="dm-insight-card">
      <div className="dm-insight-head">
        <Icon name="sparkle" size={18} />
        <div>
          <div className="dm-insight-title">它在学什么</div>
          <div className="dm-insight-sub">后台沉淀 · 越用越懂</div>
        </div>
      </div>

      {/* 行为规律（P2-3：从行为推断偏好，「越用越懂你」最高阶） */}
      <div className="dm-insight-block">
        <div className="dm-insight-block-title">它发现的规律</div>
        {beh && beh.event_count > 0 ? (
          <>
            <div className="dm-insight-meta">
              近 {beh.window_days} 天记录了 {beh.event_count} 次行为
            </div>
            {patterns.length > 0 ? (
              <ul className="dm-insight-list">
                {patterns.map((p, i) => (
                  <li key={i}>{p}</li>
                ))}
              </ul>
            ) : (
              <div className="dm-insight-empty">
                样本还太少，规律没长出来——继续用就会慢慢显现。
              </div>
            )}
          </>
        ) : (
          <div className="dm-insight-empty">
            还没有行为记录。它在观察你的节奏，样本不足前不下结论。
          </div>
        )}
      </div>

      {/* 后台整理（P2-1：记忆自我进化 / 矛盾消解 / 过时更新） */}
      <div className="dm-insight-block">
        <div className="dm-insight-block-title">后台整理了什么</div>
        {dream ? (
          <>
            <div className="dm-insight-stats">
              <div>
                <span>去重合并</span>
                <b>{dream.merged ?? 0}</b>
              </div>
              <div>
                <span>矛盾消解</span>
                <b>{dream.contradictions_resolved ?? 0}</b>
              </div>
              <div>
                <span>过时归档</span>
                <b>{dream.stale_superseded ?? 0}</b>
              </div>
            </div>
            <div className="dm-insight-meta">
              {dream.last_run
                ? `上次整理：${String(dream.last_run).slice(0, 10)}`
                : "尚未整理过"}
              {dream.due ? " · 已到期，空闲时会自动整理" : " · 未到期"}
            </div>
          </>
        ) : (
          <div className="dm-insight-empty">暂无整理记录。</div>
        )}
      </div>
    </div>
  );
}
