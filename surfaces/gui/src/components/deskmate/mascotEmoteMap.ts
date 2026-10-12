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
 *   2. **后端下发的持续心情**（/v1/deskmate/emotion 的 mood.face）→ 直接采用。
 *      阈值只在 deskmate/growth/emotion_engine.py 存在一份，前端不重算；
 *   3. 本地激素阈值（hormoneToExpression）→ 仅在后端未下发 face 时回落；
 *   4. 事件性情绪（happy → cheer 撒花；concerned → cry）由事件驱动即时覆盖；
 *   5. 默认 → smile（母版甜笑）。
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

/**
 * 后端下发的心情标签（/v1/deskmate/emotion 的 mood 字段）。
 *
 * 2026-10-12：阈值判定**只在后端存在一份**（deskmate/growth/emotion_engine.py
 * 的 MOOD_OFFSETS，按各激素单次变化上限的比例折算相对基线的偏移）。前端不再
 * 自己拿激素比阈值——那正是「对话说出的心情」与「桌宠的脸」分裂的根源：
 * 旧阈值(65/60/60/60)对 serotonin/oxytocin/cortisol 而言实际不可达，
 * 两边一起永远停在兜底 smile。
 */
export interface InnerEmotionMood {
  label: string;
  detail: string;
  intensity: number;
  face: ExpressionId;
}

/**
 * 旧版激素绝对阈值——**仅作 API 未返回 mood.face 时的回落**，不再是真相源。
 * 保留是为了兼容尚未升级的后端；后端在线时一律走 InnerEmotionMood.face。
 */
export const HORMONE_THRESHOLDS = {
  CORTISOL_HIGH: 60,
  DOPAMINE_HIGH: 65,
  DOPAMINE_LOW: 50,
  SEROTONIN_HIGH: 60,
  OXYTOCIN_HIGH: 60,
} as const;

const EXPRESSION_IDS: readonly ExpressionId[] = [
  "smile", "laugh", "cheer", "cry", "angry", "shock", "sleepy", "shy", "neutral",
];

/** 校验后端下发的 face 是合法表情帧 id（防脏数据把脸渲染成空）。 */
export function asExpressionId(v: unknown): ExpressionId | null {
  return typeof v === "string" && (EXPRESSION_IDS as readonly string[]).includes(v)
    ? (v as ExpressionId)
    : null;
}

/** 瞬时工作状态：一律用平静中性脸（配合「…」气泡表达思考）。 */
const WORKING_STATES: MascotState[] = ["thinking", "working", "reading", "searching"];

/** 判断是否为瞬时工作状态。 */
export function isWorkingState(state: MascotState | null | undefined): boolean {
  return !!state && WORKING_STATES.includes(state);
}

/**
 * 后端心情 → 桌宠姿态/粒子层（低头、跳跃…）。
 *
 * 与旧的 hormoneToMascot 同源，但改吃后端 face，不再自比阈值。
 * 返回 null 表示「不覆盖当前姿态」（亲近/平淡时让既有姿态自然延续）。
 */
export function faceToMascotState(face: ExpressionId | null | undefined): MascotState | null {
  switch (face) {
    case "angry":
    case "cry":
      return "concerned";
    case "laugh":
      return "happy";
    case "sleepy":
      return "idle";
    default:
      return null;
  }
}

/**
 * 激素 → 表情帧（**回落路径**，仅当后端未下发 mood.face 时使用）。
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
    // 注意：「concerned」是 App 的 UI/活动状态（建议中 / 忙碌 / 工具出错），
    // **不是小桔的情绪**。情绪脸只由后端心情推导（face=cry），UI 状态
    // 不得劫持情绪脸 → 映射为 neutral（思考/工作中性脸），避免「每蹦一条建议就哭」。
    case "concerned":
      return "neutral";
    default:
      return "smile";
  }
}

/**
 * 最终表情帧解析。
 *
 * @param hormone     P2 情绪底色（4 激素），可能为 null（接口未返回）
 * @param mascotState 桌宠运行状态（事件驱动）
 * @param backendFace 后端下发的心情表情帧（权威）；缺失时回落本地激素阈值
 *
 * 规则：瞬时工作状态 > 后端心情 > 本地激素阈值 > 运行状态映射。
 */
export function resolveFace(
  hormone: HormoneState | null | undefined,
  mascotState: MascotState | null | undefined,
  backendFace?: ExpressionId | null,
): ExpressionId {
  if (isWorkingState(mascotState)) return "neutral";
  const face = asExpressionId(backendFace);
  if (face) return face;
  if (!hormone || typeof hormone.dopamine !== "number") {
    return mascotStateToExpression(mascotState);
  }
  return hormoneToExpression(hormone);
}
