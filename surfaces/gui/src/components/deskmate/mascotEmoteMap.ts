/**
 * mascotEmoteMap — 小桔桌宠「P2 情绪底色（4 激素）→ 表情帧」纯函数映射。
 *
 * 背景：桌宠主体原本是固定一张甜笑 PNG（mascot-xiaoju.png），情绪只通过
 * CSS 姿态动画 + 粒子特效表达，脸不变。B 方案（换脸帧序列）为此新增 8 张
 * 同构图透明表情帧（assets/mascot-frames/*.png），由本模块把激素与桌宠
 * 运行状态解析成表情帧 id，再由 XiaojuMascot 渲染并淡入淡出切换。
 *
 * 纯函数、无副作用、不依赖 DOM，可直接单测。
 *
 * 优先级设计：
 *   1. 瞬时工作状态（thinking / working / reading / searching）→ neutral，
 *      配合现有「…」思考气泡与摆头动画表达思考（资产侧已确认不生成 think 脸）；
 *   2. 激素持续心情（dopamine↑+cortisol↓ → laugh；cortisol↑ → angry/cry；
 *      serotonin↑ → sleepy；oxytocin↑ → shy）；
 *   3. 事件性情绪（happy → cheer 撒花；concerned → cry）由事件驱动即时覆盖；
 *   4. 默认 → smile（母版甜笑）。
 */

import type { MascotState } from "./MascotStates";

/** 表情帧 id，与 assets/mascot-frames/*.png 一一对应。 */
export type ExpressionId =
  | "smile" // 母版甜笑（默认）
  | "laugh" // 开心大笑
  | "cheer" // 惊喜撒花
  | "cry" // 委屈难过
  | "angry" // 生气烦躁
  | "shock" // 震惊
  | "sleepy" // 犯困
  | "shy" // 害羞
  | "neutral"; // 平静中性（思考/工作）

/** P2 情绪底色：4 激素（取值域 0~100）。 */
export interface HormoneState {
  dopamine: number;
  serotonin: number;
  oxytocin: number;
  cortisol: number;
}

/** 激素判定阈值（与 DeskMateOverlay 旧 hormoneToMascot 口径对齐并扩展）。 */
export const HORMONE_THRESHOLDS = {
  CORTISOL_HIGH: 60,
  DOPAMINE_HIGH: 65,
  DOPAMINE_LOW: 50,
  SEROTONIN_HIGH: 60,
  OXYTOCIN_HIGH: 60,
} as const;

/** 瞬时工作状态：一律用平静中性脸（配合「…」气泡表达思考）。 */
const WORKING_STATES: MascotState[] = ["thinking", "working", "reading", "searching"];

/** 判断是否为瞬时工作状态。 */
export function isWorkingState(state: MascotState | null | undefined): boolean {
  return !!state && WORKING_STATES.includes(state);
}

/**
 * 激素 → 表情帧（持续心情层）。
 *
 * - cortisol ≥ 60（压力主导）：dopamine 也高（兴奋又焦躁）→ angry，否则 → cry
 * - dopamine ≥ 65（开心）→ laugh
 * - serotonin ≥ 60 且 dopamine < 50（平静放松）→ sleepy
 * - oxytocin ≥ 60（亲近温暖）→ shy
 * - 其余 → smile
 */
export function hormoneToExpression(h: HormoneState): ExpressionId {
  const { dopamine, serotonin, oxytocin, cortisol } = h;
  const T = HORMONE_THRESHOLDS;
  if (cortisol >= T.CORTISOL_HIGH) {
    return dopamine >= T.DOPAMINE_HIGH ? "angry" : "cry";
  }
  if (dopamine >= T.DOPAMINE_HIGH) return "laugh";
  if (serotonin >= T.SEROTONIN_HIGH && dopamine < T.DOPAMINE_LOW) return "sleepy";
  if (oxytocin >= T.OXYTOCIN_HIGH) return "shy";
  return "smile";
}

/**
 * 桌宠运行状态 → 表情帧（瞬时/事件层）。
 *
 * - 工作类状态 → neutral
 * - happy → cheer（撒花）
 * - concerned → cry
 * - 其余（idle / walking）→ smile（不覆盖激素层）
 */
export function mascotStateToExpression(state: MascotState | null | undefined): ExpressionId {
  if (isWorkingState(state)) return "neutral";
  switch (state) {
    case "happy":
      return "cheer";
    case "concerned":
      return "cry";
    default:
      return "smile";
  }
}

/**
 * 最终表情帧解析。
 *
 * @param hormone     P2 情绪底色（4 激素），可能为 null（接口未返回）
 * @param mascotState 桌宠运行状态（事件驱动）
 *
 * 规则：瞬时工作状态 > 激素持续心情；激素缺失时退回运行状态映射。
 */
export function resolveFace(
  hormone: HormoneState | null | undefined,
  mascotState: MascotState | null | undefined,
): ExpressionId {
  if (!hormone || typeof hormone.dopamine !== "number") {
    return mascotStateToExpression(mascotState);
  }
  if (isWorkingState(mascotState)) return "neutral";
  return hormoneToExpression(hormone);
}
