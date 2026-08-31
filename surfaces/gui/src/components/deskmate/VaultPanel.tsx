/**
 * VaultPanel — 长期知识库（vault）展示面板。
 *
 * P1-7：vault 端点前端 0 消费，补入口。
 * 展示 vault 统计（总条数/分类）+ 最近条目列表，点击展开查看内容。
 *
 * 设计原则：暖米白 + 暖橙，与 DiaryPanel 同语言；简洁不堆文字。
 */

import { useEffect, useState } from "react";
import { authedFetch, httpBase } from "../../api";

interface VaultEntry {
  path: string;
  category: string;
  tags?: string[];
  created?: string;
  title?: string;
  body?: string;
}

interface VaultStats {
  total: number;
  by_category?: Record<string, number>;
}

const CATEGORY_META: Record<string, { label: string; color: string; icon: string }> = {
  knowledge: { label: "知识", color: "#6366f1", icon: "📚" },
  corrections: { label: "纠正", color: "#ef4444", icon: "🎯" },
  work: { label: "创作", color: "#f59e0b", icon: "✍️" },
  preferences: { label: "偏好", color: "#10b981", icon: "💚" },
  archive: { label: "归档", color: "#9ca3af", icon: "📦" },
  life: { label: "人生档案", color: "#c2662f", icon: "📖" },
};

