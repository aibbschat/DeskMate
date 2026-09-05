/**
 * GardenTree — 花园的「树」（P2）：成长线 + 每日浇水 + 年花 + 花园年鉴。
 *
 * 全部从真实数据派生，零 LLM：
 * - 成长树：花朵总数 → 树等级（一粒种子 → 亭亭如盖），进度 = 距下一档还差几朵。
 * - 每日浇水：visit 自动结算（幂等），显示「今日已浇水 · 连续 N 天」。
 * - 年花：每一年的第一次回园留下一枚年轮，显示今年花数（从 source_ts 统计）。
 * - 花园年鉴：花朵超封顶后最旧的归档于此，按年份回看。
 *
 * 数据来自 useGarden 的 p2 快照；年鉴详情单独拉取，失败静默。
 */

import { useEffect, useState } from "react";
import { authedFetch, httpBase } from "../../api";
import { GardenTreeSVG } from "./GardenTreeSVG";
import { seasonOf, type SeasonKey } from "./SeasonTheme";
import type { GardenP2 } from "./useGarden";
import { treeProgressPct } from "./useGarden";

export function GardenTree({ p2 }: { p2?: GardenP2 | null }) {
  const tree = p2?.tree;
  const water = p2?.water;
  const years = p2?.years ?? {};
  const yearFlowers = p2?.year_flowers ?? [];
  const archive = p2?.archive;
  const seasonKey: SeasonKey = seasonOf();
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<Array<Record<string, unknown>> | null>(null);

  // 年鉴详情：展开时拉取一次，失败静默。
  useEffect(() => {
    if (!open || detail !== null) return;
    let alive = true;
    authedFetch(`${httpBase()}/v1/deskmate/garden/archive`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (alive && d) setDetail(d.flowers ?? []);
      })
      .catch(() => {
        /* 静默降级 */
      });
    return () => {
      alive = false;
    };
  }, [open, detail]);

  if (!tree && !water && yearFlowers.length === 0 && !archive) return null;

  const progressText = tree?.next
    ? `再开 ${Math.max(0, (tree.next.threshold ?? 0) - (tree?.count ?? 0))} 朵，就长成「${tree.next.title}」`
    : "花满枝头，它就是花园的样子了";
  const latestYear = yearFlowers[yearFlowers.length - 1];

  return (
    <div className="garden-tree">
      <div className="garden-tree-card">
        <div className="garden-tree-emblem" aria-hidden>
          {/* 真实 SVG 树：随成长等级生长、随季节变色（替换进度条/emoji） */}
          <GardenTreeSVG stage={tree?.key ?? "seed"} season={seasonKey} size={110} />
        </div>
        <div className="garden-tree-main">
          <div className="garden-tree-title">
            花园的树 · <span className="garden-tree-stage">{tree?.title ?? "一粒种子"}</span>
            <span className="garden-tree-count">{tree?.count ?? 0} 朵花</span>
          </div>
          <div className="garden-tree-desc">{tree?.desc ?? ""}</div>
          <div className="garden-tree-progress" role="progressbar" aria-valuenow={treeProgressPct(tree)} aria-valuemin={0} aria-valuemax={100}>
            <div
              className="garden-tree-progress-fill"
              style={{ width: `${treeProgressPct(tree)}%` }}
            />
          </div>
          <div className="garden-tree-progress-text">{progressText}</div>

          {water && (
            <div className="garden-tree-water">
              <span className="garden-tree-water-icon">💧</span>
              <span>
                {water.watered
                  ? "今天刚给花园浇过水"
                  : water.today
                    ? "今天已浇过水"
                    : "今日浇水待结算"}
              </span>
              <span className="garden-tree-water-streak">连续 {water.streak ?? 0} 天</span>
            </div>
          )}

          <div className="garden-tree-years">
            {latestYear && (
              <span className="garden-tree-year">
                {latestYear.year} 年第一次回园是 {latestYear.first_visit_date?.slice(5) ?? ""}，
                今年已开 {years[latestYear.year] ?? 0} 朵花
              </span>
            )}
          </div>

          {archive && archive.count > 0 && (
            <button type="button" className="garden-tree-archive-toggle" onClick={() => setOpen(!open)}>
              📖 花园年鉴 · 已归档 {archive.count} 朵{open ? " ▴" : " ▾"}
            </button>
          )}
          {open && (
            <div className="garden-tree-archive">
              {(archive?.years ?? []).map((y) => (
                <div key={y.year} className="garden-tree-archive-row">
                  <span className="garden-tree-archive-year">{y.year} 年</span>
                  <span className="garden-tree-archive-count">{y.count} 朵</span>
                  {y.sample_title && <span className="garden-tree-archive-sample">· {y.sample_title}</span>}
                </div>
              ))}
              {detail && detail.length > 0 && (
                <div className="garden-tree-archive-detail">
                  {detail.slice(0, 8).map((f, i) => (
                    <span key={i} className="garden-tree-archive-flower">
                      {String(f.title ?? "")}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
