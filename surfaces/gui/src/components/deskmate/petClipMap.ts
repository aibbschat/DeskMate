/**
 * petClipMap — 表情 / 情绪状态 → 桌宠序列帧 clip 名（纯函数，无副作用）。
 *
 * 素材：public/pet-clips/<clip>/frames/<clip>_NNN.webp（720² 透明 WebP，42ms/帧）
 * 共九段：idle / working / thinking / happy / cry / angry / shock / sleepy / shy
 *
 * 为什么单独抽成模块：
 *   这段选择逻辑原先内联在 XiaojuMascot 的 IIFE 里，无测试、无类型约束。
 *   写错一条分支的表现是「某种情绪下她一直站着呼吸」——不报错也不崩溃，
 *   肉眼几乎无法判断是素材坏了还是分支没命中。抽出来后由
 *   petClipMap.test.ts 逐条锁死九种映射。
 *
 * 判定顺序（与 mascotEmoteMap.resolveFace 的优先级保持一致）：
 *   1. 具名情绪（cry/angry/shock/sleepy/shy/laugh/cheer）→ 自己的素材；
 *   2. emotion=happy → happy；
 *   3. emotion=thinking → thinking（托下巴想办法）、emotion=focused → working（低头做事）。
 *      ⚠️ 关键：工作状态下 resolveFace 返回的是 "neutral"（isWorkingState 优先），
 *      所以工作素材只能靠 emotion 反推，不能只看 expression；
 *   4. 走路暂无独立素材，与 idle 同路；
 *   5. 其余（smile / neutral / 未定义）→ idle 常态呼吸。
 */

import type { EmotionName } from "./EmotionState";
import type { ExpressionId } from "./mascotEmoteMap";

/** 九段序列帧 clip 名，与 public/pet-clips/ 下目录、manifest.json 的键一一对应。 */
export const PET_CLIP_NAMES = [
  "idle",
  "working",
  "thinking",
  "happy",
  "cry",
  "angry",
  "shock",
  "sleepy",
  "shy",
] as const;

export type PetClipName = (typeof PET_CLIP_NAMES)[number];

/**
 * 解析当前应播放的 clip。
 *
 * @param expression 显式表情帧（mascotEmoteMap.resolveFace 的结果，权威）
 * @param emotion    EmotionName（由 MascotState 反推；工作态信息只存在这里）
 * @param isWalking  是否正在走路（暂无专用素材）
 */
export function resolveClip(
  expression: ExpressionId | null | undefined,
  emotion: EmotionName | null | undefined,
  isWalking = false,
): PetClipName {
  switch (expression) {
    case "cry":
      return "cry";
    case "angry":
      return "angry";
    case "shock":
      return "shock";
    case "sleepy":
      return "sleepy";
    case "shy":
      return "shy";
    case "laugh":
    case "cheer":
      return "happy";
    default:
      break;
  }

  // 工作态：expression 此时是 neutral，只能靠 emotion 区分「在想」与「在做」
  if (emotion === "happy") return "happy";
  if (emotion === "thinking") return "thinking";
  if (emotion === "focused") return "working";

  // 走路与站立共用 idle（没有 walk 素材，不硬凑）
  if (isWalking) return "idle";

  return "idle";
}
