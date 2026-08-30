/**
 * AlterEgoPage — 「它眼中的我」全屏独立页。
 *
 * 网站式架构：栏目（6 个学习维度）→ 文章（具体学习成果，朋友圈式呈现）。
 * 点 DeskHome「个人分身」卡里的「查看完整画像」整窗打开。
 *
 * 设计原则（8.0 红线 + 用户要求）：
 * - 暖米白 + 暖橙，与 MemoryGraphPage 同语言
 * - 有层次感：栏目网格 → 文章列表，两级导航
 * - 朋友圈式文章：头像 + 时间 + 标题 + 内容 + 标签，留白不挤
 * - 禁止堆文字：每篇文章内容截断，空态/学习中态有专门设计
 * - 诚实：API 失败静默降级，显示兜底状态
 */

import { useEffect, useState } from "react";
import { authedFetch, httpBase } from "../../api";

interface Props {
  open: boolean;
  onClose: () => void;
}

interface Article {
  title: string;
  content: string;
  evidence?: string;
  tags?: string[];
}

interface Section {
  id: string;
  title: string;
  icon: string;
  status: "ready" | "learning" | "empty";
  headline: string;
  articles: Article[];
  updated_at?: string;
}

interface Portrait {
  sections: Section[];
  summary: {
    ready_count: number;
    learning_count: number;
    empty_count: number;
    total_articles: number;
  };
}

interface AlterEgoData {
  enabled: boolean;
  has_data: boolean;
  total_signals: number;
  portrait: Portrait | null;
}

const STATUS_META: Record<string, { label: string; color: string; bg: string }> = {
  ready: { label: "已就绪", color: "#15803d", bg: "rgba(22,163,74,0.1)" },
  learning: { label: "学习中", color: "#b45309", bg: "rgba(180,83,9,0.1)" },
  empty: { label: "还没有", color: "#9ca3af", bg: "rgba(156,163,175,0.1)" },
};

function fmtTime(ts?: string): string {
  if (!ts) return "";
  const m = /^(\d{4}-\d{2}-\d{2})/.exec(ts);
  return m ? m[1] : ts.slice(0, 10);
}

