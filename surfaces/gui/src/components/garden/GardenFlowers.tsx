/**
 * GardenFlowers — 花园花开横幅。
 *
 * 把「值得记的事」开成的花平铺展示：四花圃（记忆/日记/勋章/里程碑）统计 +
 * 最近的花（P2 分页：每页 12 朵，上一页/下一页，防花朵无限增长拖垮前端）。
 * 每朵花 hover/点击显示 title + 日期 + 原文摘录（quote 来自真实事件）。
 * P2 · 点亮：被点亮的花带 ⭐ 永久高亮，可点「点亮」把心仪的花留在视线里。
 *
 * 无花时显示引导文案（花园还很安静，去写点什么吧），不编造。
 */

import { useState } from "react";

export interface GardenFlower {
  flower_id: string;
  kind: string;
  bed: string;
  title: string;
  quote: string;
  date: string;
  source_ts: string;
  mood_tint: string;
  echo?: string;
}

export interface GardenStats {
  total?: number;
  by_bed?: Record<string, number>;
  by_kind?: Record<string, number>;
}

/** 花种 → emoji（与后端 FLOWER_INFO 语义一致）。 */
const KIND_EMOJI: Record<string, string> = {
  starflower: "✨",
  sunflower: "🌻",
  badge_flower: "🏅",
  dandelion: "🌼",
  bellflower: "🔔",
  hyacinth: "💐",
  sprout: "🌱",
  iris: "🪻",
  daisy: "🌼",
};

const BED_LABELS: Record<string, string> = {
  memory: "记忆圃",
  diary: "日记圃",
  badge: "勋章圃",
  milestone: "里程碑圃",
};

const BED_ORDER = ["memory", "diary", "badge", "milestone"];

const PAGE_SIZE = 12;

function fmtDate(d: string): string {
  if (!d) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(d);
  if (!m) return d;
  return `${Number(m[2])}月${Number(m[3])}日`;
}

export function GardenFlowers({
  flowers,
  stats,
  litIds,
  onLit,
}: {
  flowers: GardenFlower[];
  stats?: GardenStats | null;
  litIds?: string[] | null;
  onLit?: (flowerId: string) => void;
}) {
  const [openId, setOpenId] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const total = stats?.total ?? flowers.length;
  const litSet = new Set(litIds ?? []);
  const pageCount = Math.max(1, Math.ceil(flowers.length / PAGE_SIZE));
  const safePage = Math.min(page, pageCount - 1);
  const pageFlowers = flowers.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE);

  const toggle = (fid: string) => setOpenId(openId === fid ? null : fid);

  return (
    <div className="garden-flowers">
      <div className="garden-flowers-head">
        <span className="garden-flowers-title">
          花园里的花 <span className="garden-flowers-count">{total}</span>
        </span>
        <div className="garden-beds">
          {BED_ORDER.map((bed) => {
            const n = stats?.by_bed?.[bed] ?? 0;
            return (
              <span key={bed} className="garden-bed">
                <span className="garden-bed-dot" />
                {BED_LABELS[bed] ?? bed} {n > 0 ? n : ""}
              </span>
            );
          })}
        </div>
      </div>

      {flowers.length > 0 ? (
        <>
          <div className="garden-flower-row">
            {pageFlowers.map((f) => {
              const lit = litSet.has(f.flower_id);
              return (
                <div
                  key={f.flower_id}
                  className={`garden-flower${lit ? " is-lit" : ""}`}
                  onClick={() => toggle(f.flower_id)}
                  role="button"
                  tabIndex={0}
                  title={f.title}
                >
                  <span className="garden-flower-emoji">{KIND_EMOJI[f.kind] ?? "🌷"}</span>
                  {lit && <span className="garden-flower-lit" title="已被点亮">⭐</span>}
                  <span className="garden-flower-date">{fmtDate(f.date)}</span>
                  {openId === f.flower_id && (
                    <div className="garden-flower-tip">
                      <div className="garden-flower-tip-title">{f.title}</div>
                      {f.quote && <div className="garden-flower-tip-quote">{f.quote}</div>}
                      {f.echo && (
                        <div className="garden-flower-tip-echo">
                          <span className="garden-flower-tip-echo-by">小桔</span>
                          {f.echo}
                        </div>
                      )}
                      <div className="garden-flower-tip-meta">
                        {BED_LABELS[f.bed] ?? f.bed} · {fmtDate(f.date)}
                      </div>
                      <button
                        type="button"
                        className="garden-flower-lit-btn"
                        onClick={(e) => {
                          e.stopPropagation();
                          onLit?.(f.flower_id);
                        }}
                      >
                        {lit ? "⭐ 已点亮" : "点亮这朵花"}
                      </button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          {pageCount > 1 && (
            <div className="garden-flower-pager">
              <button
                type="button"
                className="garden-flower-pager-btn"
                disabled={safePage === 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
              >
                ‹ 上一页
              </button>
              <span className="garden-flower-pager-info">
                第 {safePage + 1} / {pageCount} 页
              </span>
              <button
                type="button"
                className="garden-flower-pager-btn"
                disabled={safePage >= pageCount - 1}
                onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
              >
                下一页 ›
              </button>
            </div>
          )}
        </>
      ) : (
        <div className="garden-empty">
          花园还很安静。去写点日记、聊聊心事，或者完成一次创作，这里就会开出一朵花。
        </div>
      )}
    </div>
  );
}
