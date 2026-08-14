import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { type MascotState, mascotStateFromAgentState } from "./MascotStates";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface Position {
  x: number;
  y: number;
}

export interface WalkBounds {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
}

export interface UseMascotWalkOptions {
  /** 桌宠尺寸（用于边界计算）。 */
  size?: number;
  /** 行走边界（默认自动计算）。 */
  bounds?: WalkBounds;
  /** 是否启用行走（默认 true）。 */
  enabled?: boolean;
  /** 行走模式："free" 自由游走；"dock" 仅沿输入框上方横边左右走；"corner" 固定右下角不游走。 */
  mode?: "free" | "dock" | "corner";
  /** 停留时间范围 [min, max] (ms)。 */
  idleDuration?: [number, number];
  /** 行走时间范围 [min, max] (ms)。 */
  walkDuration?: [number, number];
  /** 初始位置。 */
  initialPosition?: Position;
}

export interface UseMascotWalkReturn {
  /** 当前位置。 */
  position: Position;
  /** 当前状态。 */
  state: MascotState;
  /** 朝向。 */
  direction: "left" | "right";
  /** 是否正在行走。 */
  isWalking: boolean;
  /** 设置桌宠状态（外部调用）。 */
  setState: (state: MascotState) => void;
  /** 跳转到指定位置。 */
  teleportTo: (pos: Position) => void;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function randomBetween(range: [number, number]): number {
  return range[0] + Math.random() * (range[1] - range[0]);
}

function randomPoint(bounds: WalkBounds): Position {
  return {
    x: bounds.minX + Math.random() * (bounds.maxX - bounds.minX),
    y: bounds.minY + Math.random() * (bounds.maxY - bounds.minY),
  };
}

/** 右下角固定坐标：根据桌宠实际渲染尺寸（200×220 等比于 size）留出边距。 */
function cornerPosition(mascotSize: number): Position {
  const pad = 16;
  const scale = mascotSize / 180;
  const w = 200 * scale;
  const h = 220 * scale;
  return {
    x: Math.max(pad, window.innerWidth - w - pad),
    y: Math.max(pad, window.innerHeight - h - pad),
  };
}

function lerp(start: number, end: number, t: number): number {
  return start + (end - start) * t;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** 计算行走边界（基于输入框位置）。 */
function calculateDefaultBounds(mascotSize: number, mode: "free" | "dock" | "corner" = "free"): WalkBounds {
  // 尝试找到聊天输入框
  const composer =
    document.querySelector('[data-testid="composer"]') ||
    document.querySelector(".composer") ||
    document.querySelector(".composer-wrap") ||
    document.querySelector('textarea[placeholder*="Ask"]');

  const padding = 24;
  const mascotHeight = mascotSize * 1.2;

  if (mode === "corner") {
    const c = cornerPosition(mascotSize);
    return { minX: c.x, maxX: c.x, minY: c.y, maxY: c.y };
  }

  if (mode === "dock") {
    // 首页：桌宠只沿输入框上方那条横边左右走，不进输入框、不占屏幕中部。
    const top = composer
      ? composer.getBoundingClientRect().top
      : window.innerHeight - 120;
    const stripY = Math.max(top - mascotHeight - 12, padding + 40);
    return {
      minX: padding,
      maxX: Math.max(window.innerWidth - mascotSize - padding, padding + 100),
      minY: stripY,
      maxY: stripY,
    };
  }

  // 自由模式（默认）：从窗口顶部到输入框上方，大半个屏幕内游走。
  const maxY = composer
    ? Math.max(composer.getBoundingClientRect().top - mascotHeight - padding, padding + 60)
    : Math.max(window.innerHeight - mascotHeight - padding, padding + 100);

  return {
    minX: padding,
    maxX: Math.max(window.innerWidth - mascotSize - padding, padding + 100),
    minY: padding,
    maxY,
  };
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

/**
 * useMascotWalk — 桌宠行走逻辑 hook。
 *
 * 负责：
 * 1. 随机行走（闲时在边界内走动）
 * 2. 响应 agent 状态变化（工作/思考时停止行走）
 * 3. 管理朝向和位置
 */
export function useMascotWalk(options: UseMascotWalkOptions = {}): UseMascotWalkReturn {
  const {
    size = 110,
    bounds: customBounds,
    enabled = true,
    mode = "free",
    idleDuration = [3000, 8000],
    walkDuration = [2000, 5000],
    initialPosition,
  } = options;

  const [position, setPosition] = useState<Position>(
    initialPosition ?? (() => {
      if (mode === "corner") return cornerPosition(size);
      if (mode === "dock") {
        const h = size * 1.2;
        return {
          x: window.innerWidth / 2 - size / 2,
          y: Math.max(window.innerHeight - h - 120, 80),
        };
      }
      return { x: window.innerWidth - 160, y: window.innerHeight - 180 };
    })(),
  );
  const [state, setStateInternal] = useState<MascotState>("idle");
  const [direction, setDirection] = useState<"left" | "right">("right");
  const [isWalking, setIsWalking] = useState(false);

  // Refs for async walk loop
  const positionRef = useRef(position);
  const stateRef = useRef(state);
  const enabledRef = useRef(enabled);
  const walkAbortRef = useRef(0);
  const isWalkingRef = useRef(isWalking);
  const rafIdRef = useRef(0);

  // 保持 ref 同步
  useEffect(() => {
    positionRef.current = position;
  }, [position]);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    enabledRef.current = enabled;
  }, [enabled]);

  useEffect(() => {
    isWalkingRef.current = isWalking;
  }, [isWalking]);

  // 用 ref 存 durations，避免内联数组每次 render 创建新引用导致 walkLoop 重启
  const idleDurRef = useRef(idleDuration);
  const walkDurRef = useRef(walkDuration);
  useEffect(() => { idleDurRef.current = idleDuration; }, [idleDuration]);
  useEffect(() => { walkDurRef.current = walkDuration; }, [walkDuration]);

  // 计算边界（useMemo 避免每次 render 都创建新对象导致 walkLoop 重启）
  const bounds = useMemo(
    () => customBounds ?? calculateDefaultBounds(size, mode),
    [customBounds, size, mode],
  );

  // 行走循环 — 只依赖 enabled 和 bounds 的值（不是引用）
  useEffect(() => {
    if (!enabled || mode === "corner") return;

    const walkId = ++walkAbortRef.current;

    // 进入行走循环时重置为 idle
    setStateInternal("idle");
    setIsWalking(false);

    const walkLoop = async () => {
      while (walkAbortRef.current === walkId && enabledRef.current) {
        // 只有在 idle 状态时才行走
        if (stateRef.current !== "idle") {
          await delay(500);
          continue;
        }

        // 停留一段时间
        await delay(randomBetween(idleDurRef.current));

        // 再次检查状态和 enabled
        if (walkAbortRef.current !== walkId || !enabledRef.current) break;
        if (stateRef.current !== "idle") continue;

        // 随机选择目标点
        const target = randomPoint(bounds);

        // 获取当前位置
        const startPos = { ...positionRef.current };

        // 判断方向
        setDirection(target.x > startPos.x ? "right" : "left");

        // 开始行走
        setIsWalking(true);
        setStateInternal("walking");

        // 动画行走
        const duration = randomBetween(walkDurRef.current);
        const startTime = Date.now();

        const animate = () => {
          if (walkAbortRef.current !== walkId) return;

          const elapsed = Date.now() - startTime;
          const progress = Math.min(elapsed / duration, 1);

          // 使用 easeInOutQuad 缓动
          const eased = progress < 0.5
            ? 2 * progress * progress
            : 1 - Math.pow(-2 * progress + 2, 2) / 2;

          setPosition({
            x: lerp(startPos.x, target.x, eased),
            y: lerp(startPos.y, target.y, eased),
          });

          if (progress < 1) {
            rafIdRef.current = requestAnimationFrame(animate);
          } else {
            // 行走结束
            setIsWalking(false);
            setStateInternal("idle");
          }
        };

        rafIdRef.current = requestAnimationFrame(animate);

        // 等待行走完成
        await delay(duration + 100);
      }
    };

    walkLoop();

    return () => {
      walkAbortRef.current++;
      if (rafIdRef.current) {
        cancelAnimationFrame(rafIdRef.current);
        rafIdRef.current = 0;
      }
    };
  }, [enabled, bounds.minX, bounds.maxX, bounds.minY, bounds.maxY, size]);

  // 监听 agent 状态变化
  useEffect(() => {
    const handleStatus = (e: Event) => {
      const detail = (e as CustomEvent<{ activity?: string; emotion?: string }>).detail;
      if (detail?.activity) {
        const newState = mascotStateFromAgentState(detail.activity);
        // 只在非行走状态时更新
        if (!isWalkingRef.current) {
          setStateInternal(newState);
        }
      }
    };

    window.addEventListener("deskmate:status", handleStatus);
    return () => window.removeEventListener("deskmate:status", handleStatus);
  }, []);

  // 窗口大小 / 页面滚动变化时重新计算边界（输入框位置会随滚动变化）
  useEffect(() => {
    const recompute = () => {
      if (!customBounds) {
        if (mode === "corner") {
          setPosition(cornerPosition(size));
          return;
        }
        const newBounds = calculateDefaultBounds(size, mode);
        // 确保当前位置仍在边界内
        setPosition((prev) => ({
          x: Math.max(newBounds.minX, Math.min(prev.x, newBounds.maxX)),
          y: Math.max(newBounds.minY, Math.min(prev.y, newBounds.maxY)),
        }));
      }
    };

    window.addEventListener("resize", recompute);
    // capture=true 以捕获任意容器（含聊天列表）的滚动
    window.addEventListener("scroll", recompute, true);
    return () => {
      window.removeEventListener("resize", recompute);
      window.removeEventListener("scroll", recompute, true);
    };
  }, [customBounds, size, mode]);

  // 外部设置状态
  const setState = useCallback(
    (newState: MascotState) => {
      if (!isWalkingRef.current) {
        setStateInternal(newState);
      }
    },
    [],
  );

  // 跳转到指定位置
  const teleportTo = useCallback((pos: Position) => {
    setPosition(pos);
  }, []);

  return {
    position,
    state,
    direction,
    isWalking,
    setState,
    teleportTo,
  };
}
