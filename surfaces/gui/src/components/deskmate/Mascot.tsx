/**
 * Mascot — 墨灵桌宠。替代 BobaPet 的卡通 sprite 表现层。
 *
 * 保留 BobaPet 的 props 接口（emotion / walkDirection / isWalking / scale /
 * onClick / onDoubleClick），但视觉换成 AI 水墨图 mok-dling.png，并叠加水墨动效：
 *  - 主体：<img> 墨灵 + SVG feTurbulence 墨渗滤镜（边缘如水墨在宣纸上渗化）
 *  - 呼吸晕染（scale 呼吸）+ 轻柔漂浮（translateY），靠嵌套 div 分层避免 transform 冲突
 *  - 情绪：墨灵自身缩放/旋转 + Canvas 水墨符号（思考墨晕 / 开心墨点 / 专注墨放大镜）
 *  - 移动：外层 useMascotWalk 的 left/top 驱动；内部 Canvas 画墨拖尾
 *  - 入场：墨落纸晕开（首次挂载：img 淡入 + canvas 墨晕扩散圈）
 *
 * 行为骨架（游走 / 情绪状态机 / 单击双击）完全沿用上层 DeskMateOverlay + useMascotWalk，
 * 本组件只负责"墨灵形象 + 水墨动态"的表现。
 */

import { useEffect, useRef } from "react";
import type { EmotionName } from "./EmotionState";

interface MascotProps {
  emotion: EmotionName;
  walkDirection?: "left" | "right";
  isWalking?: boolean;
  scale?: number;
  onClick?: () => void;
  onDoubleClick?: () => void;
}

/** 全局 SVG 墨渗滤镜（边缘如水墨在宣纸上渗化）。 */
function InkFilterDefs() {
  return (
    <svg width="0" height="0" style={{ position: "absolute", width: 0, height: 0 }} aria-hidden>
      <defs>
        <filter id="mok-ink" x="-25%" y="-25%" width="150%" height="150%">
          <feTurbulence type="fractalNoise" baseFrequency="0.022" numOctaves="2" seed="7" result="n" />
          <feDisplacementMap in="SourceGraphic" in2="n" scale="4" xChannelSelector="R" yChannelSelector="G" />
        </filter>
      </defs>
    </svg>
  );
}

/** 情绪 → 主体 transform（缩放/旋转）。过渡靠 CSS transition。 */
const EMOTION_BODY: Record<EmotionName, { scale: number; rotate: number }> = {
  idle: { scale: 1, rotate: 0 },
  thinking: { scale: 0.97, rotate: 0 },
  happy: { scale: 1.05, rotate: 0 },
  concerned: { scale: 0.95, rotate: -2 },
  focused: { scale: 0.98, rotate: 0 },
};

