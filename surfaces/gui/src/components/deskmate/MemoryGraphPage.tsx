/**
 * MemoryGraphPage — 「我们的记忆」全屏独立页。
 *
 * 点桌宠面板「查看记忆 / 我们的记忆」整窗打开。拉后端实时数据
 * /v1/deskmate/learned-history，渲染力导向图谱（作品/项目做枢纽星团、
 * 四类分色暖区）。悬停看内容、点击看详情、可按分类筛选。
 * 顶部「返回」回到桌宠面板，「列表视图」切换到原有 MemoryViewer。
 */
import { useCallback, useEffect, useState } from "react";
import { authedFetch, httpBase } from "../../api";
import { MemoryGraph, CATEGORY_META, type MemItem } from "./MemoryGraph";

interface Props {
  open: boolean;
  onClose: () => void;
  onOpenList: () => void;
}

interface SelectInfo {
  item: MemItem;
  related: MemItem[];
}

function fmtDate(ts: string): string {
  if (!ts) return "";
  const m = /^(\d{4}-\d{2}-\d{2})T?(\d{2}:\d{2})?/.exec(ts);
  if (m) return (m[1] || "") + (m[2] ? " " + m[2] : "");
  return ts;
}

export function MemoryGraphPage({ open, onClose, onOpenList }: Props) {
  const [items, setItems] = useState<MemItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [hover, setHover] = useState<string | null>(null);
  const [selected, setSelected] = useState<SelectInfo | null>(null);

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

  if (!open) return null;

  const shownCount = items.filter((i) => !hidden.has(i.category)).length;

  return (
    <div
      className="fixed inset-0 z-[60]"
      style={{
        background:
          "radial-gradient(120% 90% at 22% 12%, #fff8ef 0%, #ffe9d4 38%, #f6dcc6 72%, #efcdb4 100%)",
        color: "#5b4636",
        fontFamily:
          '"PingFang SC","Hiragino Sans GB","Microsoft YaHei",-apple-system,system-ui,sans-serif',
      }}
    >
      {/* 顶部栏 */}
      <div
        className="absolute left-0 right-0 top-0 flex items-center justify-between px-6 py-4"
        style={{ zIndex: 2 }}
      >
        <div>
          <h1 style={{ margin: 0, fontSize: 26, fontWeight: 800, letterSpacing: 2, color: "#6b4a32" }}>
            我们的记忆
          </h1>
          <div style={{ marginTop: 6, fontSize: 13, color: "#9a7b5f", maxWidth: 460, lineHeight: 1.6 }}>
            这是知了一点一点，记住的你。散落在四个温柔的角落，被一本本作品轻轻串起。
          </div>
          <div style={{ marginTop: 8, fontSize: 13, color: "#b07a4e", fontWeight: 600 }}>
            共 <b style={{ fontSize: 20, color: "#c2662f" }}>{shownCount}</b> 条记忆
            {(hidden.size > 0 || shownCount !== items.length) && (
              <span style={{ color: "#a38568", fontWeight: 400 }}> / 总计 {items.length}</span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={onOpenList}
            className="px-3 py-2 rounded-lg text-[12px] font-medium transition-colors"
            style={{ background: "rgba(255,250,243,0.82)", border: "1px solid rgba(214,188,160,0.6)", color: "#6b4a32" }}
          >
            列表视图
          </button>
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-lg text-[13px] font-semibold transition-colors"
            style={{ background: "#c2662f", color: "#fff", border: "none", boxShadow: "0 4px 14px rgba(180,110,60,0.3)" }}
          >
            返回
          </button>
        </div>
      </div>

      {/* 画布 */}
      <div className="absolute inset-0" style={{ top: 96 }}>
        {loading ? (
          <div className="flex items-center justify-center h-full" style={{ color: "#a38568" }}>
            正在唤起记忆…
          </div>
        ) : error ? (
          <div className="flex items-center justify-center h-full" style={{ color: "#c0392b" }}>
            读取记忆失败：{error}
          </div>
        ) : items.length === 0 ? (
          <div className="flex items-center justify-center h-full" style={{ color: "#a38568" }}>
            还没有记住什么，多聊聊，我就会开始收藏。
          </div>
        ) : (
          <MemoryGraph items={items} hiddenCats={hidden} onSelect={setSelected} onHover={setHover} />
        )}
      </div>

      {/* 悬停提示条 */}
      <div
        className="absolute left-1/2 -translate-x-1/2 px-4 py-2 rounded-xl"
        style={{
          bottom: 92,
          background: "rgba(60,42,30,0.86)",
          color: "#fff7ec",
          fontSize: 12.5,
          lineHeight: 1.5,
          maxWidth: "60vw",
          opacity: hover ? 1 : 0,
          transition: "opacity 0.15s",
          pointerEvents: "none",
          zIndex: 3,
        }}
      >
        {hover}
      </div>

      {/* 图例 + 筛选 */}
      <div
        className="absolute rounded-2xl"
        style={{
          left: 24,
          bottom: 24,
          zIndex: 3,
          background: "rgba(255,250,243,0.74)",
          backdropFilter: "blur(8px)",
          border: "1px solid rgba(214,188,160,0.55)",
          padding: "12px 14px",
          fontSize: 12.5,
          color: "#7a5f48",
          lineHeight: 1.8,
          boxShadow: "0 8px 26px rgba(180,130,90,0.16)",
        }}
      >
        {Object.entries(CATEGORY_META).map(([cat, meta]) => {
          const dim = hidden.has(cat);
          return (
            <div
              key={cat}
              onClick={() => toggleCat(cat)}
              style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", opacity: dim ? 0.4 : 1 }}
            >
              <span
                style={{
                  width: 12,
                  height: 12,
                  borderRadius: "50%",
                  background: meta.color,
                  boxShadow: "0 0 0 3px rgba(255,255,255,0.5)",
                  flex: "0 0 auto",
                }}
              />
              {meta.name}
              {dim && <span style={{ color: "#a38568", fontSize: 11 }}>（已隐藏）</span>}
            </div>
          );
        })}
        <div style={{ marginTop: 7, color: "#9a7b5f", fontSize: 11.5, lineHeight: 1.6 }}>
          ● 较大带字节点 = 一部作品/项目，周围环绕相关记忆。
          <br />
          悬停看内容 · 点击看详情 · 可拖动整理 · 点击图例筛选
        </div>
      </div>

      {/* 详情面板 */}
      <div
        className="absolute top-0 right-0 h-full"
        style={{
          width: 360,
          maxWidth: "86vw",
          zIndex: 4,
          background: "rgba(255,250,243,0.96)",
          backdropFilter: "blur(14px)",
          borderLeft: "1px solid rgba(214,188,160,0.6)",
          boxShadow: "-14px 0 40px rgba(160,110,70,0.18)",
          transform: selected ? "translateX(0)" : "translateX(105%)",
          transition: "transform 0.32s cubic-bezier(0.22,0.9,0.3,1)",
          display: "flex",
          flexDirection: "column",
          padding: "22px 22px 16px",
          boxSizing: "border-box",
        }}
      >
        <button
          onClick={() => setSelected(null)}
          style={{
            position: "absolute",
            top: 16,
            right: 16,
            width: 30,
            height: 30,
            borderRadius: "50%",
            border: "none",
            background: "rgba(214,188,160,0.25)",
            color: "#6b4a32",
            fontSize: 16,
            cursor: "pointer",
          }}
        >
          ×
        </button>
        {selected && (
          <div style={{ overflowY: "auto", paddingRight: 4 }}>
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 7,
                fontSize: 12,
                fontWeight: 600,
                padding: "4px 11px",
                borderRadius: 20,
                color: "#fff",
                background: CATEGORY_META[selected.item.category]?.color ?? "#b9a7d6",
              }}
            >
              {CATEGORY_META[selected.item.category]?.name ?? "其他"}
            </div>
            {selected.item.ts && (
              <div style={{ fontSize: 12, color: "#a38568", margin: "14px 0 8px" }}>
                {fmtDate(selected.item.ts)}
              </div>
            )}
            <div style={{ fontSize: 15, lineHeight: 1.75, color: "#4d3a2a" }}>{selected.item.fact}</div>

            {selected.related.length > 0 && (
              <>
                <div style={{ margin: "18px 0 8px", fontSize: 13, fontWeight: 700, color: "#6b4a32" }}>
                  相关的记忆（{selected.related.length}）
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
                  {selected.related.map((r, i) => (
                    <div
                      key={i}
                      onClick={() => setSelected({ item: r, related: [] })}
                      title="点击查看这条记忆"
                      style={{
                        fontSize: 12.5,
                        lineHeight: 1.6,
                        color: "#6a5340",
                        background: "rgba(255,255,255,0.6)",
                        border: "1px solid rgba(214,188,160,0.4)",
                        borderRadius: 9,
                        padding: "8px 10px",
                        cursor: "pointer",
                      }}
                    >
                      {r.fact.length > 80 ? r.fact.slice(0, 80) + "…" : r.fact}
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
