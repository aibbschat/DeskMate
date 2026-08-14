/**
 * CicadaMascot — 知了桌宠（分层 SVG + CSS 动画）。
 *
 * 基于用户提供的水墨蝉参考图重绘：
 *   - 头 / 胸 / 腹 / 左右翅膀 / 眼 / 腮红 / 触须 / 腿 分层绘制
 *   - 左右翅膀独立扇动（以翅根为轴旋转）
 *   - 整体呼吸浮动
 *   - 点击弹跳反馈
 *   - 情绪驱动差异化动作（思考摆头 / 开心扭身 / 关注颤抖 / 专注前倾 / 闲时张望）
 *
 * 接口与原 Mascot 兼容：walkDirection / isWalking 在 corner 模式下忽略。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { EmotionName } from "./EmotionState";

interface CicadaMascotProps {
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

const BODY_INK = "#232320";
const BODY_INK_LIGHT = "#3a3a36";
const WING_STROKE = "rgba(120, 160, 155, 0.55)";
const VEIN_STROKE = "rgba(140, 185, 180, 0.45)";
const BLUSH = "rgba(240, 160, 160, 0.45)";

export function CicadaMascot({
  emotion = "idle",
  scale = 1.0,
  mood = 80,
  vitality = 100,
  onClick,
  onDoubleClick,
}: CicadaMascotProps) {
  const [bouncing, setBouncing] = useState(false);
  const clickTimerRef = useRef<number>(0);

  // 情绪 → 翅膀扇动参数
  const flapParams = (() => {
    const m = Math.max(0, Math.min(100, mood)) / 100;
    const v = Math.max(0, Math.min(100, vitality)) / 100;
    const base = 1.0 / (0.4 + v * 0.8); // 活力高扇得快
    switch (emotion) {
      case "happy":
        return { duration: `${0.5 * base}s`, amplitude: 26 + m * 10 };
      case "thinking":
        return { duration: `${1.9 * base}s`, amplitude: 9 + m * 4 };
      case "concerned":
        return { duration: `${1.4 * base}s`, amplitude: 7 + m * 3 };
      case "focused":
        return { duration: `${1.0 * base}s`, amplitude: 14 + m * 5 };
      default:
        return { duration: `${1.1 * base}s`, amplitude: 18 + m * 7 };
    }
  })();

  // 情绪 → 身体整体动作（叠加在呼吸之上，做出可区分的姿态）
  const gestureClass: Record<EmotionName, string> = {
    idle: "cicada-g-idle",
    thinking: "cicada-g-think",
    happy: "cicada-g-happy",
    concerned: "cicada-g-concerned",
    focused: "cicada-g-focused",
  };
  // 思考时头部左右摆；其余情绪头部不动
  const headClass = emotion === "thinking" ? "cicada-head cicada-head-tilt" : "cicada-head";
  // 闲时眼睛左右张望，增加生命感
  const eyesClass = emotion === "idle" ? "cicada-eyes cicada-eyes-look" : "cicada-eyes";

  const breatheDuration = emotion === "happy" ? "2.2s" : "3.2s";

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
      aria-label="知了桌宠"
    >
      <style>{`
        @keyframes cicada-breathe {
          0%, 100% { transform: translateY(0px) scale(1); }
          50% { transform: translateY(-${5 * scale}px) scale(1.018); }
        }
        @keyframes cicada-flap-left {
          0%, 100% { transform: rotate(0deg); }
          50% { transform: rotate(${flapParams.amplitude}deg); }
        }
        @keyframes cicada-flap-right {
          0%, 100% { transform: rotate(0deg); }
          50% { transform: rotate(-${flapParams.amplitude}deg); }
        }
        @keyframes cicada-bounce {
          0% { transform: translateY(0) scale(1); }
          35% { transform: translateY(-${20 * scale}px) scale(1.08, 0.92); }
          65% { transform: translateY(4px) scale(0.95, 1.05); }
          100% { transform: translateY(0) scale(1); }
        }
        @keyframes cicada-antennae {
          0%, 100% { transform: rotate(-1.5deg); }
          50% { transform: rotate(1.5deg); }
        }
        /* 情绪动作：身体整体姿态 */
        @keyframes cicada-g-idle {
          0%, 100% { transform: rotate(0deg); }
          50% { transform: rotate(1.6deg); }
        }
        @keyframes cicada-g-think {
          0%, 100% { transform: rotate(-3.5deg); }
          50% { transform: rotate(3.5deg); }
        }
        @keyframes cicada-g-happy {
          0%, 100% { transform: rotate(-6deg); }
          50% { transform: rotate(6deg); }
        }
        @keyframes cicada-g-concerned {
          0%   { transform: translateX(-2px) rotate(-1deg); }
          25%  { transform: translateX(2px) rotate(1deg); }
          50%  { transform: translateX(-1.5px) rotate(-0.5deg); }
          75%  { transform: translateX(1.5px) rotate(0.5deg); }
          100% { transform: translateX(0) rotate(0deg); }
        }
        @keyframes cicada-g-focused {
          0%, 100% { transform: scale(1.05); }
          50% { transform: scale(1.0); }
        }
        /* 思考：头部左右摆 */
        @keyframes cicada-head-tilt {
          0%, 100% { transform: rotate(-9deg); }
          50% { transform: rotate(9deg); }
        }
        /* 闲时：眼睛左右张望 */
        @keyframes cicada-eyes-look {
          0%, 100% { transform: translateX(0); }
          20% { transform: translateX(3px); }
          40% { transform: translateX(3px); }
          60% { transform: translateX(-3px); }
          80% { transform: translateX(-3px); }
        }
        .cicada-wrapper {
          animation: cicada-breathe ${breatheDuration} ease-in-out infinite;
          transform-origin: 100px 180px;
        }
        .cicada-wrapper.bouncing {
          animation: cicada-bounce 0.42s ease-out;
        }
        .cicada-gesture {
          transform-origin: 100px 130px;
        }
        .cicada-g-idle { animation: cicada-g-idle 7s ease-in-out infinite; }
        .cicada-g-think { animation: cicada-g-think 3.5s ease-in-out infinite; }
        .cicada-g-happy { animation: cicada-g-happy 0.7s ease-in-out infinite; }
        .cicada-g-concerned { animation: cicada-g-concerned 0.22s linear infinite; }
        .cicada-g-focused { animation: cicada-g-focused 2.6s ease-in-out infinite; }
        .cicada-wing-left {
          animation: cicada-flap-left ${flapParams.duration} ease-in-out infinite;
          transform-origin: 72px 88px;
        }
        .cicada-wing-right {
          animation: cicada-flap-right ${flapParams.duration} ease-in-out infinite;
          transform-origin: 128px 88px;
        }
        .cicada-head {
          transform-origin: 100px 58px;
        }
        .cicada-head-tilt {
          animation: cicada-head-tilt 1.7s ease-in-out infinite;
        }
        .cicada-eyes {
          transform-origin: 100px 54px;
        }
        .cicada-eyes-look {
          animation: cicada-eyes-look 5s ease-in-out infinite;
        }
        .cicada-antennae {
          animation: cicada-antennae 2.4s ease-in-out infinite;
          transform-origin: 100px 45px;
        }
      `}</style>

      <svg
        viewBox="0 0 200 240"
        className={`cicada-wrapper ${bouncing ? "bouncing" : ""}`}
        style={{ width: size, height: size, display: "block" }}
      >
        <defs>
          <radialGradient id="cicadaBodyGrad" cx="40%" cy="30%" r="80%">
            <stop offset="0%" stopColor={BODY_INK_LIGHT} />
            <stop offset="55%" stopColor={BODY_INK} />
            <stop offset="100%" stopColor="#141412" />
          </radialGradient>
          <radialGradient id="cicadaHeadGrad" cx="45%" cy="35%" r="75%">
            <stop offset="0%" stopColor="#353531" />
            <stop offset="70%" stopColor={BODY_INK} />
            <stop offset="100%" stopColor="#131311" />
          </radialGradient>
          <linearGradient id="cicadaAbdomenGrad" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" stopColor={BODY_INK} />
            <stop offset="50%" stopColor="#2c2c29" />
            <stop offset="100%" stopColor="#181816" />
          </linearGradient>
          <linearGradient id="cicadaWingGrad" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor="rgba(210, 245, 240, 0.45)" />
            <stop offset="60%" stopColor="rgba(180, 220, 215, 0.28)" />
            <stop offset="100%" stopColor="rgba(160, 205, 200, 0.18)" />
          </linearGradient>
        </defs>

        {/* 身体整体姿态层（情绪动作叠加在呼吸之上） */}
        <g className={`cicada-gesture ${gestureClass[emotion]}`}>
          {/* 左翅膀 */}
          <g className="cicada-wing-left">
            <path
              d="M72 88 C 45 95, 18 130, 16 168 C 15 188, 28 202, 48 196 C 65 190, 82 160, 86 130 C 90 105, 82 92, 72 88 Z"
              fill="url(#cicadaWingGrad)"
              stroke={WING_STROKE}
              strokeWidth="1.2"
            />
            {/* 翅脉 */}
            <path
              d="M72 88 Q 52 120, 48 160 M72 88 Q 38 135, 38 170 M72 88 Q 30 125, 24 150 M72 88 Q 58 110, 56 140 M72 88 Q 65 130, 64 165"
              fill="none"
              stroke={VEIN_STROKE}
              strokeWidth="0.8"
              strokeLinecap="round"
            />
          </g>

          {/* 右翅膀 */}
          <g className="cicada-wing-right">
            <path
              d="M128 88 C 155 95, 182 130, 184 168 C 185 188, 172 202, 152 196 C 135 190, 118 160, 114 130 C 110 105, 118 92, 128 88 Z"
              fill="url(#cicadaWingGrad)"
              stroke={WING_STROKE}
              strokeWidth="1.2"
            />
            {/* 翅脉 */}
            <path
              d="M128 88 Q 148 120, 152 160 M128 88 Q 162 135, 162 170 M128 88 Q 170 125, 176 150 M128 88 Q 142 110, 144 140 M128 88 Q 135 130, 136 165"
              fill="none"
              stroke={VEIN_STROKE}
              strokeWidth="0.8"
              strokeLinecap="round"
            />
          </g>

          {/* 腿（在身体后层） */}
          <g stroke={BODY_INK} strokeWidth="2.2" strokeLinecap="round" fill="none">
            {/* 左前腿 */}
            <path d="M78 92 L 58 112 L 48 108" />
            {/* 左中腿 */}
            <path d="M74 110 L 52 132 L 42 128" />
            {/* 左后腿 */}
            <path d="M76 130 L 54 158 L 46 156" />
            {/* 右前腿 */}
            <path d="M122 92 L 142 112 L 152 108" />
            {/* 右中腿 */}
            <path d="M126 110 L 148 132 L 158 128" />
            {/* 右后腿 */}
            <path d="M124 130 L 146 158 L 154 156" />
          </g>

          {/* 腹部 */}
          <g>
            <ellipse cx="100" cy="162" rx="28" ry="52" fill="url(#cicadaAbdomenGrad)" />
            {/* 腹节 */}
            <g stroke="rgba(255,255,255,0.12)" strokeWidth="1" fill="none">
              <path d="M78 138 Q 100 145, 122 138" />
              <path d="M75 154 Q 100 162, 125 154" />
              <path d="M76 170 Q 100 178, 124 170" />
              <path d="M82 186 Q 100 192, 118 186" />
              <path d="M90 200 Q 100 204, 110 200" />
            </g>
          </g>

          {/* 胸部 */}
          <g>
            <ellipse cx="100" cy="98" rx="34" ry="28" fill="url(#cicadaBodyGrad)" />
            {/* 胸甲纹路 */}
            <path
              d="M84 92 Q 100 104, 116 92 M86 104 Q 100 114, 114 104"
              fill="none"
              stroke="rgba(255,255,255,0.14)"
              strokeWidth="1.2"
              strokeLinecap="round"
            />
          </g>

          {/* 头部（可整体摆头） */}
          <g className={headClass}>
            <ellipse cx="100" cy="58" rx="36" ry="32" fill="url(#cicadaHeadGrad)" />

            {/* 眼睛（可左右张望） */}
            <g className={eyesClass}>
              <circle cx="72" cy="54" r="11" fill="#111" />
              <circle cx="128" cy="54" r="11" fill="#111" />
              <circle cx="75" cy="51" r="3.2" fill="rgba(255,255,255,0.85)" />
              <circle cx="131" cy="51" r="3.2" fill="rgba(255,255,255,0.85)" />
              <circle cx="70" cy="57" r="1.6" fill="rgba(255,255,255,0.45)" />
              <circle cx="126" cy="57" r="1.6" fill="rgba(255,255,255,0.45)" />
            </g>

            {/* 腮红 */}
            <ellipse cx="76" cy="72" rx="5.5" ry="3.5" fill={BLUSH} />
            <ellipse cx="124" cy="72" rx="5.5" ry="3.5" fill={BLUSH} />

            {/* 嘴巴 */}
            <path
              d="M94 70 Q 100 74, 106 70"
              fill="none"
              stroke="rgba(255,255,255,0.55)"
              strokeWidth="1.4"
              strokeLinecap="round"
            />

            {/* 触须 */}
            <g className="cicada-antennae">
              <path
                d="M88 34 Q 80 16, 68 10"
                fill="none"
                stroke={BODY_INK}
                strokeWidth="1.6"
                strokeLinecap="round"
              />
              <path
                d="M112 34 Q 120 16, 132 10"
                fill="none"
                stroke={BODY_INK}
                strokeWidth="1.6"
                strokeLinecap="round"
              />
            </g>
          </g>
        </g>
      </svg>
    </div>
  );
}
