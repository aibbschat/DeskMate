/**
 * XiaojuMascot — 小桔桌宠（序列帧动画 + 情绪粒子；PNG 表情帧做兜底）。
 *
 * 两条渲染路径（由 manifest frameCount 决定，有素材走序列帧、没有回退 PNG）：
 *   A. 序列帧（当前九段全部就位，实际走的就是这条）
 *      MoodParticles（粒子层）+ XjFramePlayer（720² 透明 WebP，42ms/帧）
 *      身体动作全部由帧承担，不叠 CSS transform。
 *   B. PNG 兜底（某 clip 缺素材时）
 *      xj-wrapper（呼吸）→ xj-gesture（姿态动画）→ xj-facing（翻转）→ xj-img（表情帧）
 *      外加 TearStream 眼泪 SVG（坐标按 1024² 老构图，只适配这条路径）。
 *
 * 情绪粒子（两条路径共用 MoodParticles）：
 *   happy     → 星星/爱心从角色周围飘出
 *   thinking  → 头顶"..."思考气泡
 *   concerned → 头顶爱心（安静陪伴）
 *   focused   → 周围小火花闪烁
 *
 * 🔴 桌宠一律无声（萧哥 2026-11-05 定）：帧序列无音轨，不得添加 <audio>/音效。
 *
 * 接口与 CicadaMascot 兼容。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { EmotionName } from "./EmotionState";
import type { ExpressionId } from "./mascotEmoteMap";
import { resolveClip } from "./petClipMap";
import { XjFramePlayer } from "./XjFramePlayer";
// clip manifest：声明哪些情绪有真序列帧素材（frameCount>0），有就走序列帧，没有回退 PNG
import petManifest from "../../../public/pet-clips/manifest.json";
import mascotImg from "../../assets/mascot-xiaoju.webp";
// B 方案表情帧：同一构图只换脸（1024² 透明 WebP，assets/mascot-frames/；
// 2026-11-21 优化建议 P0-6：由同名 PNG 转制，quality 85，9 张合计 6.16MB → 0.51MB，
// 原 PNG 暂留作回滚备份，确认无误后可删）
import laughImg from "../../assets/mascot-frames/laugh.webp";
import cheerImg from "../../assets/mascot-frames/cheer.webp";
import cryImg from "../../assets/mascot-frames/cry.webp";
import angryImg from "../../assets/mascot-frames/angry.webp";
import shockImg from "../../assets/mascot-frames/shock.webp";
import sleepyImg from "../../assets/mascot-frames/sleepy.webp";
import shyImg from "../../assets/mascot-frames/shy.webp";
import neutralImg from "../../assets/mascot-frames/neutral.webp";
// 真动态层（旧 diff 贴纸方案已废弃，改用序列帧）
// import eyesClosedImg from "../../assets/mascot-layers/eyes-closed.png";
// import mouthOpenImg from "../../assets/mascot-layers/mouth-open.png";

/** 表情帧 id → 打包内图片资源。 */
const FRAME_MAP: Record<ExpressionId, string> = {
  smile: mascotImg,
  laugh: laughImg,
  cheer: cheerImg,
  cry: cryImg,
  angry: angryImg,
  shock: shockImg,
  sleepy: sleepyImg,
  shy: shyImg,
  neutral: neutralImg,
};

/** 无 expression 时的兜底：由 EmotionName 推断表情帧（保持旧调用方兼容）。 */
function emotionFallbackFrame(emotion: EmotionName): ExpressionId {
  switch (emotion) {
    case "thinking":
      return "neutral";
    case "happy":
      return "laugh";
    // 「concerned」是 App 的 UI/活动状态（建议中 / 忙碌 / 工具出错），不是小桔的情绪；
    // 情绪脸只由她的激素推导，故 fallback 也不映射到 cry，避免 UI 状态劫持情绪脸。
    case "concerned":
      return "neutral";
    case "focused":
      return "neutral";
    default:
      return "smile";
  }
}

