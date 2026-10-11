/**
 * faceStabilizer — 桌宠换脸稳定化状态机（纯函数，可单测）。
 *
 * 背景（2026-10-11 实机复现）：桌宠"一会儿哭一会儿笑"。根因三层：
 *   1. App.tsx 把 agent 每个生命周期都派成 deskmate:status 事件（thinking /
 *      tool error→concerned / turn_done→idle），一次任务多次工具出错 → 脸秒级连跳；
 *   2. emotion_engine 每轮对话后 LLM 重算激素（dopamine 单轮 ±15），映射阈值
 *      无滞回 → 激素在阈值附近横跳 → laugh/smile 反复；
 *   3. 前端对每个事件/每次 20s 轮询都立即换脸，无驻留、无稳定确认。
 *
 * 本模块提供稳定化规则：
 *   - 事件脸 = 临时驻留（dwell 后自动回落激素脸，不永久覆盖）；
 *   - 激素脸 = 连续一致确认（stableStreak 次轮询同一目标才切换）；
 *   - 任意切换受最小间隔（minSwitchIntervalMs）约束，防止秒级连跳。
 */

import type { ExpressionId } from "./mascotEmoteMap";

/** 稳定化状态机快照（由调用方持久化）。 */
export interface FaceState {
  /** 当前展示的脸。 */
  current: ExpressionId;
  /** 上次真正切换的时间戳（ms）。 */
  lastSwitchAt: number;
  /** 等待稳定确认的激素目标；null = 无待确认目标。 */
  pending: ExpressionId | null;
  /** pending 已连续一致出现的次数。 */
  pendingStreak: number;
  /** 当前临时事件脸；null = 无。 */
  eventFace: ExpressionId | null;
  /** 事件脸过期时间戳（ms）；eventFace 为 null 时无意义。 */
  eventExpiresAt: number;
}

export interface StabilizeParams {
  /** 激素脸切换需要的连续一致次数（1 = 立即，2 = 两轮确认）。 */
  stableStreak: number;
  /** 任意切换的最小间隔（ms），防秒级连跳。 */
  minSwitchIntervalMs: number;
}

export const DEFAULT_STABILIZE_PARAMS: StabilizeParams = {
  stableStreak: 2,
  minSwitchIntervalMs: 8000,
};

/** 各事件脸（ExpressionId）的驻留时长（ms）；undefined = 不覆盖（直接回落）。 */
export const EVENT_DWELL_MS: Partial<Record<ExpressionId, number>> = {
  cheer: 12_000, // happy 撒花
  cry: 12_000, // concerned 哭脸
  neutral: 15_000, // thinking/working 中性脸
  shock: 10_000,
  angry: 12_000,
};

/** 初始状态：激素脸（或默认 smile）直接成为当前脸。 */
export function initFaceState(current: ExpressionId, now: number): FaceState {
  return {
    current,
    lastSwitchAt: now,
    pending: null,
    pendingStreak: 0,
    eventFace: null,
    eventExpiresAt: 0,
  };
}

/**
 * 事件驱动：设置（或清除）临时事件脸。
 *
 * @param eventFace 事件想要的脸；"smile"/null 表示事件不覆盖（回落激素层）。
 * @param dwellMs   该事件脸的驻留时长；undefined 时不覆盖。
 */
export function applyEventFace(
  s: FaceState,
  eventFace: ExpressionId | null,
  dwellMs: number | undefined,
  now: number,
  _params: StabilizeParams = DEFAULT_STABILIZE_PARAMS,
): FaceState {
  // 不覆盖的事件（idle/smile/null）→ 清掉事件脸，交由激素层决定。
  if (!eventFace || eventFace === "smile" || dwellMs === undefined) {
    if (!s.eventFace) return s;
    return { ...s, eventFace: null, eventExpiresAt: 0 };
  }
  // 事件脸与当前一致 → 仅续期驻留，不重新切换。
  if (eventFace === s.current) {
    return { ...s, eventFace, eventExpiresAt: now + dwellMs };
  }
  // 事件脸是离散的用户可见状态（思考/工具出错/完成），即时切换；
  // 防连跳交给 dwell（到期自动回落）+ 激素流的最小间隔。
  return {
    ...s,
    current: eventFace,
    lastSwitchAt: now,
    pending: null,
    pendingStreak: 0,
    eventFace,
    eventExpiresAt: now + dwellMs,
  };
}

/**
 * 激素驱动：事件脸未过期时保持事件脸；否则对激素目标做连续一致确认。
 *
 * @param hormoneFace 本次轮询解析出的激素脸；null = 暂无激素数据（保持现状）。
 */
export function applyHormoneFace(
  s: FaceState,
  hormoneFace: ExpressionId | null,
  now: number,
  params: StabilizeParams = DEFAULT_STABILIZE_PARAMS,
): FaceState {
  // 事件脸仍驻留 → 不动当前脸。
  if (s.eventFace && now < s.eventExpiresAt) {
    return s;
  }
  // 事件脸刚过期 → 清掉，回落到激素流程（本轮到 next 轮询生效）。
  let base: FaceState = s;
  if (s.eventFace) {
    base = { ...s, eventFace: null, eventExpiresAt: 0 };
  }
  if (!hormoneFace || hormoneFace === base.current) {
    return { ...base, pending: null, pendingStreak: 0 };
  }

  // 目标与当前不同 → 稳定确认计数。
  let pending = base.pending;
  let streak = base.pendingStreak;
  if (pending !== hormoneFace) {
    pending = hormoneFace;
    streak = 1;
  } else {
    streak += 1;
  }
  if (streak < params.stableStreak) {
    return { ...base, pending, pendingStreak: streak };
  }
  // 连续一致达标 + 距上次切换 ≥ 最小间隔 → 切换。
  if (now - base.lastSwitchAt < params.minSwitchIntervalMs) {
    return { ...base, pending, pendingStreak: streak };
  }
  return {
    ...base,
    current: hormoneFace,
    lastSwitchAt: now,
    pending: null,
    pendingStreak: 0,
  };
}

/** 当前应展示的脸（current 已随事件即时切换，激素流待确认后切换）。 */
export function visibleFace(s: FaceState): ExpressionId {
  return s.current;
}
