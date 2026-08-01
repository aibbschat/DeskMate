/**
 * Emotion state — DeskMate 桌宠的情绪名与「情绪 ↔ 桌宠状态」映射。
 *
 * 早期版本的粒子动画参数（density / speed / pulseFreq / glow / spread）已随
 * 粒子渲染器（ParticleCharacter）废弃而移除；这里只保留面板所需的最小字段
 * （color / colorSoft / label / description）与和 MascotState 的映射。
 */

import { type MascotState } from "./MascotStates";

export type EmotionName = "idle" | "thinking" | "happy" | "concerned" | "focused";

/** 状态面板所需的最小展示参数。 */
export interface EmotionParams {
  /** 主色（CSS 颜色字符串，如 "var(--accent)"）。 */
  color: string;
  /** 主色的柔和变体（用于发光/底色）。 */
  colorSoft: string;
  /** 中文标签。 */
  label: string;
  /** 状态面板的简短描述。 */
  description: string;
}

const STATES: Record<EmotionName, EmotionParams> = {
  idle: {
    color: "var(--accent)",
    colorSoft: "var(--accent-soft)",
    label: "待机",
    description: "等待中，随时准备帮助你",
  },
  thinking: {
    color: "var(--warn-ink)",
    colorSoft: "var(--warn-soft)",
    label: "思考中",
    description: "正在分析和推理...",
  },
  happy: {
    color: "var(--ok)",
    colorSoft: "var(--ok-soft)",
    label: "开心",
    description: "任务完成，一切顺利",
  },
  concerned: {
    color: "var(--danger)",
    colorSoft: "var(--danger-soft)",
    label: "关注",
    description: "发现了一些需要注意的事情",
  },
  focused: {
    color: "var(--teal-ink)",
    colorSoft: "var(--teal-soft)",
    label: "专注",
    description: "正在集中注意力处理任务",
  },
};

/** 获取某情绪的展示参数。 */
export function getEmotionParams(emotion: EmotionName): EmotionParams {
  return STATES[emotion];
}

/**
 * 将 EmotionName 映射到 MascotState（保持向后兼容：旧的 5 种 emotion 映射到新的 8 种 mascot state）。
 */
export function emotionToMascotState(emotion: EmotionName): MascotState {
  switch (emotion) {
    case "idle":
      return "idle";
    case "thinking":
      return "thinking";
    case "happy":
      return "happy";
    case "concerned":
      return "concerned";
    case "focused":
      return "working"; // focused -> working
    default:
      return "idle";
  }
}

/**
 * 将 MascotState 映射回 EmotionName，供依赖 EmotionName 的组件使用。
 */
export function mascotToEmotionName(state: MascotState): EmotionName {
  switch (state) {
    case "idle":
    case "walking":
      return "idle";
    case "thinking":
      return "thinking";
    case "working":
    case "reading":
    case "searching":
      return "focused";
    case "happy":
      return "happy";
    case "concerned":
      return "concerned";
    default:
      return "idle";
  }
}
