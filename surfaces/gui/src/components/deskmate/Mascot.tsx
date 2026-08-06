/**
 * Mascot — 墨灵桌宠（程序化水墨生物 V2）。
 *
 * 完全用 Canvas 程序化绘制，不再依赖静态 PNG。
 * 每个 growth 数值都变成可见的视觉反馈：
 *   - vitality → 呼吸节奏 + 墨渗浓淡
 *   - mood → 浮动幅度 + 粒子活力
 *   - level → 阶段光环（墨晕 / 墨珠 / 飘带）
 *   - emotion → 粒子行为模式 + 肢体语言
 *
 * 眼睛跟随鼠标，会随机眨眼，情绪驱动身体语言。
 *
 * Props 接口向后兼容：旧的 emotion/walkDirection/isWalking/scale/onClick/onDoubleClick 保留，
 * 新增 mood/vitality/level 可选 props。
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

const STAGES = [
  { name: "墨童", min: 1, max: 3, bodyScale: 0.75, eyeSize: 0.85, aura: 0, pearls: 0, ribbons: 0 },
  { name: "墨生", min: 4, max: 6, bodyScale: 0.88, eyeSize: 0.93, aura: 1, pearls: 0, ribbons: 0 },
  { name: "墨客", min: 7, max: 9, bodyScale: 1.0, eyeSize: 1.0, aura: 1, pearls: 3, ribbons: 0 },
  { name: "墨仙", min: 10, max: 12, bodyScale: 1.08, eyeSize: 1.08, aura: 2, pearls: 3, ribbons: 2 },
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
  type: "drop" | "smoke" | "splash" | "pulse" | "glow" | "click" | "trail";
  x: number; y: number;
  vx: number; vy: number;
  life: number; maxLife: number;
  size: number; alpha: number;
  rotation: number; rotSpeed: number;
  born: number;
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
      }
      p.life = p.maxLife;
      particles.push(p);
    }

    // ── 绘制粒子 ──
    function drawParticle(p: Particle, lr: number) {
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.rotation);
      ctx.globalAlpha = p.alpha * lr;

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

      // ── 更新身体动画 ──
      const breathSpeed = v > 0.8 ? 0.0008 : v > 0.5 ? 0.001 : v > 0.2 ? 0.0016 : 0.0004;
      const breathAmp = v > 0.8 ? 0.055 : v > 0.5 ? 0.038 : v > 0.2 ? 0.018 : 0.007;
      body.breathPhase += breathSpeed * 16;
      body.scaleY = 1 + Math.sin(body.breathPhase) * breathAmp;
      body.scaleX = 1 - Math.sin(body.breathPhase) * breathAmp * 0.3;

      const floatSpeed = m > 0.8 ? 0.0006 : m > 0.5 ? 0.0004 : m > 0.2 ? 0.00025 : 0.00012;
      const floatAmp = m > 0.8 ? 7 * scale : m > 0.5 ? 5 * scale : m > 0.2 ? 2 * scale : 0.5 * scale;
      const floatDrop = m < 0.2 ? 6 * scale : 0;
      body.floatPhase += floatSpeed * 16;
      body.floatY = Math.sin(body.floatPhase) * floatAmp + floatDrop;

      // 情绪 → 头部微动 + 倾斜
      let targetHeadY = Math.sin(t * 0.0005) * 1 * scale;
      let targetTilt = Math.sin(t * 0.0003) * 0.008;
      if (emo === "thinking") {
        targetHeadY = Math.sin(t * 0.002) * 2.5 * scale;
        targetTilt = Math.sin(t * 0.001) * 0.018;
      } else if (emo === "happy") {
        targetHeadY = Math.sin(t * 0.004) * 3.5 * scale;
        targetTilt = Math.sin(t * 0.003) * 0.025;
      } else if (emo === "concerned") {
        targetHeadY = 2.5 * scale;
        targetTilt = Math.sin(t * 0.0008) * 0.012;
      } else if (emo === "focused") {
        targetHeadY = -1.5 * scale;
        targetTilt = 0;
      }
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

      // 点击反馈
      if (clickRef.current > 0) {
        clickRef.current--;
        if (clickRef.current === 28) {
          for (let i = 0; i < 8; i++) spawn("click");
        }
      }

      // ── 粒子系统 ──
      spawnTimer++;
      const rates: Record<string, number> = {
        idle: 85, thinking: 10, happy: 7, concerned: 13, focused: 11, walking: 22,
      };
      if (spawnTimer >= (rates[emo] ?? 85)) {
        spawnTimer = 0;
        if (emo === "thinking") spawn("smoke");
        else if (emo === "happy") spawn("splash");
        else if (emo === "concerned") spawn("pulse");
        else if (emo === "focused") spawn("glow");
        else if (isWalking) spawn("trail");
        else spawn("drop");
      }
      spawnTimerRef.current = spawnTimer;

      // 更新粒子
      for (let i = particles.length - 1; i >= 0; i--) {
        const p = particles[i];
        p.life -= 1;
        if (p.life <= 0) { particles.splice(i, 1); continue; }
        p.x += p.vx;
        p.y += p.vy;
        p.vx *= 0.995;
        p.vy *= 0.998;
        p.rotation += p.rotSpeed;
        drawParticle(p, p.life / p.maxLife);
      }

      // ── 绘制墨灵本体 ──
      ctx.save();
      ctx.translate(cx, cy + body.floatY);
      ctx.rotate(body.tilt);
      ctx.scale(body.scaleX * (walkDirection === "left" ? -1 : 1), body.scaleY);

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

      // 光环底座
      const stage = getStage(curLv);
      if (stage.aura > 0) {
        const auraR = (58 + stage.aura * 18) * sc * scale;
        inkHalo(0, 22 * sc * scale, auraR, 0.035 * m + 0.015);
        inkHalo(0, 48 * sc * scale, 42 * sc * scale, 0.05 * m + 0.015);
      }

      // 身体主体
      const bw = 48 * sc * scale;
      const bh = 55 * sc * scale;
      ctx.save();
      ctx.translate(0, 4 * sc * scale);
      ctx.beginPath();
      ctx.moveTo(0, -bh * 0.48);
      ctx.bezierCurveTo(bw * 0.68, -bh * 0.42, bw * 0.6, bh * 0.08, bw * 0.18, bh * 0.42);
      ctx.bezierCurveTo(bw * 0.04, bh * 0.52, -bw * 0.04, bh * 0.52, -bw * 0.18, bh * 0.42);
      ctx.bezierCurveTo(-bw * 0.6, bh * 0.08, -bw * 0.68, -bh * 0.42, 0, -bh * 0.48);
      ctx.closePath();
      const bg = ctx.createRadialGradient(-bw * 0.12, -bh * 0.18, bw * 0.08, 0, 0, bw * 0.75);
      bg.addColorStop(0, inkColor(0.85));
      bg.addColorStop(0.6, inkColor(0.72));
      bg.addColorStop(1, inkColor(0.52));
      ctx.fillStyle = bg;
      ctx.fill();
      // 毛边
      ctx.save();
      ctx.clip();
      for (let i = 0; i < 24; i++) {
        const angle = (i / 24) * Math.PI * 2;
        const r = bw * (0.58 + n(i + t * 0.0003) * 0.18);
        inkBlob(Math.cos(angle) * r, Math.sin(angle) * r * (bh / bw), 2.5 * scale, 2.5 * scale, 0.08, t, 5);
      }
      ctx.restore();
      ctx.restore(); // 身体

      // 触手 / 小手
      ctx.save();
      ctx.lineWidth = 2.5 * sc * scale;
      ctx.lineCap = "round";
      ctx.strokeStyle = inkColor(0.45);
      const tw = (side: number, i: number) => n(t * 0.001 + i * 2 + side) * 7 * sc * scale * (0.5 + m * 0.5)
        + (emo === "happy" ? Math.sin(t * 0.005 + i) * 5 * sc * scale : 0);
      // 左
      ctx.beginPath();
      ctx.moveTo(-32 * sc * scale, 4 * sc * scale);
      ctx.quadraticCurveTo(-46 * sc * scale + tw(-1, 0), 18 * sc * scale, -40 * sc * scale + tw(-1, 1), 36 * sc * scale);
      ctx.stroke();
      // 右
      ctx.beginPath();
      ctx.moveTo(32 * sc * scale, 4 * sc * scale);
      ctx.quadraticCurveTo(46 * sc * scale + tw(1, 0), 18 * sc * scale, 40 * sc * scale + tw(1, 1), 36 * sc * scale);
      ctx.stroke();
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

      // 眼睛
      const eyeY = -20 * sc * scale + body.headY;
      const eyeSpacing = 16 * sc * scale;
      const eyeSize = 6.5 * sc * scale * stage.eyeSize;
      const dx = mouseRef.current.x - (cx + cx); // 注意：叠加了外层 position
      const dy = mouseRef.current.y - (cy + cy);
      const lookAngle = Math.atan2(dy, dx);
      const lookDist = Math.min(3.5 * scale, Math.sqrt(dx * dx + dy * dy) * 0.008);
      const lookX = Math.cos(lookAngle) * lookDist;
      const lookY = Math.sin(lookAngle) * lookDist;
      drawEye(-eyeSpacing, eyeY, eyeSize, body.eyeOpen, lookX, lookY);
      drawEye(eyeSpacing, eyeY, eyeSize, body.eyeOpen, lookX, lookY);

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
