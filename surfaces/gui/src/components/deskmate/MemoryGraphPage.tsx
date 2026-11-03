/**
 * MemoryGraphPage — 「我们的记忆」星尘夜空全屏页（2026-11-03 改版）。
 *
 * 点桌宠面板「查看记忆 / 我们的记忆」整窗打开。拉后端实时数据
 * /v1/deskmate/learned-history，交给 MemoryGraph 画星尘夜空（每条记忆＝一颗星，
 * 作品＝星群）。外壳从暖米色换成暮色玻璃：天幕渐变由 .dm-sky 承载（刻意不压黑），
 * canvas 透明叠在其上。悬停看内容、点星看详情、点主星聚焦星群、图例筛类别、
 * 「复位星空」/面包屑「整片夜空」退出聚焦。样式真源在 styles.css 的 .dm-sky-* 段。
 */
import { useCallback, useEffect, useState } from "react";
import { authedFetch, httpBase } from "../../api";
import { MemoryGraph, CATEGORY_META, type MemItem, type SelectInfo } from "./MemoryGraph";

interface Props {
  open: boolean;
  onClose: () => void;
  onOpenList: () => void;
}

interface GrowthInfo {
  trust: number;
  intimacy: number;
  empathy: number;
  bond_stage: string;
  bond_depth: number;
  relation_factor: number;
  empathy_domains: Record<string, number>;
}

function fmtDate(ts: string): string {
  if (!ts) return "";
  const m = /^(\d{4}-\d{2}-\d{2})T?(\d{2}:\d{2})?/.exec(ts);
  if (m) return (m[1] || "") + (m[2] ? " " + m[2] : "");
  return ts;
}

