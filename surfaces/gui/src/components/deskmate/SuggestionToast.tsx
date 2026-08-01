import { useState, useEffect, useCallback, useRef } from "react";
import { Icon } from "../Icon";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface Suggestion {
  /** Unique identifier. */
  id: string;
  /** Suggestion type (for dedup). */
  type?: string;
  /** The suggestion text. */
  text: string;
  /** Confidence (0 - 1). */
  confidence?: number;
  /** Optional context explaining why this suggestion was made. */
  reason?: string;
  /** Timestamp (unix ms). */
  timestamp: number;
}

export interface SuggestionToastProps {
  /** The suggestion to display, or null to hide. */
  suggestion: Suggestion | null;
  /** Called when the user accepts the suggestion. */
  onAccept: (suggestion: Suggestion) => void;
  /** Called when the user dismisses the suggestion (clicks "忽略" or X). */
  onDismiss: (suggestion: Suggestion) => void;
  /** Called when the suggestion auto-dismisses after timeout (NOT a user action).
   *  If not provided, auto-dismiss falls back to onDismiss. */
  onAutoDismiss?: (suggestion: Suggestion) => void;
  /** Auto-dismiss timeout in ms (default 12000). */
  autoDismissMs?: number;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function SuggestionToast({
  suggestion,
  onAccept,
  onDismiss,
  onAutoDismiss,
  autoDismissMs = 12000,
}: SuggestionToastProps) {
  const [phase, setPhase] = useState<"enter" | "show" | "exit">("enter");
  const [paused, setPaused] = useState(false);
  const dismissTimerRef = useRef<ReturnType<typeof setTimeout>>();

  // 启动自动消失计时器（移出弹窗时重新计时）
  const startDismissTimer = useCallback(() => {
    if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current);
    dismissTimerRef.current = setTimeout(() => {
      setPhase("exit");
      // Let the exit animation finish before calling dismiss
      // 自动消失用 onAutoDismiss（不触发后端 record_feedback），
      // 只有用户主动点「忽略」才走 onDismiss
      setTimeout(() => {
        if (suggestion) {
          if (onAutoDismiss) onAutoDismiss(suggestion);
          else onDismiss(suggestion);
        }
      }, 300);
    }, autoDismissMs);
  }, [autoDismissMs, onDismiss, onAutoDismiss, suggestion]);

  // Reset phase when a new suggestion arrives
  useEffect(() => {
    if (!suggestion) {
      setPhase("exit");
      return;
    }
    setPhase("enter");
    setPaused(false);
    const enterTimer = setTimeout(() => setPhase("show"), 50);
    startDismissTimer();

    return () => {
      clearTimeout(enterTimer);
      if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current);
    };
  }, [suggestion, startDismissTimer]);

  const handleAccept = useCallback(() => {
    if (!suggestion) return;
    if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current);
    setPhase("exit");
    setTimeout(() => onAccept(suggestion), 200);
  }, [suggestion, onAccept]);

  const handleDismiss = useCallback(() => {
    if (!suggestion) return;
    if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current);
    setPhase("exit");
    setTimeout(() => onDismiss(suggestion), 200);
  }, [suggestion, onDismiss]);

  // 鼠标移入：暂停自动消失（含进度条动画），让用户看完；移出：恢复计时
  const handleMouseEnter = useCallback(() => {
    setPaused(true);
    if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current);
  }, []);
  const handleMouseLeave = useCallback(() => {
    setPaused(false);
    if (suggestion) startDismissTimer();
  }, [suggestion, startDismissTimer]);

  if (!suggestion) return null;

  const confidencePct =
    suggestion.confidence !== undefined
      ? Math.round(suggestion.confidence * 100)
      : null;

  const opacity = phase === "show" ? 1 : 0;
  const translateY = phase === "enter" ? -8 : phase === "exit" ? -4 : 0;

  return (
    <div
      className="fixed top-3 right-3 z-[45]"
      style={{
        width: 320,
        opacity,
        transform: `translateY(${translateY}px)`,
        transition: "opacity 0.25s ease, transform 0.25s ease",
        pointerEvents: phase === "show" ? "auto" : "none",
      }}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
      role="alert"
      aria-live="polite"
    >
      <div
        className="rounded-xl border shadow-lg overflow-hidden"
        style={{
          background: "var(--panel)",
          borderColor: "var(--line)",
          boxShadow: "0 12px 36px rgba(0,0,0,0.14)",
        }}
      >
        {/* Header */}
        <div className="flex items-start gap-2.5 px-3.5 pt-3 pb-0">
          {/* Animated sparkle icon */}
          <div
            className="flex-shrink-0 w-7 h-7 rounded-lg flex items-center justify-center"
            style={{ background: "var(--accent-soft)", color: "var(--accent)" }}
          >
            <Icon name="sparkle" size={14} />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <span
                className="text-[12.5px] font-semibold"
                style={{ color: "var(--ink)" }}
              >
                建议
              </span>
                {confidencePct !== null && (
                  <span
                    className="text-[10.5px] px-1.5 py-0.5 rounded"
                    style={{
                      background: "var(--accent-soft)",
                      color: "var(--accent)",
                    }}
                  >
                    把握 {confidencePct}%
                  </span>
                )}
            </div>
          </div>
          <button
            className="flex-shrink-0 w-6 h-6 rounded flex items-center justify-center"
            style={{ color: "var(--faint)" }}
            onClick={handleDismiss}
            aria-label="Dismiss"
          >
            <Icon name="x" size={14} />
          </button>
        </div>

        {/* Suggestion body */}
        <div
          className="px-3.5 pt-1.5 pb-2"
          style={{ color: "var(--muted)", fontSize: "13px", lineHeight: 1.5 }}
        >
          {suggestion.text}
        </div>

        {/* Reason (if provided) */}
        {suggestion.reason && (
          <div
            className="mx-3.5 mb-2 px-2 py-1.5 rounded-lg text-[11.5px]"
            style={{
              background: "var(--paper)",
              color: "var(--faint)",
              lineHeight: 1.4,
            }}
          >
            {suggestion.reason}
          </div>
        )}

        {/* Actions */}
        <div
          className="flex items-center gap-2 px-3.5 py-2.5"
          style={{ borderTop: "1px solid var(--line)" }}
        >
          <button
            className="text-[12.5px] font-semibold px-3 py-1.5 rounded-lg border transition-colors"
            style={{
              background: "var(--accent)",
              borderColor: "var(--accent)",
              color: "#fff",
            }}
            onClick={handleAccept}
          >
            接受
          </button>
          <button
            className="text-[12.5px] px-3 py-1.5 rounded-lg border transition-colors"
            style={{
              background: "transparent",
              borderColor: "var(--line)",
              color: "var(--muted)",
            }}
            onClick={handleDismiss}
          >
            忽略
          </button>
          <div className="flex-1" />
          <span className="text-[10px]" style={{ color: "var(--faint)" }}>
            {formatTime(suggestion.timestamp)}
          </span>
        </div>

        {/* Auto-dismiss progress bar */}
        <div
          className="h-[2px] overflow-hidden"
          style={{ background: "var(--line)" }}
        >
          <div
            className="h-full"
            style={{
              background: "var(--faint)",
              animation: `dm-toast-drain ${autoDismissMs}ms linear forwards`,
              animationPlayState: paused ? "paused" : "running",
            }}
          />
        </div>
      </div>

      {/* Inject the drain keyframe */}
      <style>{`
        @keyframes dm-toast-drain {
          from { width: 100%; }
          to   { width: 0%; }
        }
      `}</style>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatTime(ts: number): string {
  const d = new Date(ts);
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
