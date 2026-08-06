/**
 * Mascot — 墨灵桌宠（程序化水墨生物 V3 - 活泼增强版）。
 *
 * 完全用 Canvas 程序化绘制，超级活泼有趣的动画系统：
 *   - 随机行为状态机：张望、打哈欠、伸懒腰、做鬼脸、发呆、玩耍
 *   - 丰富交互：点击跳动、双击庆祝、鼠标悬停好奇
 *   - 有趣粒子：星星✨、问号❓、Z泡泡💤、爱心❤️、彩带🎊
 *   - 情绪深度联动：心情影响行为选择和动画幅度
 *   - 成长阶段外观：从圆润小墨团到优雅大师形态
 *
 * 眼睛跟随鼠标，随机眨眼，超级可爱的表情系统。
 */

import { useEffect, useRef, useCallback } from "react";
import type { EmotionName } from "./EmotionState";

interface MascotProps {
  emotion: EmotionName;
  walkDirection?: "left" | "right";
  isWalking?: boolean;
  scale?: number;
  onClick?: () => void;
  onDoubleClick?: () => void;
  /** 心情 0-100，驱动浮动幅度和粒子活力。默认 80 */
  mood?: number;
  /** 活力 0-100，驱动呼吸节奏和墨渗浓淡。默认 100 */
  vitality?: number;
  /** 等级 1-12，驱动阶段光环。默认 1 */
  level?: number;
}

// ─── 常量 ───

const INK = "rgba(44,44,42,";

// ─── 随机行为系统 ───

type IdleBehavior =
  | "idle"           // 普通待机（呼吸+浮动）
  | "look_around"    // 四处张望
  | "yawn"           // 打哈欠
  | "stretch"        // 伸懒腰
  | "play"           // 自己玩耍（小墨团抛接）
  | "sleepy"         // 打瞌睡
  | "curious"        // 好奇（眼睛放大、身体前倾）
  | "happy_dance"    // 开心扭动
  | "look_at_mouse"; // 盯着鼠标看

interface BehaviorState {
  current: IdleBehavior;
  duration: number;      // 当前行为剩余帧数
  nextChange: number;    // 距离下次行为切换的帧数
  intensity: number;     // 行为强度 0-1
  phase: number;         // 行为内部动画阶段
}

interface StageConfig {
  name: string;
  min: number;
  max: number;
  bodyScale: number;
  eyeSize: number;
  aura: number;
  pearls: number;
  ribbons: number;
  /** 身体形态：圆润/微椭/人形/修长 */
  bodyShape: "round" | "oval" | "humanoid" | "slim";
  /** 触手数量 */
  tentacles: number;
  /** 触手长度倍率 */
  tentacleLength: number;
  /** 衣着复杂度 0-3 */
  clothing: number;
  /** 光晕层次 */
  glowLayers: number;
  /** 墨迹纹理密度 */
  inkTexture: number;
  /** 行为动画幅度倍率 */
  animationAmplitude: number;
}

const STAGES: StageConfig[] = [
  {
    name: "墨童", min: 1, max: 3,
    bodyScale: 0.75, eyeSize: 0.85, aura: 0, pearls: 0, ribbons: 0,
    bodyShape: "round", tentacles: 0, tentacleLength: 0,
    clothing: 0, glowLayers: 0, inkTexture: 0.3, animationAmplitude: 0.8,
  },
  {
    name: "墨生", min: 4, max: 6,
    bodyScale: 0.88, eyeSize: 0.93, aura: 1, pearls: 0, ribbons: 0,
    bodyShape: "oval", tentacles: 2, tentacleLength: 0.7,
    clothing: 1, glowLayers: 1, inkTexture: 0.5, animationAmplitude: 0.9,
  },
  {
    name: "墨客", min: 7, max: 9,
    bodyScale: 1.0, eyeSize: 1.0, aura: 1, pearls: 3, ribbons: 0,
    bodyShape: "humanoid", tentacles: 2, tentacleLength: 1.0,
    clothing: 2, glowLayers: 2, inkTexture: 0.7, animationAmplitude: 1.0,
  },
  {
    name: "墨仙", min: 10, max: 12,
    bodyScale: 1.08, eyeSize: 1.08, aura: 2, pearls: 3, ribbons: 2,
    bodyShape: "slim", tentacles: 4, tentacleLength: 1.3,
    clothing: 3, glowLayers: 3, inkTexture: 1.0, animationAmplitude: 1.1,
  },
];

function getStage(lv: number) {
  return STAGES.find((s) => lv >= s.min && lv <= s.max) ?? STAGES[0];
}

// ─── 工具 ───

function lerp(a: number, b: number, t: number) {
  return a + (b - a) * Math.min(1, Math.max(0, t));
}
function noise(x: number) {
  return Math.sin(x * 1.7) * 0.5 + Math.sin(x * 3.1) * 0.3 + Math.sin(x * 5.3) * 0.2;
}

// ─── 粒子 ───

interface Particle {
  type:
    | "drop" | "smoke" | "splash" | "pulse" | "glow"  // 原有类型
    | "click" | "trail" | "care"                       // 交互粒子
    | "star" | "question" | "zzz" | "heart" | "ribbon" // 新趣味粒子
    | "sparkle" | "confetti";                          // 庆祝粒子
  x: number; y: number;
  vx: number; vy: number;
  life: number; maxLife: number;
  size: number; alpha: number;
  rotation: number; rotSpeed: number;
  born: number;
  color?: string;  // 可选颜色（用于彩色粒子）
}

// ─── 组件 ───