export function MemoryGraphPage({ open, onClose, onOpenList }: Props) {
  const [items, setItems] = useState<MemItem[]>([]);
  const [growth, setGrowth] = useState<GrowthInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [hover, setHover] = useState<string | null>(null);
  const [selected, setSelected] = useState<SelectInfo | null>(null);
  const [resetSignal, setResetSignal] = useState(0);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    authedFetch(`${httpBase()}/v1/deskmate/learned-history?limit=5000`)
      .then((r) => r.json())
      .then((d) => {
        if (cancelled) return;
        const raw = (d?.items ?? []) as { ts?: string; fact?: string; category?: string }[];
        setItems(
          raw.map((x) => ({
            ts: x.ts ?? "",
            fact: x.fact ?? "",
            category: x.category || "other",
          })),
        );
      })
      .catch((e) => {
        if (!cancelled) setError(String(e?.message ?? e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    // P1-8：并行拉成长关系快照（连接强度由默契驱动，越用越连）
    authedFetch(`${httpBase()}/v1/deskmate/learning-graph`)
      .then((r) => r.json())
      .then((d) => {
        if (!cancelled) setGrowth((d?.growth as GrowthInfo) ?? null);
      })
      .catch(() => {
        /* 关系快照是锦上添花，失败不阻断星空 */
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const toggleCat = useCallback((cat: string) => {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(cat)) next.delete(cat);
      else next.add(cat);
      return next;
    });
  }, []);

  const resetSky = useCallback(() => {
    setResetSignal((v) => v + 1);
    setSelected(null);
  }, []);

  if (!open) return null;

  const shownCount = items.filter((i) => !hidden.has(i.category)).length;

  return (
    <div className="dm-sky">
      {/* 顶部栏 */}
      <div className="dm-sky-top">
        <div>
          <h1>我们的记忆</h1>
          <div className="dm-sky-lede">
            每条记忆是一颗星星，一本作品是一片星群。夜空不是黑的，是暮色——你越用它越亮。
          </div>
          <div className="dm-sky-count">
            共 <b>{shownCount}</b> 颗星
            {(hidden.size > 0 || shownCount !== items.length) && (
              <span> / 全部 {items.length}</span>
            )}
            {growth && growth.bond_stage && (
              <span>
                　关系 · <b style={{ color: "var(--sky-amber)" }}>{growth.bond_stage}</b>
                　信任 {growth.trust.toFixed(2)} · 亲密度 {growth.intimacy.toFixed(0)} · 默契 {growth.empathy.toFixed(0)}
              </span>
            )}
          </div>
          {growth && (
            <div className="dm-sky-factor">
              星群随我们的默契加深 ×{growth.relation_factor.toFixed(2)} —— 你越用，它越懂你，我们的天越密。
            </div>
          )}
        </div>
        <div className="dm-sky-btns">
          <button className="dm-sky-ghost" onClick={resetSky}>复位星空</button>
          <button className="dm-sky-ghost" onClick={onOpenList}>列表视图</button>
          <button className="dm-sky-solid" onClick={onClose}>返回</button>
        </div>
      </div>

      {/* 天幕画布 */}
      <div className="dm-sky-canvas">
        {loading ? (
          <div className="dm-sky-state">正在唤起记忆…</div>
        ) : error ? (
          <div className="dm-sky-state" style={{ color: "#ff9d8a" }}>读取记忆失败：{error}</div>
        ) : items.length === 0 ? (
          <div className="dm-sky-state">还没有记住什么，多聊聊，我就会开始收藏星星。</div>
        ) : (
          <MemoryGraph
            items={items}
            hiddenCats={hidden}
            onSelect={setSelected}
            onHover={setHover}
            resetSignal={resetSignal}
          />
        )}
      </div>

      {/* 悬停提示条 */}
      <div className={"dm-sky-hover" + (hover ? " show" : "")}>{hover}</div>

      {/* 图例 + 筛选 */}
      <div className="dm-sky-legend">
        {Object.entries(CATEGORY_META).map(([cat, meta]) => {
          const dim = hidden.has(cat);
          return (
            <div
              key={cat}
              className={"lg-row" + (dim ? " off" : "")}
              onClick={() => toggleCat(cat)}
            >
              <span className="lg-dot" style={{ background: meta.color, boxShadow: `0 0 8px ${meta.color}` }} />
              {meta.name}
              {dim && <span style={{ fontSize: 11 }}>（已隐藏）</span>}
            </div>
          );
        })}
        <div className="lg-hint">
          ✦ 亮一点的主星 = 一部作品/项目，周围是它的记忆星尘。
          <br />
          滚轮缩放 · 拖拽平移 · 点星群看连线 · 点单星看内容 · 图例可筛
        </div>
      </div>

      {/* 详情抽屉 */}
      <div className={"dm-sky-drawer" + (selected ? " show" : "")}>
        <button className="dw-close" onClick={() => setSelected(null)}>×</button>
        {selected && (
          <div className="dw-scroll">
            {selected.group ? (
              <>
                <div className="dw-head">《{selected.group.title}》</div>
                <div className="dw-sub">这片星群 {selected.group.count} 颗星 · 点星尘看单条，点下面清单直接读</div>
                <h4>最近的星</h4>
                {selected.related.map((r, i) => (
                  <div
                    key={i}
                    className="dw-mem"
                    onClick={() => setSelected({ item: r, related: [] })}
                    title="点击查看这条记忆"
                  >
                    {r.fact.length > 80 ? r.fact.slice(0, 80) + "…" : r.fact}
                  </div>
                ))}
                {selected.group.count > selected.related.length && (
                  <div className="dw-sub">还有 {selected.group.count - selected.related.length} 颗在天上（列表视图可全量翻）</div>
                )}
              </>
            ) : (
              <>
                <span
                  className="dm-sky-chip"
                  style={{ background: CATEGORY_META[selected.item.category]?.color ?? "#b9a8ff" }}
                >
                  {CATEGORY_META[selected.item.category]?.name ?? "其他"}
                </span>
                {selected.item.ts && <div className="dw-ts">{fmtDate(selected.item.ts)}</div>}
                <div className="dw-fact">{selected.item.fact}</div>
                {selected.related.length > 0 && (
                  <>
                    <h4>同一片星群的星（{selected.related.length}）</h4>
                    {selected.related.map((r, i) => (
                      <div
                        key={i}
                        className="dw-mem"
                        onClick={() => setSelected({ item: r, related: [] })}
                        title="点击查看这条记忆"
                      >
                        {r.fact.length > 80 ? r.fact.slice(0, 80) + "…" : r.fact}
                      </div>
                    ))}
                  </>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
