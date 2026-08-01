import { useState, useCallback, useEffect, useRef } from "react";
import { Icon } from "../Icon";
import { Markdown } from "../Markdown";

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

  const current = files.find((f) => f.id === currentId) ?? files[0];

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
