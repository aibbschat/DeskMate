import { useEffect, useState } from "react";
import { Icon } from "../Icon";

export interface MemoryLearnedToastProps {
  /** 本轮新记住的事实文本列表。 */
  facts: string[];
  /** 自动隐藏毫秒数（默认 7000）。 */
  autoHideMs?: number;
  onDone: () => void;
}

/**
 * 「我刚记住了」短提示 —— 让「越用越懂你」变得可见、可信任。
 * 仅展示几秒后自动消失，不干扰对话。
 */
export function MemoryLearnedToast({ facts, autoHideMs = 7000, onDone }: MemoryLearnedToastProps) {
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    const t = setTimeout(() => {
      setVisible(false);
      setTimeout(onDone, 250);
    }, autoHideMs);
    return () => clearTimeout(t);
  }, [autoHideMs, onDone]);

  if (!visible || facts.length === 0) return null;

  const preview =
    facts.length === 1
      ? facts[0]
      : `① ${facts[0]}` + (facts[1] ? `　② ${facts[1]}` : "");

  return (
    <div
      className="fixed left-4 bottom-4 z-[46] max-w-[360px]"
      role="status"
      aria-live="polite"
      style={{
        opacity: visible ? 1 : 0,
        transform: visible ? "translateY(0)" : "translateY(6px)",
        transition: "opacity 0.25s ease, transform 0.25s ease",
      }}
    >
      <div
        className="flex items-start gap-2.5 px-3.5 py-2.5 rounded-xl border shadow-lg"
        style={{
          background: "var(--panel)",
          borderColor: "var(--line)",
          boxShadow: "0 12px 36px rgba(0,0,0,0.14)",
        }}
      >
        <div
          className="flex-shrink-0 w-6 h-6 rounded-lg flex items-center justify-center"
          style={{ background: "var(--accent-soft)", color: "var(--accent)" }}
        >
          <Icon name="sparkle" size={13} />
        </div>
        <div className="min-w-0">
          <div
            className="text-[12px] font-semibold mb-0.5"
            style={{ color: "var(--ink)" }}
          >
            我刚记住了
            {facts.length > 2 ? `（${facts.length} 条）` : ""}
          </div>
          <div
            className="text-[12.5px] leading-snug"
            style={{ color: "var(--muted)" }}
          >
            {preview}
            {facts.length > 2 ? "…" : ""}
          </div>
        </div>
      </div>
    </div>
  );
}