export function VaultPanel() {
  const [entries, setEntries] = useState<VaultEntry[]>([]);
  const [stats, setStats] = useState<VaultStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [editingPath, setEditingPath] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);

  async function handleSave(path: string) {
    if (!path || saving) return;
    setSaving(true);
    try {
      const r = await authedFetch(`${httpBase()}/v1/deskmate/vault/update`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, content: draft }),
      });
      if (r.ok) {
        // 更新本地条目内容
        setEntries((prev) =>
          prev.map((e) => (e.path === path ? { ...e, body: draft } : e))
        );
        setEditingPath(null);
        setDraft("");
      } else {
        alert("保存失败：" + (await r.text()));
      }
    } catch (e) {
      alert("保存失败：" + String(e));
    } finally {
      setSaving(false);
    }
  }

  useEffect(() => {
    authedFetch(`${httpBase()}/v1/deskmate/vault`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d) {
          setEntries(d.entries ?? []);
          setStats(d.stats ?? null);
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div style={panelStyle}>
        <div style={{ textAlign: "center", padding: "24px 0", color: "#a38568", fontSize: 13 }}>
          正在读取长期知识库…
        </div>
      </div>
    );
  }

  if (!stats || stats.total === 0) {
    return (
      <div style={panelStyle}>
        <div style={{ textAlign: "center", padding: "24px 0", color: "#a38568", fontSize: 13 }}>
          长期知识库还是空的。多和小桔聊聊，有价值的知识会自动沉淀到这里。
        </div>
      </div>
    );
  }

  // 按分类分组
  const byCategory: Record<string, VaultEntry[]> = {};
  for (const e of entries) {
    const cat = e.category || "other";
    if (!byCategory[cat]) byCategory[cat] = [];
    byCategory[cat].push(e);
  }

  return (
    <div style={panelStyle}>
      {/* 标题 + 统计 */}
      <div style={{ marginBottom: 14 }}>
        <div style={{ fontSize: 15, fontWeight: 700, color: "#4d3a2a", marginBottom: 6 }}>
          📚 长期知识库
        </div>
        <div style={{ fontSize: 12, color: "#9a7b5f", marginBottom: 10 }}>
          小桔为你沉淀的长期知识，共 <b style={{ color: "#c2662f" }}>{stats.total}</b> 条
        </div>
        {/* 分类统计条 */}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {Object.entries(stats.by_category ?? {})
            .filter(([, n]) => n > 0)
            .map(([cat, n]) => {
              const meta = CATEGORY_META[cat] ?? { label: cat, color: "#9ca3af", icon: "·" };
              return (
                <span
                  key={cat}
                  style={{
                    fontSize: 11,
                    padding: "3px 9px",
                    borderRadius: 10,
                    background: `${meta.color}15`,
                    color: meta.color,
                    fontWeight: 600,
                  }}
                >
                  {meta.icon} {meta.label} {n}
                </span>
              );
            })}
        </div>
      </div>

      {/* 条目列表（按分类分组，每类最多显示 5 条） */}
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {Object.entries(byCategory).map(([cat, list]) => {
          const meta = CATEGORY_META[cat] ?? { label: cat, color: "#9ca3af", icon: "·" };
          const shown = list.slice(0, 5);
          return (
            <div key={cat}>
              <div
                style={{
                  fontSize: 11,
                  fontWeight: 600,
                  color: meta.color,
                  marginBottom: 6,
                  textTransform: "uppercase",
                  letterSpacing: 0.5,
                }}
              >
                {meta.icon} {meta.label}（{list.length}）
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                {shown.map((e) => {
                  const isOpen = expanded === e.path;
                  return (
                    <div
                      key={e.path}
                      onClick={() => setExpanded(isOpen ? null : e.path)}
                      style={{
                        background: isOpen ? "rgba(194,102,47,0.06)" : "rgba(255,255,255,0.5)",
                        border: `1px solid ${isOpen ? "rgba(194,102,47,0.2)" : "rgba(214,188,160,0.3)"}`,
                        borderRadius: 8,
                        padding: "8px 10px",
                        cursor: "pointer",
                        transition: "background 0.15s",
                      }}
                    >
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 8 }}>
                        <span
                          style={{
                            fontSize: 12.5,
                            color: "#4d3a2a",
                            fontWeight: 500,
                            lineHeight: 1.5,
                            flex: 1,
                          }}
                        >
                          {e.title || e.path}
                        </span>
                        {e.created && (
                          <span style={{ fontSize: 10.5, color: "#b09a82", flexShrink: 0 }}>
                            {e.created.slice(5)}
                          </span>
                        )}
                      </div>
                      {e.tags && e.tags.length > 0 && (
                        <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginTop: 4 }}>
                          {e.tags.slice(0, 4).map((t, i) => (
                            <span
                              key={i}
                              style={{
                                fontSize: 10,
                                color: "#b45309",
                                background: "rgba(180,83,9,0.08)",
                                padding: "1px 6px",
                                borderRadius: 6,
                              }}
                            >
                              #{t}
                            </span>
                          ))}
                        </div>
                      )}
                      {isOpen && e.body && editingPath !== e.path && (
                        <div
                          style={{
                            marginTop: 8,
                            paddingTop: 8,
                            borderTop: "1px solid rgba(214,188,160,0.3)",
                            fontSize: 12,
                            color: "#6b5a48",
                            lineHeight: 1.7,
                            maxHeight: 200,
                            overflowY: "auto",
                            whiteSpace: "pre-wrap",
                          }}
                        >
                          {e.body}
                        </div>
                      )}
                      {isOpen && editingPath === e.path && (
                        <div style={{ marginTop: 8, paddingTop: 8, borderTop: "1px solid rgba(214,188,160,0.3)" }}>
                          <textarea
                            value={draft}
                            onChange={(ev) => setDraft(ev.target.value)}
                            style={{
                              width: "100%",
                              minHeight: 160,
                              fontSize: 12,
                              color: "#4d3a2a",
                              lineHeight: 1.7,
                              padding: "8px 10px",
                              borderRadius: 8,
                              border: "1px solid rgba(194,102,47,0.3)",
                              background: "#fffdf8",
                              fontFamily: "inherit",
                              resize: "vertical",
                              boxSizing: "border-box",
                            }}
                          />
                          <div style={{ display: "flex", gap: 8, marginTop: 8, justifyContent: "flex-end" }}>
                            <button
                              onClick={() => { setEditingPath(null); setDraft(""); }}
                              style={{
                                fontSize: 11.5, padding: "4px 12px", borderRadius: 6,
                                border: "1px solid rgba(214,188,160,0.5)", background: "#fff", color: "#6b5a48",
                                cursor: "pointer",
                              }}
                            >
                              取消
                            </button>
                            <button
                              onClick={() => handleSave(e.path)}
                              disabled={saving}
                              style={{
                                fontSize: 11.5, padding: "4px 12px", borderRadius: 6,
                                border: "none", background: saving ? "#cbb39c" : "#c2662f", color: "#fff",
                                cursor: saving ? "default" : "pointer", fontWeight: 600,
                              }}
                            >
                              {saving ? "保存中…" : "保存"}
                            </button>
                          </div>
                        </div>
                      )}
                      {isOpen && e.body && editingPath !== e.path && (
                        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 6 }}>
                          <button
                            onClick={(ev) => { ev.stopPropagation(); setEditingPath(e.path); setDraft(e.body || ""); }}
                            style={{
                              fontSize: 11, padding: "3px 10px", borderRadius: 6,
                              border: "1px solid rgba(194,102,47,0.25)", background: "rgba(194,102,47,0.06)", color: "#c2662f",
                              cursor: "pointer", fontWeight: 500,
                            }}
                          >
                            ✎ 编辑
                          </button>
                        </div>
                      )}
                    </div>
                  );
                })}
                {list.length > 5 && (
                  <div style={{ fontSize: 11, color: "#b09a82", textAlign: "center", padding: "2px 0" }}>
                    还有 {list.length - 5} 条…
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

const panelStyle: React.CSSProperties = {
  background: "linear-gradient(to bottom, #fdfbf7, #faf6ef)",
  border: "1px solid rgba(214,188,160,0.4)",
  borderRadius: 14,
  padding: "16px 18px",
  margin: "8px 0",
  fontFamily: '"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif',
};