export function Mascot({
  emotion,
  walkDirection = "right",
  isWalking = false,
  scale = 1.0,
  onClick,
  onDoubleClick,
}: MascotProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  const baseW = Math.round(180 * scale);
  const baseH = Math.round(195 * scale);

  const body = EMOTION_BODY[emotion] ?? EMOTION_BODY.idle;
  const flipX = walkDirection === "left";

  // Canvas 水墨氛围层：拖尾 + 情绪符号 + 入场晕开
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = baseW * dpr;
    canvas.height = baseH * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const w = baseW;
    const h = baseH;
    const cx = w / 2;
    const cy = h / 2;

    const trail: { x: number; y: number; born: number }[] = [];
    const start = performance.now();
    let raf = 0;

    const draw = (now: number) => {
      const elapsed = now - start;
      ctx.clearRect(0, 0, w, h);

      // 入场：墨落纸晕开（前 900ms）
      if (elapsed < 900) {
        const p = elapsed / 900;
        ctx.save();
        ctx.globalAlpha = (1 - p) * 0.5;
        ctx.strokeStyle = "rgba(44,44,42,0.85)";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(cx, cy, 12 + p * Math.max(w, h) * 0.55, 0, Math.PI * 2);
        ctx.stroke();
        ctx.restore();
      }

      // 情绪符号（水墨形态，而非肢体动作）
      if (emotion === "thinking") {
        const tp = (now / 1500) % 1;
        ctx.save();
        ctx.globalAlpha = (1 - tp) * 0.5;
        ctx.strokeStyle = "rgba(44,44,42,0.9)";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(cx, cy - h * 0.44, 5 + tp * 24, 0, Math.PI * 2);
        ctx.stroke();
        ctx.restore();
      } else if (emotion === "happy") {
        const hp = (now / 1600) % 1;
        ctx.save();
        ctx.globalAlpha = (1 - hp) * 0.45;
        ctx.strokeStyle = "rgba(44,44,42,0.9)";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(cx, cy, 7 + hp * 32, 0, Math.PI * 2);
        ctx.stroke();
        ctx.restore();
      } else if (emotion === "focused") {
        const fp = (now / 1800) % 1;
        ctx.save();
        ctx.globalAlpha = (1 - fp) * 0.4;
        ctx.strokeStyle = "rgba(15,110,86,0.9)";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(cx + 16, cy - 16, 5 + fp * 22, 0, Math.PI * 2);
        ctx.stroke();
        ctx.restore();
      }

      // 移动拖尾：墨灵身后散开的墨晕（墨在纸上流淌）
      if (isWalking) {
        const off = walkDirection === "right" ? -1 : 1;
        trail.push({ x: cx + off * 8, y: cy + 8, born: now });
      }
      for (let i = trail.length - 1; i >= 0; i--) {
        const age = now - trail[i].born;
        if (age > 700) {
          trail.splice(i, 1);
          continue;
        }
        const p = age / 700;
        const off = walkDirection === "right" ? -1 : 1;
        const x = trail[i].x + off * p * 28;
        ctx.save();
        ctx.globalAlpha = (1 - p) * 0.32;
        ctx.fillStyle = "rgba(44,44,42,0.9)";
        ctx.beginPath();
        ctx.arc(x, trail[i].y, 7 * (1 - p * 0.5), 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      }

      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [emotion, isWalking, walkDirection, baseW, baseH]);

  return (
    <>
      <InkFilterDefs />
      <style>{`
        @keyframes mok-float { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-7px); } }
        @keyframes mok-breathe { 0%,100% { transform: scale(1); } 50% { transform: scale(1.05); } }
        @keyframes mok-appear { from { opacity: 0; } to { opacity: 1; } }
        .mok-float { animation: mok-float 4s ease-in-out infinite; }
        .mok-breathe { animation: mok-breathe 3.2s ease-in-out infinite; transform-origin: center; }
        .mok-appear { animation: mok-appear 0.9s ease-out; }
      `}</style>
      <div
        className="relative cursor-pointer select-none mok-float"
        style={{ width: baseW, height: baseH }}
        onClick={onClick}
        onDoubleClick={onDoubleClick}
        role="button"
        tabIndex={0}
      >
        <canvas
          ref={canvasRef}
          className="pointer-events-none absolute inset-0"
          style={{ width: baseW, height: baseH }}
        />
        <div className="mok-breathe absolute inset-0" style={{ transformOrigin: "center" }}>
          <img
            src={`${import.meta.env.BASE_URL}pets/mok-dling.png`}
            alt="墨灵"
            draggable={false}
            className="mok-appear absolute inset-0"
            style={{
              width: baseW,
              height: baseH,
              objectFit: "contain",
              filter: "url(#mok-ink)",
              transform: `scaleX(${flipX ? -1 : 1}) scale(${body.scale}) rotate(${body.rotate}deg)`,
              transition: "transform 0.6s ease-in-out",
              willChange: "transform",
            }}
          />
        </div>
      </div>
    </>
  );
}
