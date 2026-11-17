import { useEffect, useState } from "react";
import {
  getLlmChannels,
  setLlmMemoryGroup,
  getLlmChannelAudit,
  getProactiveHeartbeat,
  type LlmChannelsStatus,
  type LlmChannelAuditRow,
  type ProactiveHeartbeat,
} from "../../api";

const CARD = "rounded-xl2 border border-line bg-panel";
const FIELD_LABEL = "text-[12.5px] font-medium text-ink";

/** 超过这个时长没真的思考，就当作「可能断了」标红（tick 30s 一跳，正常不会超过几分钟）。 */
const HEARTBEAT_STALE_SECONDS = 30 * 60;

/** 秒 → 「刚刚 / N 分钟前 / N 小时前 / N 天前」。 */
function ageText(seconds: number | null | undefined): string {
  if (seconds == null) return "还没有记录";
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

/** LLM 通道组：两个总开关（付费组 / 免费组）+ 组内自动排的状态与审计。
 *  她对你说的话走付费组；后台任务默认也走付费组（2026-10-13 起，免费池 prompt
 *  可能被上游记录）。免费组 2026-10-18 起仅剩云端本地 llama-server 兜底
 *  （外网免费车道全撤，见 PITFALLS §P4）。
 *  组内具体走哪条由deskmate.llm_channels.route 自动排（跳过熔断中的），
 *  本卡片只管「手动切组」+ 让状态可见（哪些候选可用/ 哪些在熔断）。 */
export function LlmChannelsCard() {
  const [status, setStatus] = useState<LlmChannelsStatus | null>(null);
  const [audit, setAudit] = useState<LlmChannelAuditRow[] | null>(null);
  const [heartbeat, setHeartbeat] = useState<ProactiveHeartbeat | null>(null);
  const [busy, setBusy] = useState(false);

  // 用普通函数而非 useCallback：本卡片只在自己挂载时取一次，
  // 不需要引用稳定化（少一个依赖数组陷阱）。
  const load = () => {
    void getLlmChannels().then((d) => d && setStatus(d));
    void getLlmChannelAudit(8).then((d) => d && setAudit(d));
    void getProactiveHeartbeat().then((d) => d && setHeartbeat(d));
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!status) return null;

  const pick = async (group: "paid" | "free") => {
    if (busy) return;
    setBusy(true);
    const ok = await setLlmMemoryGroup(group);
    if (ok) setStatus({ ...status, memory_group: group });
    else load();
    setBusy(false);
    // 切完顺手刷新审计：切换本身也会留一行（group_fallback等）
    void getLlmChannelAudit(8).then((d) => d && setAudit(d));
  };

  const grp = (g: "paid" | "free") => status[g];
  const active = status.memory_group;

  return (
    <div className={CARD + " p-4 mb-4"} data-testid="llm-channels-card">
      <div className={FIELD_LABEL}>小桔用哪个模型通道</div>
      <p className="text-x] text-muted mt-1 mb-1">
        两个总开关。组内自动排：某条通道限额了就换下一条，额度恢复会自动切回。
        两端（电脑/云端）共用同一份熔断记录。
      </p>

      {/* 两个总开关 */}
      <div className="flex gap-2 mt-3" role="group" aria-label="通道组">
        {(
          [
            ["free", "免费组", "仅剩云端本地模型兜底（电脑端不可用，会自动降回付费组）"],
            ["paid", "付费组", "她对你说的话走这里（质量与延迟更稳）"],
          ] as const
        ).map(([key, label, hint]) => {
          const g = grp(key);
          const on = active === key;
          return (
            <button
              key={key}
              type="button"
              disabled={busy}
              onClick={() => pick(key)}
              data-testid={`llm-group-${key}`}
              aria-pressed={on}
              className={[
                "flex-1 text-left px-3 py-2 rounded-lg border transition-colors disabled:opacity-50",
                on
                  ? "border-accent bg-accent-soft"
                  : "border-line bg-panel hover:border-line-strong",
              ].join(" ")}
            >
              <span
                className={[
                  "block text-ui font-medium",
                  on ? "text-accent" : "text-ink",
                ].join(" ")}
              >
                {label}
                {on ? " · 当前" : ""}
              </span>
              <span className="block text-x] text-muted mt-0.5">{hint}</span>
              <span className="block text-x] text-muted mt-1">
                {g.total} 条候选
                {g.open.length > 0
                  ? ` · ${g.open.length} 条限额中`
                  : g.available.length > 0
                    ? ` · ${g.available.length} 条可用`
                    : " · 暂无可用"}
              </span>
            </button>
          );
        })}
      </div>

      {/* 熔断中的通道：让「为什么在换模型」可见 */}
      {[...grp("paid").open, ...grp("free").open].length > 0 && (
        <div className="mt-3 pt-3 border-t border-line">
          <div className="text-x] text-muted">
            限额中（自动跳过，额度恢复后自动切回）
          </div>
          <ul className="mt-1.5 space-y-1" data-testid="llm-open-list">
            {[...grp("paid").open, ...grp("free").open].map((k) => (
              <li key={k} className="text-x] text-muted font-mono break-all">
                {k}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 后台心跳（2026-11-17）：她还在不在思考。断了要能看见，不能只靠「感觉她变安静」。 */}
      {heartbeat && (
        <div
          className="mt-3 pt-3 border-t border-line"
          data-testid="proactive-heartbeat"
        >
          <div className="text-x] text-muted">后台心跳（主动推理）</div>
          <div
            className={[
              "text-x] mt-1",
              heartbeat.judge_age_seconds == null
                ? "text-muted"
                : heartbeat.judge_age_seconds > HEARTBEAT_STALE_SECONDS
                  ? "text-danger"
                  : "text-ok",
            ].join(" ")}
          >
            上次思考：{ageText(heartbeat.judge_age_seconds)}
            {heartbeat.model ? `（${heartbeat.model}）` : ""}
            {typeof heartbeat.judge_calls_today === "number"
              ? ` · 今日 ${heartbeat.judge_calls_today} 次`
              : ""}
          </div>
          <div className="text-x] text-muted mt-0.5">
            上次心跳：{ageText(heartbeat.tick_age_seconds)}
            {heartbeat.last_skip_reason ? ` · ${heartbeat.last_skip_reason}` : ""}
          </div>
        </div>
      )}

      {/* 最近审计：为什么换了通道 */}
      {audit && audit.length > 0 && (
        <div className="mt-3 pt-3 border-t border-line">
          <div className="text-x] text-muted">最近的通道切换</div>
          <ul className="mt-1.5 space-y-1" data-testid="llm-audit-list">
            {audit.map((r, i) => (
              <li
                key={`${r.ts}-${i}`}
                className="text-x] text-muted flex items-baseline gap-2"
              >
                <span className="font-mono shrink-0">{r.ts.slice(11, 19)}</span>
                <span className="text-ink shrink-0">
                  {r.event === "open"
                    ? "限额"
                    : r.event === "close"
                      ? "恢复"
                      : r.event === "skip"
                        ? "跳过"
                        : r.event === "group_fallback"
                          ? "降级"
                          : r.event}
                </span>
                <span className="font-mono break-all truncate">
                  {r.channel || r.detail}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
