/**
 * CareToast — P2-5 连续情感关怀的温暖呈现。
 *
 * 与 SuggestionToast（创作建议，采纳/忽略按钮）不同：
 * - 纯粹的关心问候，不是任务建议
 * - 温暖样式（暖米白 + 柔和边框 + 圆角），emoji 图标按文案匹配
 * - 只有关闭按钮（"好的"），无采纳/忽略
 * - 展示时间更长（30 秒），给用户时间感受温暖
 * - 位置：右上角（与 SuggestionToast 同位，但视觉区分明显）
 */

import { useCallback, useEffect, useRef, useState } from "react";

export interface CareMessage {
  /** 唯一标识 */
  id: string;
  /** 关怀文案 */
  text: string;
  /** 建议类型（emotional_care / companion） */
  type?: string;
  /** 置信度 */
  confidence?: number;
  /** 微区分：care=情感关怀（小桔的关心+情感emoji），companion=专注陪伴（小桔的陪伴+⏱️） */
  kind?: "care" | "companion";
}

export interface CareToastProps {
  message: CareMessage | null;
  /** 用户点击关闭或自动消失时调用 */
  onClose: (message: CareMessage) => void;
  /** 自动消失时间（默认 30 秒） */
  autoDismissMs?: number;
}

/** 根据关怀文案关键词匹配 emoji 图标，多样化呈现。 */
export function careEmoji(text: string): string {
  const t = text || "";
  if (/咖啡|茶|赶稿|辛苦|忙了|累|熬夜|加班/.test(t)) return "☕";
  if (/压力|放松|心情|紧张|焦虑|烦|闷/.test(t)) return "🌸";
  if (/休息|工作.*久|眼睛|歇|伸懒腰|活动一下/.test(t)) return "😊";
  if (/加油|鼓励|进步|棒|厉害|做得好|肯定/.test(t)) return "💪";
  if (/周末|假期|出去玩|散步|阳光/.test(t)) return "🌿";
  if (/冷|热|天气|添衣|喝水/.test(t)) return "🍵";
  return "💚";
}

export function CareToast({
  message,
  onClose,
  autoDismissMs = 30_000,
}: CareToastProps) {
  const [phase, setPhase] = useState<"enter" | "show" | "exit">("enter");
  const [paused, setPaused] = useState(false);
  const dismissTimerRef = useRef<ReturnType<typeof setTimeout>>();

  const startDismissTimer = useCallback(() => {
    if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current);
    dismissTimerRef.current = setTimeout(() => {
      setPhase("exit");
      setTimeout(() => {
        if (message) onClose(message);
      }, 300);
    }, autoDismissMs);
  }, [autoDismissMs, message, onClose]);

  useEffect(() => {
    if (message) {
      setPhase("enter");
      // 下一帧切到 show，触发进入动画
      requestAnimationFrame(() => {
        requestAnimationFrame(() => setPhase("show"));
      });
      startDismissTimer();
    }
    return () => {
      if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current);
    };
  }, [message, startDismissTimer]);

  const handleClose = useCallback(() => {
    setPhase("exit");
    if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current);
    setTimeout(() => {
      if (message) onClose(message);
    }, 300);
  }, [message, onClose]);

  if (!message) return null;

  const kind = message.kind || "care";
  // 微区分：关怀用情感 emoji（按文案匹配），陪伴固定用 ⏱️（时间/专注）
  const emoji = kind === "companion" ? "⏱️" : careEmoji(message.text);
  // 微区分：关怀「小桔的关心」，陪伴「小桔的陪伴」
  const title = kind === "companion" ? "小桔的陪伴" : "小桔的关心";

  return (
    <div
      className={`care-toast care-toast-${phase}`}
      onMouseEnter={() => {
        setPaused(true);
        if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current);
      }}
      onMouseLeave={() => {
        setPaused(false);
        if (!paused) startDismissTimer();
      }}
      role="status"
      aria-live="polite"
    >
      <div className="care-toast-icon">{emoji}</div>
      <div className="care-toast-body">
        <div className="care-toast-title">{title}</div>
        <div className="care-toast-text">{message.text}</div>
      </div>
      <button
        type="button"
        className="care-toast-close"
        onClick={handleClose}
        aria-label="关闭"
      >
        好的
      </button>
    </div>
  );
}