export function Mascot({
  emotion,
  walkDirection = "right",
  isWalking = false,
  scale = 1.0,
  onClick,
  onDoubleClick,
  mood = 80,
  vitality = 100,
  level = 1,
}: MascotProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rafRef = useRef(0);
  const mouseRef = useRef({ x: 0, y: 0 });
  const clickRef = useRef(0);
  const particlesRef = useRef<Particle[]>([]);
  const spawnTimerRef = useRef(0);

  // 生长数值用 ref，避免变化时重建整个 Canvas（粒子连续性）
  const growthRef = useRef({ mood, vitality, level });
  growthRef.current = { mood, vitality, level };

  // 身体动画状态
  const bodyRef = useRef({
    scaleY: 1, scaleX: 1, tilt: 0, headY: 0, eyeOpen: 1, blinkTimer: 0,
    floatY: 0, breathPhase: 0, floatPhase: 0,
    // 新增：行为动画状态
    bounceY: 0, bounceVY: 0,  // 弹跳
    squishX: 1, squishY: 1,   // 挤压拉伸
    lookTargetX: 0, lookTargetY: 0,  // 视线目标
    armWave: 0,               // 手臂挥舞
  });

  // 随机行为状态机
  const behaviorRef = useRef<BehaviorState>({
    current: "idle",
    duration: 300,
    nextChange: 200 + Math.random() * 400,
    intensity: 0.5,
    phase: 0,
  });

  // 跟踪鼠标（用于眼睛跟随）
  useEffect(() => {
    const onMove = (e: MouseEvent) => { mouseRef.current = { x: e.clientX, y: e.clientY }; };
    window.addEventListener("mousemove", onMove);
    return () => window.removeEventListener("mousemove", onMove);
  }, []);

  // 主渲染循环
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx0 = canvas.getContext("2d");
    if (!ctx0) return;
    const ctx: CanvasRenderingContext2D = ctx0;

    const dpr = window.devicePixelRatio || 1;
    const W = Math.round(200 * scale);
    const H = Math.round(220 * scale);
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const particles = particlesRef.current;
    const body = bodyRef.current;
    let spawnTimer = spawnTimerRef.current;

    const cx = W / 2;
    const cy = H / 2;
    let entryElapsed = 0;

    // 简单噪声
    const n = noise;

    // ── 墨迹绘制工具 ──
    function inkColor(alpha: number) { return `${INK}${alpha})`; }

    function inkBlob(x: number, y: number, rx: number, ry: number, alpha: number, t: number, detail = 10) {
      ctx.beginPath();
      for (let i = 0; i <= detail; i++) {
        const angle = (i / detail) * Math.PI * 2;
        const rNoise = n(angle * 3 + t * 0.001) * 0.15;
        const r = 1 + rNoise;
        const px = x + Math.cos(angle) * rx * r;
        const py = y + Math.sin(angle) * ry * r;
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      }
      ctx.closePath();
      ctx.fillStyle = inkColor(alpha);
      ctx.fill();
    }

    function inkHalo(x: number, y: number, r: number, alpha: number) {
      const g = ctx.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, inkColor(alpha));
      g.addColorStop(0.7, inkColor(alpha * 0.3));
      g.addColorStop(1, inkColor(0));
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = g;
      ctx.fill();
    }

    // ── 眼睛 ──
    function drawEye(ex: number, ey: number, size: number, blink: number, lookX: number, lookY: number) {
      ctx.save();
      ctx.translate(ex, ey);
      ctx.scale(1, blink);

      // 眼眶
      ctx.beginPath();
      ctx.ellipse(0, 0, size, size * 0.85, 0, 0, Math.PI * 2);
      ctx.fillStyle = "#f8f6f1";
      ctx.fill();
      ctx.strokeStyle = inkColor(0.55);
      ctx.lineWidth = 1.2 * scale;
      ctx.stroke();

      // 瞳孔
      const ps = size * 0.42;
      ctx.beginPath();
      ctx.arc(lookX * 0.6, lookY * 0.6, ps, 0, Math.PI * 2);
      ctx.fillStyle = inkColor(0.92);
      ctx.fill();

      // 高光
      ctx.beginPath();
      ctx.arc(lookX * 0.6 - ps * 0.3, lookY * 0.6 - ps * 0.3, ps * 0.22, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(255,255,255,0.75)";
      ctx.fill();

      ctx.restore();
    }

    // ── 粒子生成 ──
    function spawn(type: Particle["type"], ox = 0, oy = 0) {
      const now = performance.now();
      const p: Particle = {
        type, x: cx + ox, y: cy + oy, vx: 0, vy: 0,
        life: 1, maxLife: 1, size: 4, alpha: 0.4,
        rotation: 0, rotSpeed: 0, born: now,
      };
      switch (type) {
        case "drop":
          p.x += (Math.random() - 0.5) * 25 * scale;
          p.y -= 52 * scale;
          p.vy = (0.12 + Math.random() * 0.08) * scale;
          p.size = (2 + Math.random() * 2.5) * scale;
          p.alpha = 0.14 + Math.random() * 0.08;
          p.maxLife = 200 + Math.random() * 150;
          break;
        case "smoke": {
          p.x += (Math.random() - 0.5) * 14 * scale;
          p.y -= 48 * scale;
          p.vy = (-0.22 - Math.random() * 0.12) * scale;
          p.vx = (Math.random() - 0.5) * 0.12 * scale;
          p.rotSpeed = (Math.random() - 0.5) * 0.012;
          p.size = (3 + Math.random() * 3.5) * scale;
          p.alpha = 0.1 + Math.random() * 0.07;
          p.maxLife = 250 + Math.random() * 200;
          break;
        }
        case "splash": {
          const a = Math.random() * Math.PI * 2;
          const sp = (0.4 + Math.random() * 0.7) * scale;
          p.vx = Math.cos(a) * sp;
          p.vy = Math.sin(a) * sp;
          p.size = (2 + Math.random() * 2) * scale;
          p.alpha = 0.22 + Math.random() * 0.12;
          p.maxLife = 100 + Math.random() * 80;
          break;
        }
        case "pulse": {
          const pa = Math.random() * Math.PI * 2;
          const pr = (28 + Math.random() * 14) * scale;
          p.x += Math.cos(pa) * pr;
          p.y += Math.sin(pa) * pr * 0.7;
          p.size = (4 + Math.random() * 4) * scale;
          p.alpha = 0.12;
          p.maxLife = 100 + Math.random() * 60;
          break;
        }
        case "glow":
          p.x += (Math.random() - 0.5) * 18 * scale;
          p.y -= (48 + Math.random() * 14) * scale;
          p.vy = -0.06 * scale;
          p.size = (3 + Math.random() * 3) * scale;
          p.alpha = 0.18;
          p.maxLife = 180 + Math.random() * 120;
          break;
        case "click": {
          const ca = Math.random() * Math.PI * 2;
          const cs = (0.8 + Math.random() * 1.0) * scale;
          p.vx = Math.cos(ca) * cs;
          p.vy = Math.sin(ca) * cs;
          p.size = (2 + Math.random() * 2) * scale;
          p.alpha = 0.32;
          p.maxLife = 55 + Math.random() * 35;
          break;
        }
        case "trail":
          p.x += (walkDirection === "right" ? -1 : 1) * 6 * scale;
          p.y += 8 * scale;
          p.vx = (walkDirection === "right" ? -1 : 1) * 0.2 * scale;
          p.vy = 0.05 * scale;
          p.size = (3 + Math.random() * 3) * scale;
          p.alpha = 0.2;
          p.maxLife = 120 + Math.random() * 80;
          break;
        case "care": {
          // 照顾行为粒子：温暖的上升光点
          const careAngle = Math.random() * Math.PI * 2;
          const careDist = 15 + Math.random() * 25;
          p.x += Math.cos(careAngle) * careDist * scale;
          p.y += Math.sin(careAngle) * careDist * scale * 0.5 - 20 * scale;
          p.vy = (-0.3 - Math.random() * 0.2) * scale;
          p.vx = (Math.random() - 0.5) * 0.15 * scale;
          p.size = (3 + Math.random() * 3) * scale;
          p.alpha = 0.35 + Math.random() * 0.15;
          p.maxLife = 150 + Math.random() * 100;
          p.rotSpeed = (Math.random() - 0.5) * 0.02;
          break;
        }
        case "star": {
          // 星星粒子：开心时飞溅
          const starAngle = Math.random() * Math.PI * 2;
          const starSpeed = (0.5 + Math.random() * 1.0) * scale;
          p.vx = Math.cos(starAngle) * starSpeed;
          p.vy = Math.sin(starAngle) * starSpeed - 0.3 * scale;
          p.size = (4 + Math.random() * 4) * scale;
          p.alpha = 0.5 + Math.random() * 0.3;
          p.maxLife = 80 + Math.random() * 60;
          p.rotSpeed = (Math.random() - 0.5) * 0.05;
          p.color = `hsl(${40 + Math.random() * 20}, 90%, ${60 + Math.random() * 20}%)`;
          break;
        }
        case "question": {
          // 问号泡泡：好奇时出现
          p.x += (Math.random() - 0.5) * 30 * scale;
          p.y -= (35 + Math.random() * 15) * scale;
          p.vy = -0.15 * scale;
          p.vx = (Math.random() - 0.5) * 0.08 * scale;
          p.size = (8 + Math.random() * 6) * scale;
          p.alpha = 0.4;
          p.maxLife = 120 + Math.random() * 80;
          p.rotSpeed = (Math.random() - 0.5) * 0.02;
          break;
        }
        case "zzz": {
          // Z 泡泡：打瞌睡/睡觉
          p.x += (Math.random() - 0.5) * 20 * scale;
          p.y -= (30 + Math.random() * 10) * scale;
          p.vy = -0.1 * scale;
          p.vx = 0.05 * scale;
          p.size = (6 + Math.random() * 5) * scale;
          p.alpha = 0.3;
          p.maxLife = 150 + Math.random() * 100;
          break;
        }
        case "heart": {
          // 爱心粒子：被照顾/开心时
          const heartAngle = Math.random() * Math.PI * 2;
          const heartSpeed = (0.3 + Math.random() * 0.5) * scale;
          p.vx = Math.cos(heartAngle) * heartSpeed;
          p.vy = Math.sin(heartAngle) * heartSpeed - 0.2 * scale;
          p.size = (5 + Math.random() * 4) * scale;
          p.alpha = 0.4 + Math.random() * 0.2;
          p.maxLife = 100 + Math.random() * 80;
          p.color = `hsl(${340 + Math.random() * 30}, 80%, ${60 + Math.random() * 20}%)`;
          break;
        }
        case "ribbon": {
          // 彩带：庆祝/升级时
          const ribbonAngle = Math.random() * Math.PI * 2;
          const ribbonSpeed = (0.6 + Math.random() * 0.8) * scale;
          p.vx = Math.cos(ribbonAngle) * ribbonSpeed;
          p.vy = Math.sin(ribbonAngle) * ribbonSpeed - 0.4 * scale;
          p.size = (3 + Math.random() * 3) * scale;
          p.alpha = 0.5;
          p.maxLife = 100 + Math.random() * 80;
          p.rotSpeed = (Math.random() - 0.5) * 0.08;
          p.color = `hsl(${Math.random() * 360}, 85%, 65%)`;
          break;
        }
        case "sparkle": {
          // 闪烁光点：玩耍/惊喜时
          const sparkleAngle = Math.random() * Math.PI * 2;
          const sparkleDist = 20 + Math.random() * 30;
          p.x += Math.cos(sparkleAngle) * sparkleDist * scale;
          p.y += Math.sin(sparkleAngle) * sparkleDist * scale;
          p.vy = (-0.1 - Math.random() * 0.1) * scale;
          p.size = (2 + Math.random() * 3) * scale;
          p.alpha = 0.6 + Math.random() * 0.3;
          p.maxLife = 50 + Math.random() * 40;
          p.color = `hsl(${50 + Math.random() * 30}, 100%, 80%)`;
          break;
        }
        case "confetti": {
          // 五彩纸屑：大庆祝
          const confettiAngle = Math.random() * Math.PI * 2;
          const confettiSpeed = (0.8 + Math.random() * 1.2) * scale;
          p.vx = Math.cos(confettiAngle) * confettiSpeed;
          p.vy = Math.sin(confettiAngle) * confettiSpeed - 0.5 * scale;
          p.size = (4 + Math.random() * 4) * scale;
          p.alpha = 0.6;
          p.maxLife = 120 + Math.random() * 80;
          p.rotSpeed = (Math.random() - 0.5) * 0.1;
          p.color = `hsl(${Math.random() * 360}, 90%, 60%)`;
          break;
        }
      }
      p.life = p.maxLife;
      particles.push(p);
    }

    // ── 绘制粒子 ──
    function drawParticle(p: Particle, lr: number, moodAlpha = 1.0) {
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.rotation);
      ctx.globalAlpha = p.alpha * lr * moodAlpha;

      if (p.type === "smoke") {
        const s = p.size * (1 + (1 - lr) * 2.5);
        ctx.beginPath();
        for (let j = 0; j <= 8; j++) {
          const a = (j / 8) * Math.PI * 2;
          const r = s * (0.55 + Math.sin(a * 3 + lr * 8) * 0.45);
          const px = Math.cos(a) * r;
          const py = Math.sin(a) * r * 0.55;
          if (j === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
        }
        ctx.closePath();
        ctx.fillStyle = inkColor(0.75);
        ctx.fill();
      } else if (p.type === "pulse") {
        ctx.beginPath();
        ctx.arc(0, 0, p.size * (1.5 - lr * 0.5), 0, Math.PI * 2);
        ctx.strokeStyle = inkColor(lr * 0.22);
        ctx.lineWidth = 1.2 * scale;
        ctx.stroke();
      } else if (p.type === "glow") {
        inkHalo(0, 0, p.size * (1 + (1 - lr)), lr * 0.28);
      } else if (p.type === "care") {
        // 照顾粒子：温暖的橙黄色光点
        const careAlpha = lr * 0.6;
        const careSize = p.size * (1 + (1 - lr) * 0.8);
        // 外层光晕
        const gradient = ctx.createRadialGradient(0, 0, 0, 0, 0, careSize * 2.5);
        gradient.addColorStop(0, `rgba(255, 180, 80, ${careAlpha * 0.4})`);
        gradient.addColorStop(0.5, `rgba(255, 160, 60, ${careAlpha * 0.15})`);
        gradient.addColorStop(1, `rgba(255, 140, 40, 0)`);
        ctx.fillStyle = gradient;
        ctx.beginPath();
        ctx.arc(0, 0, careSize * 2.5, 0, Math.PI * 2);
        ctx.fill();
        // 内核
        ctx.fillStyle = `rgba(255, 200, 100, ${careAlpha * 0.8})`;
        ctx.beginPath();
        ctx.arc(0, 0, careSize * 0.5, 0, Math.PI * 2);
        ctx.fill();
      } else if (p.type === "star") {
        // 星星粒子：五角星形状
        const starAlpha = lr * 0.7;
        const starSize = p.size * (1 + (1 - lr) * 0.5);
        ctx.fillStyle = p.color || `rgba(255, 200, 50, ${starAlpha})`;
        ctx.globalAlpha = starAlpha;
        ctx.beginPath();
        for (let i = 0; i < 5; i++) {
          const angle = (i * Math.PI * 2) / 5 - Math.PI / 2;
          const outerX = Math.cos(angle) * starSize;
          const outerY = Math.sin(angle) * starSize;
          const innerAngle = angle + Math.PI / 5;
          const innerX = Math.cos(innerAngle) * starSize * 0.4;
          const innerY = Math.sin(innerAngle) * starSize * 0.4;
          if (i === 0) ctx.moveTo(outerX, outerY);
          else ctx.lineTo(outerX, outerY);
          ctx.lineTo(innerX, innerY);
        }
        ctx.closePath();
        ctx.fill();
      } else if (p.type === "question") {
        // 问号泡泡
        const qAlpha = lr * 0.5;
        const qSize = p.size * (1 + (1 - lr) * 0.3);
        // 泡泡背景
        ctx.fillStyle = `rgba(150, 200, 255, ${qAlpha * 0.2})`;
        ctx.beginPath();
        ctx.arc(0, 0, qSize, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = `rgba(100, 150, 200, ${qAlpha * 0.4})`;
        ctx.lineWidth = 1.5 * scale;
        ctx.stroke();
        // 问号文字
        ctx.fillStyle = `rgba(80, 120, 180, ${qAlpha * 0.8})`;
        ctx.font = `bold ${qSize * 1.2}px sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText("?", 0, 0);
      } else if (p.type === "zzz") {
        // Z 泡泡：睡觉
        const zAlpha = lr * 0.4;
        const zSize = p.size * (1 + (1 - lr) * 0.4);
        ctx.fillStyle = `rgba(150, 180, 220, ${zAlpha})`;
        ctx.font = `bold ${zSize}px sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        // 渐变大小的 Z
        const zCount = 3;
        for (let i = 0; i < zCount; i++) {
          const zOff = i * zSize * 0.5;
          const zScale = 1 - i * 0.2;
          ctx.save();
          ctx.translate(zOff, -zOff);
          ctx.scale(zScale, zScale);
          ctx.globalAlpha = zAlpha * (1 - i * 0.3);
          ctx.fillText("Z", 0, 0);
          ctx.restore();
        }
      } else if (p.type === "heart") {
        // 爱心粒子
        const heartAlpha = lr * 0.6;
        const heartSize = p.size * (1 + (1 - lr) * 0.5);
        ctx.fillStyle = p.color || `rgba(255, 100, 120, ${heartAlpha})`;
        ctx.globalAlpha = heartAlpha;
        // 绘制爱心路径
        ctx.beginPath();
        ctx.moveTo(0, heartSize * 0.3);
        ctx.bezierCurveTo(-heartSize, -heartSize * 0.3, -heartSize * 0.5, -heartSize, 0, -heartSize * 0.4);
        ctx.bezierCurveTo(heartSize * 0.5, -heartSize, heartSize, -heartSize * 0.3, 0, heartSize * 0.3);
        ctx.fill();
      } else if (p.type === "ribbon" || p.type === "confetti") {
        // 彩带/纸屑：彩色小条
        const rcAlpha = lr * 0.6;
        const rcSize = p.size * (1 + (1 - lr) * 0.3);
        ctx.fillStyle = p.color || `hsl(${Math.random() * 360}, 80%, 60%)`;
        ctx.globalAlpha = rcAlpha;
        ctx.fillRect(-rcSize * 0.5, -rcSize * 0.15, rcSize, rcSize * 0.3);
      } else if (p.type === "sparkle") {
        // 闪烁光点
        const spAlpha = lr * 0.7;
        const spSize = p.size * (1 + Math.sin(p.born * 0.01) * 0.3);
        const gradient = ctx.createRadialGradient(0, 0, 0, 0, 0, spSize * 2);
        gradient.addColorStop(0, p.color || `rgba(255, 255, 200, ${spAlpha})`);
        gradient.addColorStop(0.5, `rgba(255, 255, 150, ${spAlpha * 0.3})`);
        gradient.addColorStop(1, `rgba(255, 255, 100, 0)`);
        ctx.fillStyle = gradient;
        ctx.beginPath();
        ctx.arc(0, 0, spSize * 2, 0, Math.PI * 2);
        ctx.fill();
      } else {
        const s = p.type === "drop" || p.type === "trail" ? p.size * (1 + (1 - lr) * 1.2) : p.size * lr;
        const edges = 6;
        ctx.beginPath();
        for (let j = 0; j <= edges; j++) {
          const a = (j / edges) * Math.PI * 2;
          const r = s * (0.7 + Math.abs(n(a * 2 + p.born * 0.01)) * 0.6);
          if (j === 0) ctx.moveTo(Math.cos(a) * r, Math.sin(a) * r);
          else ctx.lineTo(Math.cos(a) * r, Math.sin(a) * r);
        }
        ctx.closePath();
        ctx.fillStyle = inkColor(0.78);
        ctx.fill();
      }
      ctx.restore();
    }

    // ── 主循环 ──
    const draw = (now: number) => {
      entryElapsed += 16;
      const t = now;
      ctx.clearRect(0, 0, W, H);

      const { mood: curMood, vitality: curVit, level: curLv } = growthRef.current;
      const m = curMood / 100;
      const v = curVit / 100;
      const sc = getStage(curLv).bodyScale;
      const emo = emotion;

      // ── 更新身体动画 - 心情深度影响 ──
      // 心情低落：动作整体变慢、幅度减小
      const moodSlowdown = m < 0.3 ? 0.5 : m < 0.5 ? 0.7 : m < 0.8 ? 0.9 : 1.0;
      const stageAnimAmp = getStage(curLv).animationAmplitude;

      const breathSpeed = v > 0.8 ? 0.0008 : v > 0.5 ? 0.001 : v > 0.2 ? 0.0016 : 0.0004;
      const breathAmp = v > 0.8 ? 0.055 : v > 0.5 ? 0.038 : v > 0.2 ? 0.018 : 0.007;
      body.breathPhase += breathSpeed * 16 * moodSlowdown;
      body.scaleY = 1 + Math.sin(body.breathPhase) * breathAmp * stageAnimAmp;
      body.scaleX = 1 - Math.sin(body.breathPhase) * breathAmp * 0.3 * stageAnimAmp;

      // 浮动：心情低落时下沉、速度慢；高涨时更活泼
      const floatSpeed = m > 0.8 ? 0.0007 : m > 0.5 ? 0.00045 : m > 0.2 ? 0.00025 : 0.0001;
      const floatAmp = m > 0.8 ? 8 * scale : m > 0.5 ? 5 * scale : m > 0.2 ? 2.5 * scale : 0.8 * scale;
      const floatDrop = m < 0.2 ? 8 * scale : m < 0.4 ? 4 * scale : 0; // 心情低落时下沉
      body.floatPhase += floatSpeed * 16 * moodSlowdown;
      body.floatY = Math.sin(body.floatPhase) * floatAmp * stageAnimAmp + floatDrop;

      // 情绪 + 心情 + 行为 → 头部微动 + 倾斜
      let targetHeadY = Math.sin(t * 0.0005) * 1 * scale * moodSlowdown;
      let targetTilt = Math.sin(t * 0.0003) * 0.008 * moodSlowdown;

      // 心情低落时头部下垂
      const moodHeadDrop = m < 0.3 ? 3 * scale : m < 0.5 ? 1.5 * scale : 0;

      // 根据行为状态机调整头部动作
      const behavior = behaviorRef.current;
      const currentBehavior = behavior.current;
      if (currentBehavior === "look_around") {
        // 张望：左右转头
        targetTilt = Math.sin(t * 0.003) * 0.06 * behavior.intensity;
        targetHeadY = Math.sin(t * 0.002) * 2 * scale;
      } else if (currentBehavior === "yawn") {
        // 打哈欠：微微抬头
        targetHeadY = -3 * scale * Math.sin(behavior.phase * 2);
        targetTilt = 0;
      } else if (currentBehavior === "stretch") {
        // 伸懒腰：向上伸展
        targetHeadY = -5 * scale * Math.sin(behavior.phase);
        body.scaleY = lerp(body.scaleY, 1.1, 0.05);
      } else if (currentBehavior === "curious") {
        // 好奇：身体前倾、头微侧
        targetHeadY = 2 * scale;
        targetTilt = Math.sin(t * 0.002) * 0.04;
      } else if (currentBehavior === "sleepy") {
        // 打瞌睡：头慢慢低下再抬起
        targetHeadY = 4 * scale * Math.sin(behavior.phase * 0.5);
        targetTilt = Math.sin(t * 0.001) * 0.02;
      } else if (currentBehavior === "happy_dance") {
        // 开心扭动：活泼的头部动作
        targetHeadY = Math.sin(t * 0.005) * 4 * scale;
        targetTilt = Math.sin(t * 0.004) * 0.04;
      } else {
        // 默认：根据情绪
        if (emo === "thinking") {
          targetHeadY = Math.sin(t * 0.002) * 2.5 * scale * moodSlowdown;
          targetTilt = Math.sin(t * 0.001) * 0.018 * moodSlowdown;
        } else if (emo === "happy") {
          targetHeadY = Math.sin(t * 0.004) * 3.5 * scale * stageAnimAmp;
          targetTilt = Math.sin(t * 0.003) * 0.025 * stageAnimAmp;
        } else if (emo === "concerned") {
          targetHeadY = 2.5 * scale + moodHeadDrop;
          targetTilt = Math.sin(t * 0.0008) * 0.012 * moodSlowdown;
        } else if (emo === "focused") {
          targetHeadY = -1.5 * scale;
          targetTilt = 0;
        }
      }

      // 心情低落时整体头部位置下移
      targetHeadY += moodHeadDrop;
      body.headY = lerp(body.headY, targetHeadY, 0.04);
      body.tilt = lerp(body.tilt, targetTilt, 0.04);

      // 眨眼
      body.blinkTimer++;
      const blinkInterval = v < 0.3 ? 80 : 180;
      if (body.blinkTimer > blinkInterval + Math.random() * 200) {
        body.eyeOpen = Math.max(0, body.eyeOpen - 0.15);
        if (body.eyeOpen <= 0.05) {
          body.blinkTimer = 0;
        }
      } else {
        body.eyeOpen = lerp(body.eyeOpen, 1, 0.12);
      }

      // 点击反馈 - 跳动效果 + 有趣粒子组合
      if (clickRef.current > 0) {
        clickRef.current--;

        // 跳动动画（被点击时向上跳然后弹回来）
        if (clickRef.current > 20) {
          body.bounceY = lerp(body.bounceY, -15 * scale, 0.2);
          body.squishX = lerp(body.squishX, 1.15, 0.15);
          body.squishY = lerp(body.squishY, 0.85, 0.15);
        } else {
          body.bounceY = lerp(body.bounceY, 0, 0.15);
          body.squishX = lerp(body.squishX, 1, 0.1);
          body.squishY = lerp(body.squishY, 1, 0.1);
        }

        // 粒子爆发（点击瞬间）
        if (clickRef.current === 28) {
          // 基础墨点
          for (let i = 0; i < 6; i++) spawn("click");
          // 照顾粒子（温暖感）
          const careCount = 3 + Math.floor(Math.random() * 3);
          for (let i = 0; i < careCount; i++) spawn("care");
          // 根据心情添加额外粒子
          if (m > 0.7) {
            // 心情好时：星星飞溅
            for (let i = 0; i < 4; i++) spawn("star");
          } else if (m > 0.4) {
            // 心情一般：闪烁光点
            for (let i = 0; i < 3; i++) spawn("sparkle");
          }
        }
      }

      // ── 随机行为状态机 ──
      behavior.duration--;
      behavior.nextChange--;
      behavior.phase += 0.02;

      // 行为切换逻辑
      if (behavior.nextChange <= 0) {
        // 根据心情选择行为
        const moodBehaviors: IdleBehavior[] = m > 0.7
          ? ["happy_dance", "play", "look_around", "curious"]
          : m > 0.4
            ? ["idle", "look_around", "look_at_mouse", "curious"]
            : ["idle", "sleepy", "yawn"];

        const newBehavior = moodBehaviors[Math.floor(Math.random() * moodBehaviors.length)];

        // 不要连续重复同一个行为
        if (newBehavior !== behavior.current) {
          behavior.current = newBehavior;
          behavior.duration = newBehavior === "yawn" ? 120
            : newBehavior === "stretch" ? 100
            : newBehavior === "happy_dance" ? 150
            : newBehavior === "play" ? 180
            : newBehavior === "sleepy" ? 200
            : 150;
          behavior.intensity = 0.5 + Math.random() * 0.5;
          behavior.phase = 0;
        }

        // 心情低落时行为切换更频繁（更无聊）
        behavior.nextChange = m < 0.3
          ? 100 + Math.random() * 200
          : 250 + Math.random() * 400;
      }

      // ── 粒子系统 - 心情深度影响 ──
      spawnTimer++;
      // 心情低落时粒子生成间隔变长（减少粒子数量）
      const moodRateMultiplier = m < 0.3 ? 1.8 : m < 0.5 ? 1.4 : m < 0.8 ? 1.1 : 1.0;
      const rates: Record<string, number> = {
        idle: 85, thinking: 10, happy: 7, concerned: 13, focused: 11, walking: 22,
      };
      const effectiveRate = Math.floor((rates[emo] ?? 85) * moodRateMultiplier);

      if (spawnTimer >= effectiveRate) {
        spawnTimer = 0;

        // 根据行为状态机生成粒子
        if (behavior.current === "yawn") {
          // 打哈欠：生成 Z 泡泡
          if (Math.random() > 0.7) spawn("zzz");
        } else if (behavior.current === "happy_dance") {
          // 开心扭动：星星飞溅
          spawn("star");
          if (m > 0.8 && Math.random() > 0.5) spawn("star");
        } else if (behavior.current === "play") {
          // 自己玩耍：小墨团抛接（普通粒子+偶尔星星）
          spawn("drop");
          if (Math.random() > 0.8) spawn("sparkle");
        } else if (behavior.current === "curious") {
          // 好奇：问号泡泡
          if (Math.random() > 0.6) spawn("question");
        } else if (behavior.current === "sleepy") {
          // 打瞌睡：很少的粒子（快睡着了）
          if (Math.random() > 0.9) spawn("zzz");
        } else {
          // 根据情绪生成（保留原有逻辑）
          if (emo === "thinking") spawn("smoke");
          else if (emo === "happy") {
            spawn("splash");
            if (m > 0.8 && Math.random() > 0.6) spawn("star");
          }
          else if (emo === "concerned") spawn("pulse");
          else if (emo === "focused") spawn("glow");
          else if (isWalking) spawn("trail");
          else spawn("drop");
        }
      }
      spawnTimerRef.current = spawnTimer;

      // 更新粒子 - 心情影响粒子行为
      const moodAlphaMultiplier = m < 0.3 ? 0.4 : m < 0.5 ? 0.65 : m < 0.8 ? 0.85 : 1.0;
      const moodLifeMultiplier = m < 0.3 ? 0.6 : m < 0.5 ? 0.8 : 1.0; // 心情低落时粒子消失更快

      for (let i = particles.length - 1; i >= 0; i--) {
        const p = particles[i];
        p.life -= 1 * moodLifeMultiplier;
        if (p.life <= 0) { particles.splice(i, 1); continue; }
        p.x += p.vx;
        p.y += p.vy;
        p.vx *= 0.995;
        p.vy *= 0.998;
        p.rotation += p.rotSpeed;
        drawParticle(p, p.life / p.maxLife, moodAlphaMultiplier);
      }

      // ── 绘制墨灵本体 ──
      ctx.save();
      ctx.translate(cx, cy + body.floatY + body.bounceY);
      ctx.rotate(body.tilt);
      ctx.scale(
        body.scaleX * body.squishX * (walkDirection === "left" ? -1 : 1),
        body.scaleY * body.squishY
      );

      // 入场墨晕
      if (entryElapsed < 800) {
        const p = entryElapsed / 800;
        ctx.save();
        ctx.globalAlpha = (1 - p) * 0.35;
        ctx.strokeStyle = inkColor(0.7);
        ctx.lineWidth = 1.5 * scale;
        ctx.beginPath();
        ctx.arc(0, 0, 10 * scale + p * 80 * scale, 0, Math.PI * 2);
        ctx.stroke();
        ctx.restore();
      }

      // 光晕效果 - 根据阶段添加多层次
      const stage = getStage(curLv);
      if (stage.glowLayers > 0) {
        const baseAuraR = (52 + stage.aura * 14) * sc * scale;
        // 底层大光晕
        inkHalo(0, 22 * sc * scale, baseAuraR * 1.3, 0.02 * m + 0.008);
        // 中层光晕
        inkHalo(0, 22 * sc * scale, baseAuraR, 0.035 * m + 0.015);
        // 顶部光晕
        inkHalo(0, -20 * sc * scale, baseAuraR * 0.6, 0.025 * m + 0.01);

        // Lv.7+: 额外的呼吸光晕
        if (stage.glowLayers >= 2) {
          const breathGlow = Math.sin(body.breathPhase * 2) * 0.015 + 0.025;
          inkHalo(0, 0, baseAuraR * 0.8, breathGlow * m);
        }

        // Lv.10+: 脉动光晕
        if (stage.glowLayers >= 3) {
          const pulseGlow = Math.sin(t * 0.003) * 0.01 + 0.02;
          inkHalo(0, 30 * sc * scale, baseAuraR * 0.5, pulseGlow * m);
          // 微粒子光晕点
          for (let i = 0; i < 3; i++) {
            const angle = (t * 0.001 + i * Math.PI * 2 / 3) % (Math.PI * 2);
            const dist = 45 * sc * scale;
            const px = Math.cos(angle) * dist;
            const py = Math.sin(angle) * dist * 0.4 + 10 * sc * scale;
            inkHalo(px, py, 8 * scale, 0.04 * m);
          }
        }
      }

      // 身体主体 - 根据阶段形态差异化绘制
      const bw = 48 * sc * scale;
      const bh = 55 * sc * scale;
      ctx.save();
      ctx.translate(0, 4 * sc * scale);

      // 根据 bodyShape 调整身体轮廓
      ctx.beginPath();
      if (stage.bodyShape === "round") {
        // Lv.1-3: 圆润的小墨团，接近圆形
        ctx.ellipse(0, 0, bw * 0.52, bh * 0.45, 0, 0, Math.PI * 2);
      } else if (stage.bodyShape === "oval") {
        // Lv.4-6: 微椭，开始有头部和身体的区分
        ctx.moveTo(0, -bh * 0.45);
        ctx.bezierCurveTo(bw * 0.55, -bh * 0.42, bw * 0.48, bh * 0.1, bw * 0.2, bh * 0.38);
        ctx.bezierCurveTo(bw * 0.06, bh * 0.48, -bw * 0.06, bh * 0.48, -bw * 0.2, bh * 0.38);
        ctx.bezierCurveTo(-bw * 0.48, bh * 0.1, -bw * 0.55, -bh * 0.42, 0, -bh * 0.45);
      } else if (stage.bodyShape === "humanoid") {
        // Lv.7-9: 人形，有明显的头部、躯干
        ctx.moveTo(0, -bh * 0.48);
        ctx.bezierCurveTo(bw * 0.65, -bh * 0.42, bw * 0.58, bh * 0.08, bw * 0.18, bh * 0.42);
        ctx.bezierCurveTo(bw * 0.04, bh * 0.52, -bw * 0.04, bh * 0.52, -bw * 0.18, bh * 0.42);
        ctx.bezierCurveTo(-bw * 0.58, bh * 0.08, -bw * 0.65, -bh * 0.42, 0, -bh * 0.48);
      } else {
        // Lv.10-12: 修长大师形态，更优雅的曲线
        ctx.moveTo(0, -bh * 0.52);
        ctx.bezierCurveTo(bw * 0.72, -bh * 0.45, bw * 0.62, bh * 0.05, bw * 0.15, bh * 0.45);
        ctx.bezierCurveTo(bw * 0.02, bh * 0.55, -bw * 0.02, bh * 0.55, -bw * 0.15, bh * 0.45);
        ctx.bezierCurveTo(-bw * 0.62, bh * 0.05, -bw * 0.72, -bh * 0.45, 0, -bh * 0.52);
      }
      ctx.closePath();

      // 渐变填充
      const bg = ctx.createRadialGradient(-bw * 0.12, -bh * 0.18, bw * 0.08, 0, 0, bw * 0.75);
      bg.addColorStop(0, inkColor(0.85));
      bg.addColorStop(0.6, inkColor(0.72));
      bg.addColorStop(1, inkColor(0.52));
      ctx.fillStyle = bg;
      ctx.fill();

      // 毛边纹理 - 密度随阶段增加
      ctx.save();
      ctx.clip();
      const textureCount = Math.floor(12 + stage.inkTexture * 18);
      for (let i = 0; i < textureCount; i++) {
        const angle = (i / textureCount) * Math.PI * 2;
        const r = bw * (0.58 + n(i + t * 0.0003) * 0.18);
        const blobSize = (2 + stage.inkTexture * 1.5) * scale;
        inkBlob(
          Math.cos(angle) * r,
          Math.sin(angle) * r * (bh / bw),
          blobSize, blobSize, 0.06 + stage.inkTexture * 0.04, t, 5
        );
      }
      ctx.restore();

      // 衣着装饰（Lv.4+）
      if (stage.clothing >= 1) {
        ctx.save();
        ctx.globalAlpha = 0.15 + stage.clothing * 0.08;
        ctx.strokeStyle = inkColor(0.3);
        ctx.lineWidth = 1.2 * scale;

        // Lv.4-6: 简单的领口线条
        if (stage.clothing >= 1) {
          ctx.beginPath();
          ctx.moveTo(-bw * 0.2, -bh * 0.15);
          ctx.quadraticCurveTo(0, -bh * 0.22, bw * 0.2, -bh * 0.15);
          ctx.stroke();
        }

        // Lv.7-9: 衣襟线条
        if (stage.clothing >= 2) {
          ctx.beginPath();
          ctx.moveTo(0, -bh * 0.15);
          ctx.lineTo(0, bh * 0.15);
          ctx.stroke();
          // 袖口
          ctx.beginPath();
          ctx.ellipse(-bw * 0.35, bh * 0.05, bw * 0.08, bh * 0.06, -0.3, 0, Math.PI * 2);
          ctx.stroke();
          ctx.beginPath();
          ctx.ellipse(bw * 0.35, bh * 0.05, bw * 0.08, bh * 0.06, 0.3, 0, Math.PI * 2);
          ctx.stroke();
        }

        // Lv.10-12: 华丽纹饰
        if (stage.clothing >= 3) {
          ctx.lineWidth = 0.8 * scale;
          for (let i = 0; i < 3; i++) {
            const y = -bh * 0.05 + i * bh * 0.12;
            ctx.beginPath();
            ctx.moveTo(-bw * 0.25, y);
            for (let j = 0; j < 5; j++) {
              const x = -bw * 0.25 + j * bw * 0.125;
              const wave = Math.sin(t * 0.002 + i + j) * 3 * scale;
              ctx.quadraticCurveTo(x + bw * 0.06, y + wave, x + bw * 0.125, y);
            }
            ctx.stroke();
          }
        }
        ctx.restore();
      }

      ctx.restore(); // 身体

      // 触手 / 小手 - 根据阶段配置
      if (stage.tentacles > 0) {
        ctx.save();
        ctx.lineWidth = (2 + stage.tentacleLength * 0.8) * sc * scale;
        ctx.lineCap = "round";
        ctx.strokeStyle = inkColor(0.35 + stage.tentacleLength * 0.15);
        const len = stage.tentacleLength;
        const tw = (side: number, i: number) =>
          n(t * 0.001 + i * 2 + side) * 7 * sc * scale * (0.5 + m * 0.5)
          + (emo === "happy" ? Math.sin(t * 0.005 + i) * 5 * sc * scale : 0);

        // 主触手（左右对称）
        const mainTentacles = Math.min(stage.tentacles, 2);
        for (let i = 0; i < mainTentacles; i++) {
          const side = i === 0 ? -1 : 1;
          const baseX = (32 + stage.tentacleLength * 4) * sc * scale * side;
          const endX = (40 + stage.tentacleLength * 8) * sc * scale * side;

          // Lv.1-3: 无触手（已在条件外）
          // Lv.4-6: 短小触手
          // Lv.7-9: 标准触手
          // Lv.10-12: 修长优雅的触手

          ctx.beginPath();
          ctx.moveTo(baseX, 4 * sc * scale);
          ctx.quadraticCurveTo(
            baseX * 1.4 + tw(side, 0),
            18 * sc * scale * len,
            endX + tw(side, 1),
            36 * sc * scale * len
          );
          ctx.stroke();

          // Lv.10-12: 触手尖端分叉
          if (stage.tentacles >= 4 && i === 0) {
            ctx.lineWidth *= 0.6;
            ctx.globalAlpha = 0.4;
            for (let j = 0; j < 2; j++) {
              const forkSide = j === 0 ? -1 : 1;
              ctx.beginPath();
              ctx.moveTo(endX + tw(side, 1), 36 * sc * scale * len);
              ctx.quadraticCurveTo(
                endX * 1.1 + forkSide * 8 * scale + tw(side, 2),
                42 * sc * scale * len,
                endX * 1.15 + forkSide * 12 * scale + tw(side, 3),
                48 * sc * scale * len
              );
              ctx.stroke();
            }
            ctx.globalAlpha = 1;
            ctx.lineWidth = (2 + stage.tentacleLength * 0.8) * sc * scale;
          }
        }

        // Lv.10-12: 额外的装饰触手
        if (stage.tentacles >= 4) {
          ctx.globalAlpha = 0.3;
          ctx.lineWidth *= 0.5;
          for (let i = 0; i < 2; i++) {
            const side = i === 0 ? -1 : 1;
            const startX = side * 20 * sc * scale;
            const startY = -15 * sc * scale;
            ctx.beginPath();
            ctx.moveTo(startX, startY);
            ctx.quadraticCurveTo(
              startX * 1.5 + tw(side, 4),
              startY + 15 * scale,
              startX * 1.8 + tw(side, 5),
              startY + 30 * scale
            );
            ctx.stroke();
          }
          ctx.globalAlpha = 1;
        }

        // thinking: 托腮
        if (emo === "thinking") {
          ctx.globalAlpha = 0.55;
          ctx.beginPath();
          ctx.moveTo(26 * sc * scale, -2 * scale);
          ctx.quadraticCurveTo(32 * sc * scale, -10 * sc * scale, 18 * sc * scale, -16 * sc * scale);
          ctx.stroke();
        }
        // concerned: 触手缩回
        if (emo === "concerned") {
          ctx.globalAlpha = 0.25;
          ctx.beginPath();
          ctx.moveTo(-28 * sc * scale, 8 * sc * scale);
          ctx.quadraticCurveTo(-32 * sc * scale, 18 * sc * scale, -22 * sc * scale, 28 * sc * scale);
          ctx.stroke();
          ctx.beginPath();
          ctx.moveTo(28 * sc * scale, 8 * sc * scale);
          ctx.quadraticCurveTo(32 * sc * scale, 18 * sc * scale, 22 * sc * scale, 28 * sc * scale);
          ctx.stroke();
        }
        ctx.restore();
      }

      // 眼睛 - 根据行为调整大小和视线
      const eyeY = -20 * sc * scale + body.headY;
      const eyeSpacing = 16 * sc * scale;
      let eyeSize = 6.5 * sc * scale * stage.eyeSize;
      let eyeOpen = body.eyeOpen;

      // 行为影响眼睛大小
      if (behavior.current === "curious") {
        // 好奇时眼睛放大
        eyeSize *= 1.2 + Math.sin(behavior.phase) * 0.1;
      } else if (behavior.current === "sleepy") {
        // 打瞌睡时眼睛半闭
        eyeOpen *= 0.3 + Math.sin(behavior.phase * 0.5) * 0.2;
      } else if (behavior.current === "yawn") {
        // 打哈欠时眯眼
        eyeOpen *= 0.5;
      }

      // 视线跟随（根据行为选择跟随鼠标还是随机扫视）
      let lookX = 0, lookY = 0;
      const dx = mouseRef.current.x - (cx + cx);
      const dy = mouseRef.current.y - (cy + cy);
      const lookAngle = Math.atan2(dy, dx);
      const lookDist = Math.min(3.5 * scale, Math.sqrt(dx * dx + dy * dy) * 0.008);

      if (behavior.current === "look_around") {
        // 张望时：视线缓慢扫视
        body.lookTargetX = lerp(body.lookTargetX, Math.sin(t * 0.002) * 2.5, 0.02);
        body.lookTargetY = lerp(body.lookTargetY, Math.cos(t * 0.0015) * 1.5, 0.02);
        lookX = body.lookTargetX;
        lookY = body.lookTargetY;
      } else if (behavior.current === "look_at_mouse") {
        // 盯着鼠标看：更紧密地跟随
        lookX = Math.cos(lookAngle) * lookDist * 1.5;
        lookY = Math.sin(lookAngle) * lookDist * 1.5;
      } else {
        // 默认：跟随鼠标
        lookX = Math.cos(lookAngle) * lookDist;
        lookY = Math.sin(lookAngle) * lookDist;
      }

      drawEye(-eyeSpacing, eyeY, eyeSize, eyeOpen, lookX, lookY);
      drawEye(eyeSpacing, eyeY, eyeSize, eyeOpen, lookX, lookY);

      // 墨珠
      if (stage.pearls > 0) {
        const pearlAngles = [0, Math.PI * 2 / 3, Math.PI * 4 / 3];
        for (let i = 0; i < stage.pearls; i++) {
          const pa = pearlAngles[i] + t * 0.0005 * (0.3 + m * 0.7);
          const pr = (50 + Math.sin(t * 0.001 + i) * 3.5) * sc * scale;
          const px = Math.cos(pa) * pr;
          const py = -36 * sc * scale + Math.sin(pa) * pr * 0.3;
          const pearlR = (2.8 + m * 1.8) * sc * scale;
          inkHalo(px, py, pearlR * 2, 0.07 * m + 0.02);
          ctx.beginPath();
          ctx.arc(px, py, pearlR, 0, Math.PI * 2);
          ctx.fillStyle = inkColor(0.28 + m * 0.38);
          ctx.fill();
          ctx.beginPath();
          ctx.arc(px - pearlR * 0.28, py - pearlR * 0.28, pearlR * 0.25, 0, Math.PI * 2);
          ctx.fillStyle = `rgba(255,255,255,${0.35 + m * 0.25})`;
          ctx.fill();
        }
      }

      // 飘带
      if (stage.ribbons > 0) {
        ctx.save();
        ctx.lineWidth = 1.8 * sc * scale;
        ctx.lineCap = "round";
        for (let i = 0; i < stage.ribbons; i++) {
          const side = i === 0 ? -1 : 1;
          const bx = side * 28 * sc * scale;
          const by = -12 * sc * scale;
          ctx.beginPath();
          ctx.moveTo(bx, by);
          for (let j = 1; j <= 4; j++) {
            const wave = Math.sin(t * 0.0015 + i * 2.5 + j * 0.8) * (5 + j * 4.5) * sc * scale;
            ctx.lineTo(bx + wave * side, by + j * 13 * sc * scale);
          }
          ctx.strokeStyle = inkColor(0.05 + m * 0.07);
          ctx.stroke();
        }
        ctx.restore();
      }

      ctx.restore(); // 整体变换

      rafRef.current = requestAnimationFrame(draw);
    };

    rafRef.current = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(rafRef.current);
  }, [emotion, isWalking, walkDirection, scale]);

  // 点击处理
  const handleClick = useCallback(() => {
    clickRef.current = 30;
    onClick?.();
  }, [onClick]);

  const W = Math.round(200 * scale);
  const H = Math.round(220 * scale);

  return (
    <div
      className="relative cursor-pointer select-none"
      style={{ width: W, height: H }}
      onClick={handleClick}
      onDoubleClick={onDoubleClick}
      role="button"
      tabIndex={0}
    >
      <canvas
        ref={canvasRef}
        className="pointer-events-none absolute inset-0"
        style={{ width: W, height: H }}
      />
    </div>
  );
}