export function AlterEgoPage({ open, onClose }: Props) {
  const [data, setData] = useState<AlterEgoData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeSection, setActiveSection] = useState<Section | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setActiveSection(null);
    authedFetch(`${httpBase()}/v1/deskmate/alter-ego`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => {
        if (cancelled) return;
        setData(d as AlterEgoData);
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

  if (!open) return null;

  const portrait = data?.portrait;
  const sections = portrait?.sections ?? [];
  const summary = portrait?.summary;

  return (
    <div
      className="fixed inset-0 z-[60] overflow-y-auto"
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
        className="sticky top-0 z-10 flex items-center justify-between px-6 py-4"
        style={{
          background: "rgba(255,248,239,0.85)",
          backdropFilter: "blur(12px)",
          borderBottom: "1px solid rgba(214,188,160,0.3)",
        }}
      >
        <div>
          <h1 style={{ margin: 0, fontSize: 24, fontWeight: 800, letterSpacing: 2, color: "#6b4a32" }}>
            {activeSection ? (
              <span
                onClick={() => setActiveSection(null)}
                style={{ cursor: "pointer", color: "#a38568", fontSize: 16, marginRight: 8 }}
              >
                ← 返回
              </span>
            ) : null}
            它眼中的我
          </h1>
          <div style={{ marginTop: 4, fontSize: 12.5, color: "#9a7b5f", maxWidth: 480, lineHeight: 1.6 }}>
            {activeSection
              ? `${activeSection.icon} ${activeSection.title} · 小桔从你身上学到的`
              : "这是小桔一点一点，从你身上学到的。散落在六个角落，被时间轻轻串起。"}
          </div>
        </div>
        <button
          onClick={onClose}
          className="px-4 py-2 rounded-lg text-[13px] font-semibold transition-colors"
          style={{ background: "#c2662f", color: "#fff", border: "none", boxShadow: "0 4px 14px rgba(180,110,60,0.3)" }}
        >
          关闭
        </button>
      </div>

      <div style={{ maxWidth: 960, margin: "0 auto", padding: "24px 20px 60px" }}>
        {/* 加载态 */}
        {loading && (
          <div style={{ textAlign: "center", padding: "80px 0", color: "#a38568" }}>
            正在唤起它眼中的你…
          </div>
        )}

        {/* 错误态 */}
        {!loading && error && (
          <div style={{ textAlign: "center", padding: "60px 0", color: "#c0392b" }}>
            读取失败：{error}
          </div>
        )}

        {/* 概览统计（栏目列表页） */}
        {!loading && !error && !activeSection && summary && (
          <div
            style={{
              display: "flex",
              gap: 16,
              marginBottom: 24,
              flexWrap: "wrap",
            }}
          >
            <div style={statBoxStyle}>
              <div style={{ fontSize: 28, fontWeight: 800, color: "#c2662f" }}>{summary.ready_count}</div>
              <div style={{ fontSize: 12, color: "#9a7b5f", marginTop: 2 }}>栏目就绪</div>
            </div>
            <div style={statBoxStyle}>
              <div style={{ fontSize: 28, fontWeight: 800, color: "#b45309" }}>{summary.learning_count}</div>
              <div style={{ fontSize: 12, color: "#9a7b5f", marginTop: 2 }}>学习中</div>
            </div>
            <div style={statBoxStyle}>
              <div style={{ fontSize: 28, fontWeight: 800, color: "#9ca3af" }}>{summary.empty_count}</div>
              <div style={{ fontSize: 12, color: "#9a7b5f", marginTop: 2 }}>还没有</div>
            </div>
            <div style={statBoxStyle}>
              <div style={{ fontSize: 28, fontWeight: 800, color: "#6b4a32" }}>{summary.total_articles}</div>
              <div style={{ fontSize: 12, color: "#9a7b5f", marginTop: 2 }}>篇学习成果</div>
            </div>
          </div>
        )}

        {/* 栏目网格（列表页） */}
        {!loading && !error && !activeSection && (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
              gap: 16,
            }}
          >
            {sections.map((s) => {
              const meta = STATUS_META[s.status] ?? STATUS_META.empty;
              return (
                <div
                  key={s.id}
                  onClick={() => setActiveSection(s)}
                  style={{
                    ...sectionCardStyle,
                    opacity: s.status === "empty" ? 0.7 : 1,
                    cursor: s.status === "empty" ? "default" : "pointer",
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <div style={iconBoxStyle}>{s.icon}</div>
                      <div style={{ fontSize: 16, fontWeight: 700, color: "#4d3a2a" }}>{s.title}</div>
                    </div>
                    <span
                      style={{
                        fontSize: 11,
                        fontWeight: 600,
                        padding: "3px 8px",
                        borderRadius: 10,
                        color: meta.color,
                        background: meta.bg,
                      }}
                    >
                      {meta.label}
                    </span>
                  </div>
                  <div style={{ fontSize: 13, color: "#6b5a48", lineHeight: 1.6, minHeight: 40 }}>
                    {s.headline}
                  </div>
                  <div
                    style={{
                      marginTop: 12,
                      paddingTop: 10,
                      borderTop: "1px solid rgba(214,188,160,0.3)",
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                    }}
                  >
                    <span style={{ fontSize: 11.5, color: "#a38568" }}>
                      {s.articles.length > 0 ? `${s.articles.length} 篇` : "还在积累"}
                    </span>
                    {s.status !== "empty" && (
                      <span style={{ fontSize: 12, color: "#c2662f", fontWeight: 600 }}>查看 →</span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* 文章列表（详情页，朋友圈式） */}
        {!loading && !error && activeSection && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            {/* 栏目头部信息 */}
            <div
              style={{
                background: "rgba(255,255,255,0.6)",
                borderRadius: 16,
                padding: "16px 20px",
                border: "1px solid rgba(214,188,160,0.4)",
                marginBottom: 4,
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8 }}>
                <div style={{ ...iconBoxStyle, width: 44, height: 44, fontSize: 22 }}>{activeSection.icon}</div>
                <div>
                  <div style={{ fontSize: 18, fontWeight: 700, color: "#4d3a2a" }}>{activeSection.title}</div>
                  {activeSection.updated_at && (
                    <div style={{ fontSize: 12, color: "#a38568" }}>最近更新：{fmtTime(activeSection.updated_at)}</div>
                  )}
                </div>
              </div>
              <div style={{ fontSize: 13.5, color: "#6b5a48", lineHeight: 1.7 }}>{activeSection.headline}</div>
            </div>

            {/* 文章列表（朋友圈式） */}
            {activeSection.articles.length === 0 ? (
              <div style={emptyStateStyle}>
                <div style={{ fontSize: 40, marginBottom: 12 }}>🌱</div>
                <div style={{ fontSize: 15, color: "#9a7b5f", marginBottom: 4 }}>还在学习中</div>
                <div style={{ fontSize: 12.5, color: "#b09a82", lineHeight: 1.6 }}>
                  多和小桔聊聊，这里就会长出它学到的东西。
                </div>
              </div>
            ) : (
              activeSection.articles.map((a, i) => (
                <div key={i} style={articleCardStyle}>
                  {/* 朋友圈式头部：头像 + 名称 + 时间 */}
                  <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
                    <div style={avatarStyle}>{activeSection.icon}</div>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontSize: 13.5, fontWeight: 600, color: "#4d3a2a" }}>小桔</div>
                      <div style={{ fontSize: 11.5, color: "#a38568" }}>
                        {a.evidence || `从你的${activeSection.title}中学习`}
                      </div>
                    </div>
                  </div>
                  {/* 文章标题 */}
                  <div style={{ fontSize: 15, fontWeight: 700, color: "#3d2e1f", marginBottom: 8, lineHeight: 1.5 }}>
                    {a.title}
                  </div>
                  {/* 文章内容 */}
                  <div style={{ fontSize: 14, color: "#5b4a38", lineHeight: 1.8, marginBottom: 12 }}>
                    {a.content}
                  </div>
                  {/* 标签 */}
                  {a.tags && a.tags.length > 0 && (
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      {a.tags.map((t, ti) => (
                        <span
                          key={ti}
                          style={{
                            fontSize: 11,
                            color: "#b45309",
                            background: "rgba(180,83,9,0.08)",
                            padding: "2px 8px",
                            borderRadius: 8,
                            fontWeight: 500,
                          }}
                        >
                          #{t}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        )}

        {/* 底部提示 */}
        {!loading && !error && !activeSection && (
          <div style={{ textAlign: "center", marginTop: 40, fontSize: 12, color: "#b09a82", lineHeight: 1.8 }}>
            它眼中的你，会随着时间慢慢生长。<br />
            多创作、多做选择、多接受建议，这里就会越来越丰富。
          </div>
        )}
      </div>
    </div>
  );
}

// ---- 样式常量 ----
const statBoxStyle: React.CSSProperties = {
  flex: "1 1 120px",
  background: "rgba(255,255,255,0.55)",
  borderRadius: 14,
  padding: "14px 16px",
  border: "1px solid rgba(214,188,160,0.35)",
  textAlign: "center",
  minWidth: 100,
};

const sectionCardStyle: React.CSSProperties = {
  background: "rgba(255,255,255,0.65)",
  borderRadius: 16,
  padding: "18px 18px 14px",
  border: "1px solid rgba(214,188,160,0.4)",
  boxShadow: "0 4px 16px rgba(180,130,90,0.08)",
  transition: "transform 0.15s, box-shadow 0.15s",
};

const iconBoxStyle: React.CSSProperties = {
  width: 36,
  height: 36,
  borderRadius: 10,
  background: "rgba(194,102,47,0.1)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: 18,
  flexShrink: 0,
};

const articleCardStyle: React.CSSProperties = {
  background: "rgba(255,255,255,0.72)",
  borderRadius: 16,
  padding: "18px 20px",
  border: "1px solid rgba(214,188,160,0.35)",
  boxShadow: "0 2px 12px rgba(180,130,90,0.06)",
};

const avatarStyle: React.CSSProperties = {
  width: 40,
  height: 40,
  borderRadius: "50%",
  background: "linear-gradient(135deg, #f5deb3, #deb887)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: 18,
  flexShrink: 0,
  boxShadow: "0 2px 8px rgba(180,130,90,0.15)",
};

const emptyStateStyle: React.CSSProperties = {
  textAlign: "center",
  padding: "48px 24px",
  background: "rgba(255,255,255,0.5)",
  borderRadius: 16,
  border: "1px dashed rgba(214,188,160,0.5)",
};
