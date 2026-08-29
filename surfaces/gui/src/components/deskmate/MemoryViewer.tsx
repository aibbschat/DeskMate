import { useState, useCallback, useEffect, useRef } from "react";
import { Icon } from "../Icon";
import { Markdown } from "../Markdown";
import { getDeskmateMemoryTrace, getDeskmateUserDedup, getDeskmateMemoryLayering, type TraceRecord, type DeskmateMemoryLayering } from "../../api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** A single memory file that the viewer can display/edit. */
export interface MemoryFile {
  /** Unique id (e.g. "soul", "user", "memory"). */
  id: string;
  /** Display name. */
  name: string;
  /** Full file path (for display). */
  path: string;
  /** Markdown content. */
  content: string;
  /** Last modified timestamp (unix ms), or null. */
  lastModified: number | null;
}

export interface MemoryViewerProps {
  /** The list of memory files to display. */
  files: MemoryFile[];
  /** Currently active file id (defaults to the first file). */
  activeId?: string;
  /** Called when the user saves edits to a file. */
  onSave: (fileId: string, newContent: string) => void;
  /** Called when the panel should close. */
  onClose: () => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function MemoryViewer({
  files,
  activeId,
  onSave,
  onClose,
}: MemoryViewerProps) {
  const [currentId, setCurrentId] = useState(activeId ?? files[0]?.id ?? "");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // 溯源 + 遗忘可见（P0-4 + 8.3）：最近注入了哪些记忆 / 忘了哪些记忆
  const [traces, setTraces] = useState<{ inject?: TraceRecord[]; forgot?: TraceRecord[] }>({});
  const [traceOpen, setTraceOpen] = useState(false);
  // P0-5 记忆去重：USER.md 近重复率（8.0 红线：前端可感可看）
  const [dedup, setDedup] = useState<{ dup_rate?: number; entries?: number }>({});
  // P0-6 记忆分层净化 + 时间感知：书的记忆 vs 你的记忆 分离状态（8.0 红线）
  const [layering, setLayering] = useState<DeskmateMemoryLayering>({});

  // 打开面板即拉取最近溯源/遗忘记录（静默降级）
  useEffect(() => {
    getDeskmateMemoryTrace("both", 8)
      .then(setTraces)
      .catch(() => {});
    getDeskmateUserDedup()
      .then(setDedup)
      .catch(() => {});
    getDeskmateMemoryLayering()
      .then(setLayering)
      .catch(() => {});
  }, []);

  const current = files.find((f) => f.id === currentId) ?? files[0];

  const handleExport = useCallback(() => {
    if (!current) return;
    const blob = new Blob([current.content || ""], {
      type: "text/markdown;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = current.name || "memory.md";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [current]);

  // Reset editing state when switching files
  useEffect(() => {
    setEditing(false);
    setDraft("");
  }, [currentId]);

  // Auto-resize textarea
  useEffect(() => {
    if (editing && textareaRef.current) {
      const ta = textareaRef.current;
      ta.style.height = "auto";
      ta.style.height = `${ta.scrollHeight}px`;
    }
  }, [editing, draft]);

  const startEditing = useCallback(() => {
    if (!current) return;
    setDraft(current.content);
    setEditing(true);
  }, [current]);

  const cancelEditing = useCallback(() => {
    setEditing(false);
    setDraft("");
  }, []);

  const saveEditing = useCallback(async () => {
    if (!current) return;
    setSaving(true);
    try {
      await onSave(current.id, draft);
      setEditing(false);
      setDraft("");
    } finally {
      setSaving(false);
    }
  }, [current, draft, onSave]);

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.45)" }}
      onClick={onClose}
    >
      <div
        className="flex flex-col w-full max-w-3xl h-[82vh] max-h-[82vh] rounded-2xl overflow-hidden"
        style={{
          background: "var(--panel)",
          border: "1px solid var(--line)",
          boxShadow: "0 24px 64px rgba(0,0,0,0.3)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {files.length === 0 ? (
          <div
            className="flex flex-col items-center justify-center flex-1 py-16 px-6"
            style={{ color: "var(--faint)" }}
          >
            <Icon name="file" size={24} className="mb-2 opacity-40" />
            <span className="text-[13px]">未找到记忆文件</span>
          </div>
        ) : (
          <div className="flex flex-col h-full min-h-0">
      {/* Top bar */}
      <div
        className="flex items-center justify-between px-4 py-3 flex-shrink-0"
        style={{ borderBottom: "1px solid var(--line)" }}
      >
        <div className="flex items-center gap-2">
          <span style={{ color: "var(--muted)" }}>
            <Icon name="file" size={16} className="opacity-60" />
          </span>
            <span
              className="text-[14px] font-semibold"
              style={{ color: "var(--ink)" }}
            >
              记忆
            </span>
        </div>
        <button
          className="w-7 h-7 rounded-lg flex items-center justify-center"
          style={{ color: "var(--faint)" }}
          onClick={onClose}
          aria-label="Close memory viewer"
        >
          <Icon name="x" size={16} />
        </button>
      </div>

      {/* File tabs */}
      <div
        className="flex gap-1 px-3 py-2 flex-shrink-0 overflow-x-auto"
        style={{ borderBottom: "1px solid var(--line)" }}
      >
        {files.map((f) => (
          <button
            key={f.id}
            className="text-[12.5px] px-3 py-1.5 rounded-lg whitespace-nowrap transition-colors"
            style={{
              color: f.id === currentId ? "var(--ink)" : "var(--muted)",
              background: f.id === currentId ? "var(--accent-soft)" : "transparent",
              fontWeight: f.id === currentId ? 600 : 400,
            }}
            onClick={() => setCurrentId(f.id)}
          >
            {f.name}
          </button>
        ))}
      </div>

      {/* Content area */}
      <div className="flex-1 min-h-0 overflow-y-auto px-4 py-3">
        {current && (
          <>
            {/* File meta */}
            <div
              className="flex items-center gap-3 mb-3 pb-2"
              style={{ borderBottom: "1px solid var(--line)" }}
            >
              <span
                className="text-[11px]"
                style={{ color: "var(--faint)", fontFamily: "var(--mono)" }}
              >
                {current.path}
              </span>
              {current.lastModified && (
                <span className="text-[11px]" style={{ color: "var(--faint)" }}>
                  {formatDate(current.lastModified)}
                </span>
              )}
              <div className="flex-1" />
              <button
                className="text-[12px] flex items-center gap-1 px-2 py-1 rounded-lg"
                style={{ color: "var(--muted)" }}
                onClick={handleExport}
                title="导出当前记忆文件"
              >
                <Icon name="archive" size={12} />
                导出
              </button>
              {!editing ? (
                <button
                  className="text-[12px] flex items-center gap-1 px-2 py-1 rounded-lg"
                  style={{ color: "var(--accent)" }}
                  onClick={startEditing}
                >
                  <Icon name="pencil" size={12} />
                  编辑
                </button>
              ) : (
                <div className="flex items-center gap-1.5">
                  <button
                    className="text-[12px] px-2 py-1 rounded-lg"
                    style={{
                      background: "var(--accent)",
                      color: "#fff",
                      opacity: saving ? 0.6 : 1,
                    }}
                    onClick={saveEditing}
                    disabled={saving}
                  >
                      {saving ? "保存中…" : "保存"}
                    </button>
                    <button
                      className="text-[12px] px-2 py-1 rounded-lg"
                      style={{ color: "var(--muted)" }}
                      onClick={cancelEditing}
                      disabled={saving}
                    >
                      取消
                    </button>
                </div>
              )}
            </div>

            {/* Edit mode */}
            {editing ? (
              <textarea
                ref={textareaRef}
                className="w-full min-h-[300px] resize-none outline-none rounded-lg p-3"
                style={{
                  background: "var(--paper)",
                  border: "1px solid var(--line)",
                  color: "var(--ink)",
                  fontFamily: "var(--mono)",
                  fontSize: "13px",
                  lineHeight: 1.6,
                }}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                spellCheck={false}
              />
            ) : (
              /* Preview mode */
              <div className="prose-sm">
                <Markdown text={current.content || "*空文件*"} />
              </div>
            )}

            {/* 溯源 + 遗忘可见（P0-4 + 8.3）：让「懂」与「忘」都可被看见 */}
            <div
              className="mt-4 pt-3"
              style={{ borderTop: "1px solid var(--line)" }}
            >
              <button
                className="flex items-center gap-1.5 text-[11.5px] font-medium"
                style={{ color: "var(--muted)" }}
                onClick={() => setTraceOpen((o) => !o)}
              >
                <Icon name="clock" size={12} />
                记忆溯源 · 最近忘了什么
                <span style={{ color: "var(--faint)" }}>{traceOpen ? "▾" : "▸"}</span>
              </button>
              <div className="text-[11px] mt-1.5" style={{ color: "var(--faint)" }}>
                USER.md 重复率：
                <span
                  style={{
                    color: (dedup.dup_rate ?? 0) > 0 ? "var(--accent)" : "var(--muted)",
                    fontWeight: 600,
                  }}
                >
                  {((dedup.dup_rate ?? 0) * 100).toFixed(0)}%
                </span>
                {" · "}
                {dedup.entries ?? 0} 条
              </div>
              <div className="text-[11px] mt-1" style={{ color: "var(--faint)" }}>
                记忆分层：
                <span style={{ color: (layering.layered ?? true) ? "var(--muted)" : "var(--accent)", fontWeight: 600 }}>
                  {layering.layered ?? true ? "已分离" : "待净化"}
                </span>
                {" · 你的记忆 "}
                <b style={{ color: "var(--muted)" }}>{layering.global_entries ?? 0}</b>
                {" 条 / 书稿记忆 "}
                <b style={{ color: "var(--muted)" }}>{layering.project_entries ?? 0}</b>
                {" 条"}
                {(layering.project_names ?? []).length > 0 && (
                  <span> （{(layering.project_names ?? []).join("、")}）</span>
                )}
                {" · 陈旧率 "}
                <span style={{ color: ((layering.stale_rate ?? 0) > 0.3 ? "var(--accent)" : "var(--muted)"), fontWeight: 600 }}>
                  {((layering.stale_rate ?? 0) * 100).toFixed(0)}%
                </span>
                <span style={{ color: "var(--faint)" }}>（{layering.stale_days ?? 180}天阈值）</span>
              </div>
              {traceOpen && (
                <div className="flex flex-col gap-1.5 mt-2">
                  {(traces.forgot ?? []).length === 0 &&
                    (traces.inject ?? []).length === 0 && (
                      <div className="text-[11px]" style={{ color: "var(--faint)" }}>
                        暂无溯源记录（对话后会逐渐积累）
                      </div>
                    )}
                  {(traces.forgot ?? []).map((r, i) => (
                    <div key={`f${i}`} className="text-[11px] leading-relaxed" style={{ color: "var(--muted)" }}>
                      <span style={{ color: "var(--accent)" }}>
                        {r.action === "archived" ? "已归档" : "已删除"}
                      </span>
                      {" · "}
                      {r.section || "记忆"}
                      {r.date ? ` · ${r.date}` : ""}
                      {" · "}
                      {r.summary || ""}
                    </div>
                  ))}
                  {(traces.inject ?? []).slice(0, 3).map((r, i) => (
                    <div key={`i${i}`} className="text-[11px] leading-relaxed" style={{ color: "var(--muted)" }}>
                      <span style={{ color: "var(--accent)" }}>本轮注入</span>
                      {" · "}
                      {r.ts ? formatDate(new Date(r.ts).getTime()) : ""}
                      {r.n != null ? ` · ${r.n} 条` : ""}
                      {r.refs?.[0]?.summary ? ` · ${r.refs[0].summary.slice(0, 40)}` : ""}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </>
        )}
          </div>
        </div>
      )}
    </div>
  </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatDate(ts: number): string {
  const d = new Date(ts);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMin = Math.floor(diffMs / 60_000);

  if (diffMin < 1) return "刚刚";
  if (diffMin < 60) return `${diffMin} 分钟前`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr} 小时前`;
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}
