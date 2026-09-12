/**
 * XiaojuMascot — 小桔桌宠（透明 PNG + CSS 动画 + 情绪粒子）。
 *
 * 视觉分层：
 *   xj-root      → 粒子层（绝对定位，随情绪渲染星星/气泡/雨滴/火花）
 *   xj-wrapper   → 呼吸浮动（所有状态共有）
 *   xj-gesture   → 姿态动画（idle 轻摇 / walk 颠簸 / think 摆头 / happy 跳跃 / sad 低头 / focused 前倾）
 *   xj-facing    → 朝向翻转（walkDirection=left 时 scaleX(-1)）
 *   xj-img       → 小桔 PNG
 *
 * 情绪粒子：
 *   happy     → 星星/爱心从角色周围飘出
 *   thinking  → 头顶"..."思考气泡
 *   concerned → 头顶雨滴（沮丧）
 *   focused   → 周围小火花闪烁
 *
 * 接口与 CicadaMascot 兼容。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { EmotionName } from "./EmotionState";
import mascotImg from "../../assets/mascot-xiaoju.png";

interface XiaojuMascotProps {
  emotion?: EmotionName;
  walkDirection?: "left" | "right";
  isWalking?: boolean;
  scale?: number;
  mood?: number;
  vitality?: number;
  level?: number;
  onClick?: () => void;
  onDoubleClick?: () => void;
}

/* ------------------------------------------------------------------ */
/*  粒子子组件                                                          */
/* ------------------------------------------------------------------ */

/** 开心：星星+爱心从角色周围向上飘出 */
function HappyParticles({ size }: { size: number }) {
  const items = [
    { char: "★", dx: -40, dy: -60, delay: 0, dur: 1.6 },
    { char: "♥", dx: 30, dy: -70, delay: 0.2, dur: 1.8 },
    { char: "✦", dx: -20, dy: -50, delay: 0.4, dur: 1.5 },
    { char: "★", dx: 50, dy: -55, delay: 0.6, dur: 1.7 },
    { char: "♥", dx: -55, dy: -45, delay: 0.3, dur: 1.9 },
    { char: "✦", dx: 10, dy: -75, delay: 0.8, dur: 1.6 },
    { char: "★", dx: 60, dy: -40, delay: 0.5, dur: 1.4 },
    { char: "♥", dx: -10, dy: -65, delay: 0.1, dur: 1.8 },
  ];
  return (
    <>
      {items.map((p, i) => (
        <span
          key={i}
          className="xj-particle xj-happy-particle"
          style={{
            left: `${size * 0.5 + p.dx * 0.5}px`,
            top: `${size * 0.55 + p.dy * 0.3}px`,
            color: i % 3 === 0 ? "#ff6b9d" : i % 3 === 1 ? "#ffb84d" : "#ffd93d",
            fontSize: `${14 + (i % 3) * 4}px`,
            animationDelay: `${p.delay}s`,
            animationDuration: `${p.dur}s`,
            ["--dx" as string]: `${p.dx}px`,
            ["--dy" as string]: `${p.dy}px`,
          }}
        >
          {p.char}
        </span>
      ))}
    </>
  );
}

/** 思考：头顶"..."气泡 */
function ThinkingBubble({ size }: { size: number }) {
  return (
    <div
      className="xj-think-bubble"
      style={{ left: `${size * 0.5 - 28}px`, top: `${-size * 0.08}px` }}
    >
      <span className="xj-dot" style={{ animationDelay: "0s" }}>·</span>
      <span className="xj-dot" style={{ animationDelay: "0.2s" }}>·</span>
      <span className="xj-dot" style={{ animationDelay: "0.4s" }}>·</span>
    </div>
  );
}

/** 沮丧：头顶雨滴 */
function SadRain({ size }: { size: number }) {
  const drops = [
    { left: 0.38, delay: 0, dur: 1.2 },
    { left: 0.48, delay: 0.3, dur: 1.4 },
    { left: 0.58, delay: 0.6, dur: 1.1 },
    { left: 0.44, delay: 0.9, dur: 1.3 },
  ];
  return (
    <>
      {drops.map((d, i) => (
        <span
          key={i}
          className="xj-particle xj-rain-drop"
          style={{
            left: `${size * d.left}px`,
            top: `${size * 0.05}px`,
            animationDelay: `${d.delay}s`,
            animationDuration: `${d.dur}s`,
          }}
        >
          💧
        </span>
      ))}
    </>
  );
}