interface XiaojuMascotProps {
  emotion?: EmotionName;
  /** B 方案：显式表情帧（由 P2 情绪底色 / 运行状态解析后传入）；缺省时按 emotion 兜底。 */
  expression?: ExpressionId;
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

/** 关注（你忙碌时）：轻柔上浮的小爱心，安静陪伴、不打扰 */
function CareHearts({ size }: { size: number }) {
  const items = [
    { left: 0.40, top: 0.30, delay: 0, dur: 2.6 },
    { left: 0.55, top: 0.22, delay: 0.7, dur: 3.0 },
    { left: 0.48, top: 0.38, delay: 1.3, dur: 2.8 },
  ];
  return (
    <>
      {items.map((p, i) => (
        <span
          key={i}
          className="xj-particle xj-care-heart"
          style={{
            left: `${size * p.left}px`,
            top: `${size * p.top}px`,
            animationDelay: `${p.delay}s`,
            animationDuration: `${p.dur}s`,
          }}
        >
          ♡
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

/** 哭：真眼泪——两滴泪从眼角沿 SVG 路径流下、滑过脸颊、落地蒸发 */
function TearStream() {
  return (
    <svg
      className="xj-tears"
      viewBox="0 0 1024 1024"
      width="100%"
      height="100%"
      style={{ position: "absolute", inset: 0, pointerEvents: "none", zIndex: 6 }}
    >
      {/* 左眼路径：外眼角 (335,470) → 沿脸颊 (348,560) → (355,620) */}
      <path id="xj-tear-l" d="M 335 470 C 345 520, 352 570, 355 625" fill="none" />
      {/* 右眼路径：外眼角 (690,470) → 沿脸颊 (678,560) → (672,625) */}
      <path id="xj-tear-r" d="M 690 470 C 680 520, 674 570, 672 625" fill="none" />
      {[0, 1].map((i) => (
        <g key={i}>
          <circle r="7" fill="#9fd8ff" opacity="0.9">
            <animateMotion
              dur="1.5s"
              begin={`${i * 0.75}s`}
              repeatCount="indefinite"
              path={`M ${i === 0 ? "335 470 C 345 520, 352 570, 355 625" : "690 470 C 680 520, 674 570, 672 625"}`}
            />
            <animate
              attributeName="opacity"
              values="0;0.9;0.9;0"
              keyTimes="0;0.15;0.85;1"
              dur="1.5s"
              begin={`${i * 0.75}s`}
              repeatCount="indefinite"
            />
            <animate
              attributeName="r"
              values="5;7;6;2"
              keyTimes="0;0.2;0.8;1"
              dur="1.5s"
              begin={`${i * 0.75}s`}
              repeatCount="indefinite"
            />
          </circle>
        </g>
      ))}
    </svg>
  );
}

/* ------------------------------------------------------------------ */
/*  情绪粒子层（两条渲染路径共用）                                        */
/* ------------------------------------------------------------------ */

/**
 * MoodParticles — 星星 / 思考气泡 / 爱心 / 火花。
 *
 * 为什么单独抽出来（2026-11-05）：九段序列帧全部就位后 useFramePlayer 恒为真，
 * 而旧实现里「序列帧分支」在粒子层之前就 return 了 —— 素材换上了，但原本的
 * 情绪细节（开心的星星、思考的「…」气泡、专注的火花、陪伴的爱心）被静默吞掉，
 * 且没有报错、没人会发现。现在粒子层包在两条路径外面，序列帧只负责身体动作。
 *
 * 不含眼泪：cry 素材已自带「低头忍泪 → 眼泪滑下 → 抬手抹眼角」的表演；
 * 旧 TearStream 的 SVG 路径按 1024² 老构图锚定眼角坐标（335,470 / 690,470），
 * 叠到 720² 新素材上会错位，故只在 PNG 回退路径里保留。
 */
function MoodParticles({ emotion, size }: { emotion: EmotionName; size: number }) {
  return (
    <>
      {emotion === "happy" && <HappyParticles size={size} />}
      {emotion === "thinking" && <ThinkingBubble size={size} />}
      {emotion === "concerned" && <CareHearts size={size} />}
      {emotion === "focused" && <FocusedSparks size={size} />}
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  主组件                                                              */
/* ------------------------------------------------------------------ */

export function XiaojuMascot({
  emotion = "idle",
  expression,
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

  // 旧 diff 贴纸方案的眨眼/嘴动逻辑已废弃，真动态改由序列帧承担。
  // const eyesClosed ...
  // const mouthOpen ...

  // B 方案换脸：expression 显式优先，缺省退回 emotion 兜底。
  // 默认 smile 仍用打包内高清母版图（mascot-xiaoju.png，透明甜笑）；
  // 表情帧 1024² 透明 PNG 由 Vite 处理为带 hash 的 URL，object-fit: contain 等比缩放。
  const faceSrc = FRAME_MAP[expression ?? emotionFallbackFrame(emotion)];

  // 真序列帧 clip：emotion/expression → clip 名。manifest 里 frameCount>0 才用序列帧，否则回退 PNG。
  // 选择规则见 petClipMap.ts（九条映射由 petClipMap.test.ts 逐条锁死）；这里不再内联判断。
  const clipName = resolveClip(expression, emotion, isWalking);
  const clipMeta = (petManifest.clips as Record<string, { frameCount: number }>)[clipName];
  const useFramePlayer = !!clipMeta && clipMeta.frameCount > 0;

  // 换脸淡入淡出：旧帧留在底层淡出，新帧在上层淡入，320ms 后卸载旧帧。
  // 注意：effect 只依赖 faceSrc（不用 currentSrc），避免 setState 触发的二次
  // 运行把卸载定时器清掉，导致旧帧永远残留在 DOM（实测线上 bug）。
  const [shown, setShown] = useState<{ src: string; prev: string | null }>({
    src: faceSrc,
    prev: null,
  });
  useEffect(() => {
    setShown((s) => (s.src === faceSrc ? s : { src: faceSrc, prev: s.src }));
    const t = window.setTimeout(
      () => setShown((s) => (s.prev ? { ...s, prev: null } : s)),
      320,
    );
    return () => window.clearTimeout(t);
  }, [faceSrc]);

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

  // 真序列帧模式：XjFramePlayer 负责身体动作，粒子层仍叠在外层（见 MoodParticles 注释）。
  // 这里刻意不加 CSS 弹跳：帧序列本身就在动，再叠 transform 会破坏「帧间硬切」的表现。
  if (useFramePlayer) {
    return (
      <div className="relative" style={{ width: size, height: size }}>
        <MoodParticles emotion={emotion} size={size} />
        <XjFramePlayer
          clip={clipName}
          frameCount={clipMeta.frameCount}
          size={size}
          facing={walkDirection}
          onClick={handleClick}
          onDoubleClick={onDoubleClick}
        />
      </div>
    );
  }

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
        /* 关注：轻轻微倾、柔和陪伴（不再低头垂肩显得丧气） */
        @keyframes xj-sad {
          0%, 100% { transform: translateY(2px) scale(0.99, 1.01) rotate(-1.5deg); }
          50% { transform: translateY(3px) scale(0.98, 1.02) rotate(-2.5deg); }
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
        /* B 方案换脸：双层堆叠淡入淡出（旧帧淡出 + 新帧淡入） */
        .xj-face {
          position: relative;
          width: 100%;
          height: 100%;
        }
        .xj-face .xj-img {
          position: absolute;
          inset: 0;
        }
        @keyframes xj-face-in {
          from { opacity: 0; transform: scale(0.96); }
          to { opacity: 1; transform: scale(1); }
        }
        @keyframes xj-face-out {
          from { opacity: 1; }
          to { opacity: 0; }
        }
        .xj-face-in {
          animation: xj-face-in 0.28s ease-out both;
        }
        .xj-face-out {
          animation: xj-face-out 0.28s ease-in both;
        }
        /* 真动态贴纸层：绝对定位叠在整图上，按 1024² 画布百分比锚定 */
        .xj-layer {
          position: absolute;
          object-fit: fill;
          pointer-events: none;
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
        .xj-spark {
          font-size: 14px;
          color: #ffb84d;
          text-shadow: 0 0 8px rgba(255, 184, 77, 0.8);
          animation: xj-spark-flash 0.9s ease-in-out infinite;
        }
        /* 关注：轻柔上浮的小爱心（暖色、缓慢、幅度小，表达安静陪伴） */
        @keyframes xj-care-float {
          0% { opacity: 0; transform: translate(0, 4px) scale(0.5); }
          25% { opacity: 0.9; }
          100% { opacity: 0; transform: translate(-3px, -22px) scale(1.1); }
        }
        .xj-care-heart {
          font-size: 13px;
          color: #ff8fa3;
          text-shadow: 0 0 8px rgba(255, 143, 163, 0.5);
          animation: xj-care-float 2.8s ease-out infinite;
        }
      `}</style>

      {/* 情绪粒子层（PNG 路径与序列帧路径共用同一份，避免两套漂移） */}
      <MoodParticles emotion={emotion} size={size} />
      {/* 真眼泪：仅 PNG 路径需要——序列帧素材自带流泪表演（见 MoodParticles 注释） */}
      {expression === "cry" && <TearStream />}

      {/* 角色层 */}
      <div
        className={`xj-wrapper ${bouncing ? "bouncing" : ""}`}
        style={{ width: size, height: size }}
      >
        <div className="xj-gesture" style={{ width: size, height: size }}>
          <div className="xj-facing">
            <div className="xj-face">
              {shown.prev && (
                <img
                  key={`out-${shown.prev}`}
                  src={shown.prev}
                  alt=""
                  aria-hidden="true"
                  className="xj-img xj-face-out"
                  draggable={false}
                />
              )}
              <img
                key={`in-${shown.src}`}
                src={shown.src}
                alt="小桔"
                className="xj-img xj-face-in"
                draggable={false}
              />
              {/* 真动态贴纸层暂禁用：diff 抠图在 3D 黏土脸上会露馅（补丁轮廓/横切线），
                  等真分层素材或 3D 模型方案落地后再启用。 */}
              {/*
              <img
                src={eyesClosedImg}
                ...
              />
              <img
                src={mouthOpenImg}
                ...
              />
              */}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