/** 专注：周围小火花 */
function FocusedSparks({ size }: { size: number }) {
  const sparks = [
    { left: 0.2, top: 0.4, delay: 0, dur: 0.8 },
    { left: 0.8, top: 0.5, delay: 0.3, dur: 1.0 },
    { left: 0.3, top: 0.75, delay: 0.6, dur: 0.9 },
    { left: 0.75, top: 0.8, delay: 0.15, dur: 1.1 },
  ];
  return (
    <>
      {sparks.map((s, i) => (
        <span
          key={i}
          className="xj-particle xj-spark"
          style={{
            left: `${size * s.left}px`,
            top: `${size * s.top}px`,
            animationDelay: `${s.delay}s`,
            animationDuration: `${s.dur}s`,
          }}
        >
          ✦
        </span>
      ))}
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  主组件                                                              */
/* ------------------------------------------------------------------ */

export function XiaojuMascot({
  emotion = "idle",
  walkDirection = "right",
  isWalking = false,
  scale = 1.0,
  mood = 80,
  vitality = 100,
  onClick,
  onDoubleClick,
}: XiaojuMascotProps) {
  const [bouncing, setBouncing] = useState(false);
  const clickTimerRef = useRef<number>(0);

  // 桌宠形象固定用打包内的高清默认图（src/assets/mascot-xiaoju.png）——
  // 换肤只改整站配色/装饰，永远不替换小桔自己的形象。
  // (Vite 处理 import 为带 hash 的 URL)
  const mascotUrl = mascotImg;

  const m = Math.max(0, Math.min(100, mood)) / 100;
  const v = Math.max(0, Math.min(100, vitality)) / 100;
  const speedBase = 1 / (0.5 + v * 0.7);
  const breatheAmp = 4 + m * 3;

  // 姿态动画：走路优先，其次按情绪
  const poseAnim = (() => {
    if (isWalking) {
      return { name: "xj-walk-bounce", dur: `${0.5 * speedBase}s` };
    }
    switch (emotion) {
      case "happy":
        return { name: "xj-happy-jump", dur: `${0.9 * speedBase}s` };
      case "thinking":
        return { name: "xj-think", dur: `${3.0 * speedBase}s` };
      case "concerned":
        return { name: "xj-sad", dur: `${3.5 * speedBase}s` };
      case "focused":
        return { name: "xj-focused", dur: `${1.2 * speedBase}s` };
      default:
        return { name: "xj-idle", dur: "7s" };
    }
  })();

  const breatheDuration = emotion === "happy" ? "2.0s" : emotion === "concerned" ? "4.0s" : "3.2s";
  const facing = walkDirection === "left" ? "scaleX(-1)" : "none";

  useEffect(() => {
    if (!bouncing) return;
    clickTimerRef.current = window.setTimeout(() => setBouncing(false), 420);
    return () => window.clearTimeout(clickTimerRef.current);
  }, [bouncing]);

  const handleClick = useCallback(() => {
    setBouncing(true);
    onClick?.();
  }, [onClick]);

  const size = Math.round(200 * scale);

  return (
    <div
      className="relative cursor-pointer select-none"
      style={{ width: size, height: size }}
      onClick={handleClick}
      onDoubleClick={onDoubleClick}
      role="button"
      tabIndex={0}
      aria-label="小桔桌宠"
    >
      <style>{`
        @keyframes xj-breathe {
          0%, 100% { transform: translateY(0) scale(1); }
          50% { transform: translateY(-${breatheAmp * scale}px) scale(1.02); }
        }
        @keyframes xj-bounce {
          0% { transform: translateY(0) scale(1); }
          35% { transform: translateY(-${20 * scale}px) scale(1.08, 0.92); }
          65% { transform: translateY(4px) scale(0.95, 1.05); }
          100% { transform: translateY(0) scale(1); }
        }
        /* 走路：上下颠簸 + 轻微左右倾 */
        @keyframes xj-walk-bounce {
          0%, 100% { transform: translateY(0) rotate(0deg) scale(1, 1); }
          25% { transform: translateY(-7px) rotate(3deg) scale(1.02, 0.98); }
          50% { transform: translateY(0) rotate(0deg) scale(1, 1); }
          75% { transform: translateY(-5px) rotate(-3deg) scale(1.02, 0.98); }
        }
        @keyframes xj-idle {
          0%, 100% { transform: rotate(0deg); }
          50% { transform: rotate(1.6deg); }
        }
        @keyframes xj-think {
          0%, 100% { transform: rotate(-4deg) translateY(0); }
          50% { transform: rotate(4deg) translateY(-3px); }
        }
        /* 开心：跳跃 + 左右摆 */
        @keyframes xj-happy-jump {
          0%, 100% { transform: translateY(0) scale(1) rotate(0deg); }
          25% { transform: translateY(-22px) scale(1.06, 0.94) rotate(-6deg); }
          45% { transform: translateY(-26px) scale(0.94, 1.08) rotate(6deg); }
          65% { transform: translateY(-8px) scale(1.03) rotate(-3deg); }
          80% { transform: translateY(0) scale(1) rotate(2deg); }
        }
        /* 沮丧：低头 + 垂肩（压缩Y） */
        @keyframes xj-sad {
          0%, 100% { transform: translateY(5px) scale(0.95, 1.04) rotate(-4deg); }
          50% { transform: translateY(7px) scale(0.93, 1.06) rotate(-5deg); }
        }
        /* 专注：前倾 + 小点头 */
        @keyframes xj-focused {
          0%, 100% { transform: rotate(3deg) translateY(0) scale(1.03); }
          50% { transform: rotate(5deg) translateY(-2px) scale(1.05); }
        }
        /* 思考气泡点 */
        @keyframes xj-dot-blink {
          0%, 20% { opacity: 0.15; transform: translateY(0); }
          50% { opacity: 1; transform: translateY(-4px); }
          80%, 100% { opacity: 0.15; transform: translateY(0); }
        }
        /* 开心粒子：向上飘出 */
        @keyframes xj-star-float {
          0% { opacity: 0; transform: translate(0, 0) scale(0.4) rotate(0deg); }
          15% { opacity: 1; }
          100% { opacity: 0; transform: translate(var(--dx), var(--dy)) scale(1.3) rotate(200deg); }
        }
        /* 雨滴落下 */
        @keyframes xj-rain-fall {
          0% { opacity: 0; transform: translateY(-8px) scale(0.8); }
          20% { opacity: 0.85; }
          100% { opacity: 0; transform: translateY(36px) scale(1); }
        }
        /* 火花闪烁 */
        @keyframes xj-spark-flash {
          0%, 100% { opacity: 0; transform: scale(0.4) rotate(0deg); }
          50% { opacity: 1; transform: scale(1.3) rotate(90deg); }
        }
        .xj-wrapper {
          animation: xj-breathe ${breatheDuration} ease-in-out infinite;
          transform-origin: center bottom;
        }
        .xj-wrapper.bouncing {
          animation: xj-bounce 0.42s ease-out;
        }
        .xj-gesture {
          transform-origin: center bottom;
          animation: ${poseAnim.name} ${poseAnim.dur} ease-in-out infinite;
        }
        .xj-facing {
          transform: ${facing};
          transform-origin: center center;
          width: 100%;
          height: 100%;
        }
        .xj-img {
          width: 100%;
          height: 100%;
          object-fit: contain;
          display: block;
          pointer-events: none;
          filter: drop-shadow(0 6px 12px rgba(0, 0, 0, 0.12));
        }
        /* 粒子通用 */
        .xj-particle {
          position: absolute;
          pointer-events: none;
          user-select: none;
          z-index: 5;
        }
        .xj-happy-particle {
          animation: xj-star-float 1.6s ease-out infinite;
          text-shadow: 0 0 6px rgba(255, 184, 77, 0.6);
        }
        .xj-think-bubble {
          position: absolute;
          display: flex;
          gap: 3px;
          align-items: center;
          justify-content: center;
          width: 56px;
          height: 24px;
          background: rgba(255, 255, 255, 0.92);
          border-radius: 14px;
          box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12);
          z-index: 5;
          pointer-events: none;
        }
        .xj-dot {
          font-size: 18px;
          font-weight: 700;
          color: #888;
          line-height: 1;
          animation: xj-dot-blink 1.2s ease-in-out infinite;
        }
        .xj-rain-drop {
          font-size: 12px;
          animation: xj-rain-fall 1.2s ease-in infinite;
          opacity: 0.7;
        }
        .xj-spark {
          font-size: 14px;
          color: #ffb84d;
          text-shadow: 0 0 8px rgba(255, 184, 77, 0.8);
          animation: xj-spark-flash 0.9s ease-in-out infinite;
        }
      `}</style>

      {/* 情绪粒子层 */}
      {emotion === "happy" && <HappyParticles size={size} />}
      {emotion === "thinking" && <ThinkingBubble size={size} />}
      {emotion === "concerned" && <SadRain size={size} />}
      {emotion === "focused" && <FocusedSparks size={size} />}

      {/* 角色层 */}
      <div
        className={`xj-wrapper ${bouncing ? "bouncing" : ""}`}
        style={{ width: size, height: size }}
      >
        <div className="xj-gesture" style={{ width: size, height: size }}>
          <div className="xj-facing">
            <img src={mascotUrl} alt="小桔" className="xj-img" draggable={false} />
          </div>
        </div>
      </div>
    </div>
  );
}
